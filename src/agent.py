"""LLM agent that drives the EDA engine through OpenAI function-calling."""

from __future__ import annotations

import json
import os
import signal as _signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv  # type: ignore[import]
    load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not installed; fall back to environment variables

from .eda_engine import EDAEngine
from .tools import TOOLS


# ---------------------------------------------------------------------------
# Timeout helper
# ---------------------------------------------------------------------------

class _Timeout:
    """Context manager that raises TimeoutError after *seconds*."""

    def __init__(self, seconds: int) -> None:
        self.seconds = seconds

    def _handler(self, signum: int, frame: Any) -> None:  # noqa: ANN001
        raise TimeoutError(f"Operation timed out after {self.seconds} s")

    def __enter__(self) -> "_Timeout":
        _signal.signal(_signal.SIGALRM, self._handler)
        _signal.alarm(self.seconds)
        return self

    def __exit__(self, *args: Any) -> None:
        _signal.alarm(0)


# ---------------------------------------------------------------------------
# EDAAgent
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are an expert EDA assistant. You have access to a set of netlist analysis "
    "and transformation tools. When the user gives a natural-language request about "
    "a gate-level Verilog netlist, call the appropriate tool(s) in the correct order. "
    "After all tool calls are complete, return a concise, clear natural-language answer "
    "describing what was found or what was changed. Do not discuss scoring or evaluation."
)

# Timeouts (seconds) per category
_FAST_OPS = {"read_design", "write_design", "set_testcase_name"}
_FAST_TIMEOUT = 60
_SLOW_TIMEOUT = 300


class EDAAgent:
    """LLM agent that translates natural-language EDA requests into tool calls.

    Args:
        config:     Parsed YAML config dict.
        eda_engine: Initialised EDAEngine instance.
    """

    def __init__(self, config: Dict[str, Any], eda_engine: EDAEngine) -> None:
        self._config = config
        self._engine = eda_engine
        self._io_handler: Optional[Any] = None  # set later by IOHandler
        self._current_case_name: str = ""
        self._last_tool_result: str = ""

        # Developer mode — when enabled, loads the conversation logger
        dev_cfg = config.get("developer", {})
        self._developer_mode: bool = bool(dev_cfg.get("mode", False))
        if self._developer_mode:
            from .developer import ConversationLogger, NoopLogger, get_logs_dir, extract_case_name  # noqa: E501
            log_base = dev_cfg.get("log_dir", None)
            self._logs_dir = get_logs_dir(log_base if log_base else None)
            self._LoggerCls = ConversationLogger
            self._extract_case_name = extract_case_name
        else:
            from .developer import NoopLogger  # noqa: E501
            self._logs_dir = Path(".")  # unused dummy
            self._LoggerCls = NoopLogger
            self._extract_case_name = lambda _: ""

        provider = config.get("provider", "openai").lower()
        if provider == "openai":
            self._client = self._build_openai_client(config)
            self._model: str = config["openai"]["model"]
        else:
            raise RuntimeError(
                f"Unsupported provider: {provider!r}. Only 'openai' is currently supported."
            )

        gen = config.get("generation", {})
        self._temperature: float = float(gen.get("temperature", 0.2))
        self._max_tokens: int = int(gen.get("max_output_tokens", 4096))

    # ------------------------------------------------------------------
    # Client construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_openai_client(config: Dict[str, Any]) -> Any:
        """Build and return an OpenAI client from config."""
        try:
            from openai import OpenAI  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "openai package is not installed. Run: pip install openai>=1.0.0"
            ) from exc

        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key or api_key.startswith("YOUR_"):
            raise RuntimeError(
                "OpenAI API key not configured. "
                "Set OPENAI_API_KEY in the .env file or export it as an environment variable."
            )
        return OpenAI(api_key=api_key)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_io_handler(self, io_handler: Any) -> None:
        """Register the IOHandler so the agent can call set_log_file."""
        self._io_handler = io_handler

    def build_state_summary(self) -> str:
        """Return a short string describing the current engine state.

        The IOHandler calls this after each request to feed context into
        the next request.  Includes: testcase name, loaded design, last
        operation performed.
        """
        parts: List[str] = []
        if self._current_case_name:
            parts.append(f"testcase='{self._current_case_name}'")

        try:
            nl = self._engine.netlist
            # Count gates by type
            gate_types: Dict[str, int] = {}
            for g in nl.nodes.values():
                gate_types[g.gate_type] = gate_types.get(g.gate_type, 0) + 1
            type_summary = ", ".join(
                f"{cnt}x {gt}" for gt, cnt in sorted(gate_types.items())
            )
            parts.append(
                f"design loaded: '{nl.module_name}' "
                f"({type_summary}; {len(nl.dffs)} DFFs; "
                f"{len(nl.primary_inputs)} PIs, {len(nl.primary_outputs)} POs)"
            )
        except (ValueError, AttributeError):
            pass  # no design loaded yet

        # Last operation performed (tracked by _last_tool_result)
        if hasattr(self, "_last_tool_result") and self._last_tool_result:
            parts.append(f"last operation: {self._last_tool_result}")

        return "; ".join(parts) if parts else "(no state)"

    @staticmethod
    def _summarize_tool_result(tool_name: str, result_str: str) -> str:
        """Create a one-line summary of a tool result for context tracking."""
        try:
            result = json.loads(result_str)
        except json.JSONDecodeError:
            return f"{tool_name} completed"

        if "error" in result:
            return f"{tool_name} FAILED: {result['error']}"

        data = result.get("result", result)

        if tool_name == "read_design":
            return f"loaded design via read_design"
        elif tool_name == "write_design":
            return f"wrote design via write_design"
        elif tool_name == "set_testcase_name":
            return f"testcase initialized via set_testcase_name"
        elif tool_name == "get_max_depth":
            return f"get_max_depth: depth={data.get('depth')}"
        elif tool_name == "find_instances_by_name_pattern":
            count = data.get("count", 0)
            instances = data.get("instances", [])
            return f"find_instances_by_name_pattern: found {count} instances ({', '.join(instances[:5])}{'...' if count > 5 else ''})"
        elif tool_name == "replace_gate":
            return f"replace_gate: {data.get('replaced')} → {data.get('new_type')}"
        elif tool_name == "replace_pattern":
            return f"replace_pattern: {data.get('replacements', 0)} replacements"
        elif tool_name == "insert_buffers_for_fanout":
            return f"insert_buffers_for_fanout: {data.get('buffers_inserted', 0)} buffers"
        elif tool_name == "balance_depth":
            return f"balance_depth: {data.get('buffers_inserted', 0)} buffers"
        elif tool_name == "remove_dangling_gates":
            return f"remove_dangling_gates: {data.get('gates_removed', 0)} gates removed"
        elif tool_name == "optimize_cone_depth":
            return f"optimize_cone_depth: {'OK' if data.get('success') else 'FAILED'}"
        else:
            return f"{tool_name} completed"

    # A simple counter for log-file naming (shared across all requests)
    _request_counter: int = 0

    def process_request(self, user_message: str, context_summary: str = "") -> str:
        """Process a natural-language EDA request and return a text response.

        Sends user_message to the LLM with function-calling enabled,
        dispatches tool calls to the EDA engine, and loops until the model
        returns a final text response.  When developer mode is enabled in
        config.yaml, the full conversation is logged to logs/.

        If *context_summary* is non-empty, it is inserted as an additional
        system message so the LLM knows the current engine state (loaded
        design, previous operations, etc.).

        Args:
            user_message:    The user's natural-language request.
            context_summary: Optional summary of previous operations' state.

        Returns:
            Final text response from the LLM.

        Raises:
            RuntimeError: On API errors or unsupported configurations.
        """
        EDAAgent._request_counter += 1

        # Extract case name; if not found in this line, reuse the last known one
        label = self._extract_case_name(user_message)
        if label:
            self._current_case_name = label
        else:
            label = self._current_case_name

        with self._LoggerCls(
            EDAAgent._request_counter, self._logs_dir, self._model, label=label
        ) as conv_log:
            conv_log.log_user_input(user_message)
            if context_summary:
                conv_log._fh.write(
                    f"  ── CONTEXT ──\n  {context_summary}\n  ─────────────\n\n"
                )

            messages: List[Dict[str, Any]] = []
            messages.append({"role": "system", "content": _SYSTEM_PROMPT})
            if context_summary:
                messages.append(
                    {"role": "system", "content": f"Current state: {context_summary}"}
                )
            messages.append({"role": "user", "content": user_message})

            turn = 0
            while True:
                turn += 1
                timeout = _FAST_TIMEOUT  # conservative default

                conv_log.log_llm_prompt(messages, turn)
                t0 = time.monotonic()
                response = self._call_llm(messages, timeout)
                elapsed = time.monotonic() - t0
                conv_log.log_llm_response(response, turn, elapsed)

                choice = response.choices[0]
                msg = choice.message

                # Append assistant message to history
                messages.append(msg.model_dump(exclude_unset=False))

                if choice.finish_reason == "tool_calls" and msg.tool_calls:
                    # Dispatch each tool call
                    for tc in msg.tool_calls:
                        tool_name = tc.function.name
                        op_timeout = (
                            _FAST_TIMEOUT if tool_name in _FAST_OPS else _SLOW_TIMEOUT
                        )
                        result_str = self._dispatch_tool(
                            tool_name, tc.function.arguments, op_timeout
                        )
                        conv_log.log_tool_result(tool_name, result_str)

                        # Track last operation for context summary
                        self._last_tool_result = self._summarize_tool_result(
                            tool_name, result_str
                        )

                        # If a design was just loaded, dump the netlist to the log
                        if tool_name == "read_design":
                            conv_log.log_netlist(self._engine.netlist)

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result_str,
                            }
                        )
                else:
                    # Final text response
                    final_text = msg.content or ""
                    conv_log.log_final_response(final_text)
                    return final_text

    # ------------------------------------------------------------------
    # Internal: LLM call
    # ------------------------------------------------------------------

    def _call_llm(
        self, messages: List[Dict[str, Any]], timeout: int
    ) -> Any:
        """Call the OpenAI chat completions API.

        Args:
            messages: Conversation history.
            timeout:  Seconds before raising TimeoutError.

        Returns:
            The API response object.

        Raises:
            RuntimeError: On API errors.
        """
        try:
            with _Timeout(timeout):
                return self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )
        except TimeoutError:
            raise RuntimeError(
                f"LLM call timed out after {timeout} seconds."
            )
        except Exception as exc:
            raise RuntimeError(f"OpenAI API error: {exc}") from exc

    # ------------------------------------------------------------------
    # Internal: tool dispatch
    # ------------------------------------------------------------------

    def _dispatch_tool(
        self, tool_name: str, arguments_json: str, timeout: int
    ) -> str:
        """Execute a tool call and return a JSON-serialisable result string.

        On failure the error message is returned so the LLM can decide
        how to proceed.

        Args:
            tool_name:       Name of the tool to call.
            arguments_json:  JSON string of tool arguments.
            timeout:         Seconds before aborting.

        Returns:
            JSON string with the tool result or an error description.
        """
        try:
            args: Dict[str, Any] = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"Invalid JSON arguments: {exc}"})

        try:
            with _Timeout(timeout):
                result = self._invoke_tool(tool_name, args)
            return json.dumps({"result": result})
        except TimeoutError:
            return json.dumps(
                {"error": f"Tool '{tool_name}' timed out after {timeout} s"}
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _invoke_tool(self, tool_name: str, args: Dict[str, Any]) -> Any:
        """Map a tool name to an EDAEngine method and call it.

        Args:
            tool_name: The tool name string.
            args:      Dictionary of parsed arguments.

        Returns:
            The raw Python return value of the EDA operation.

        Raises:
            ValueError: If the tool name is unknown.
        """
        eng = self._engine

        if tool_name == "read_design":
            eng.load(args["filepath"])
            return f"Design loaded from {args['filepath']!r}"

        if tool_name == "write_design":
            eng.save(args["filepath"])
            return f"Design written to {args['filepath']!r}"

        if tool_name == "set_testcase_name":
            case_name: str = args["case_name"]
            log_path: str = args.get("log_path") or f"{case_name}.log"
            if self._io_handler is not None:
                self._io_handler.set_log_file(log_path)
            return f"Testcase name set to {case_name!r}; log file: {log_path!r}"

        if tool_name == "get_max_depth":
            depth, path = eng.get_max_depth(args["source"], args["sink"])
            return {"depth": depth, "path": path}

        if tool_name == "path_passes_through":
            result = eng.path_passes_through(
                args["source"], args["sink"], args["node"]
            )
            return {"passes_through": result}

        if tool_name == "find_path_avoiding":
            path = eng.find_path_avoiding(
                args["source"], args["sink"], args["avoid"]
            )
            return {"path": path}

        if tool_name == "get_logic_cone":
            cone = eng.get_logic_cone(args["output_signal"])
            return {"gates": cone, "count": len(cone)}

        if tool_name == "count_cone_gates":
            count = eng.count_cone_gates(args["output_signal"])
            return {"count": count}

        if tool_name == "get_fanout":
            fanout = eng.get_fanout(args["net_name"])
            return {"fanout": fanout, "count": len(fanout)}

        if tool_name == "are_same_clock_domain":
            same = eng.are_same_clock_domain(args["dff1"], args["dff2"])
            return {"same_clock_domain": same}

        if tool_name == "insert_gate_before":
            new_inst = eng.insert_gate_before(
                args["target_instance"], args["gate_type"], args["extra_input"]
            )
            return {"new_instance": new_inst}

        if tool_name == "replace_gate":
            eng.replace_gate(
                args["instance_name"],
                args["new_gate_type"],
                args.get("extra_input"),
            )
            return {"replaced": args["instance_name"], "new_type": args["new_gate_type"]}

        if tool_name == "insert_buffers_for_fanout":
            n = eng.insert_buffers_for_fanout(args["net_name"], args["max_fanout"])
            return {"buffers_inserted": n}

        if tool_name == "balance_depth":
            n = eng.balance_depth(args["source"], args["sinks"])
            return {"buffers_inserted": n}

        if tool_name == "remove_dangling_gates":
            n = eng.remove_dangling_gates()
            return {"gates_removed": n}

        if tool_name == "optimize_cone_depth":
            success = eng.optimize_cone_depth(
                args["output_signal"], args["max_depth"]
            )
            return {"success": success}

        if tool_name == "replace_pattern":
            n = eng.replace_pattern(args["pattern"], args["replacement"])
            return {"replacements": n}

        if tool_name == "find_instances_by_name_pattern":
            instances = eng.find_instances_by_name_pattern(
                args.get("gate_type", ""), args["name_pattern"]
            )
            return {"instances": instances, "count": len(instances)}

        if tool_name == "check_equivalence":
            equiv = eng.check_equivalence(args["output"])
            return {"equivalent": equiv}

        raise ValueError(f"Unknown tool: {tool_name!r}")
