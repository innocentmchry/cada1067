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
    "describing what was found or what was changed. Do not discuss scoring or evaluation. "
    "User prompts arrive sequentially. If the current request asks for a result or fact "
    "already recorded in the ordered context history, answer directly from that history "
    "without calling another tool. Call tools only when the needed fact is absent from "
    "history or the user explicitly requests recomputation or current-state analysis. "
    "For numerical or statistical design queries whose answer is not already explicit in "
    "the ordered context history, use tools instead of estimating."
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
        self._context_history: List[str] = []

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

        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            api_key = str(config.get("openai", {}).get("api_key", "")).strip()
        if not api_key or api_key.startswith("YOUR_"):
            raise RuntimeError(
                "OpenAI API key not configured. "
                "Set OPENAI_API_KEY in the environment/.env file or set openai.api_key in config.yaml."
            )
        return OpenAI(api_key=api_key)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_io_handler(self, io_handler: Any) -> None:
        """Register the IOHandler so the agent can call set_log_file."""
        self._io_handler = io_handler

    def build_state_summary(self) -> str:
        """Return a short string describing the current engine state and all
        previous operations for this testcase.

        The IOHandler calls this after each request to feed context into
        the next request. Includes testcase name, original netlist path, and
        the full history of operations performed so far.
        """
        parts: List[str] = []
        if self._current_case_name:
            parts.append(f"testcase='{self._current_case_name}'")

        if self._engine.original_netlist_path:
            parts.append(f"original_netlist='{self._engine.original_netlist_path}'")

        # Full operation history
        if self._context_history:
            parts.append("operations so far in order:")
            for item in self._context_history:
                parts.append(f"  - {item}")

        return "\n".join(parts) if parts else "(no state)"

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
        elif tool_name == "count_gates":
            total = data.get("total_gates", "?")
            breakdown = data.get("breakdown", {})
            parts = ", ".join(f"{cnt}x {gt}" for gt, cnt in sorted(breakdown.items()) if cnt > 0)
            return f"count_gates: {total} total gates ({parts})"
        elif tool_name == "get_max_depth":
            return f"get_max_depth: depth={data.get('depth')}"
        elif tool_name == "get_max_depth_between_endpoint_classes":
            return (
                f"get_max_depth_between_endpoint_classes: "
                f"{data.get('source_class')} to {data.get('sink_class')} "
                f"depth={data.get('depth')}"
            )
        elif tool_name == "get_fanin_cone_depth":
            return (
                f"fanin cone depth of "
                f"{data.get('output')} = "
                f"{data.get('depth')}"
            )
        elif tool_name == "count_outputs_by_logic_depth":
            return (
                f"count_outputs_by_logic_depth: "
                f"{data.get('count')} outputs "
                f"{data.get('operator')} {data.get('threshold')}"
            )
        elif tool_name == "find_all_paths":
            summary = (
                f"find_all_paths: "
                f"{data.get('count')} paths "
                f"from {data.get('source')} "
                f"to {data.get('sink')}"
            )
            if data.get("file_path"):
                summary += f"; full list in {data['file_path']}"
            return summary
        elif tool_name == "find_register_to_register_paths":
            summary = f"find_register_to_register_paths: {data.get('count', 0)} paths"
            if data.get("file_path"):
                summary += f"; full list in {data['file_path']}"
            if data.get("truncated"):
                summary += "; result truncated at safety limit"
            return summary
        elif tool_name in {"get_net_fanout", "get_gate_output_fanout"}:
            count = data.get("count", 0)
            if data.get("file_path"):
                return f"{tool_name}: {count} direct gates; full list in {data['file_path']}"
            return f"{tool_name}: {count} direct gates ({', '.join(data.get('fanout', []))})"
        elif tool_name == "resolve_name_type":
            return f"resolve_name_type: {data.get('name')} is {data.get('type')}"
        elif tool_name == "get_gate_info":
            pins = ", ".join(
                f"{pin}={net}" for pin, net in data.get("pins", {}).items()
            )
            return (
                f"get_gate_info: {data.get('instance')} is {data.get('gate_type')} "
                f"({pins})"
            )
        elif tool_name in {"get_reachable_gates_from_net", "get_reachable_gates_from_gate"}:
            count = data.get("count", 0)
            if data.get("file_path"):
                return f"{tool_name}: {count} reachable gates; full list in {data['file_path']}"
            return f"{tool_name}: {count} reachable gates ({', '.join(data.get('gates', []))})"
        elif tool_name == "find_instances_by_name_pattern":
            count = data.get("count", 0)
            instances = data.get("instances", [])
            return f"find_instances_by_name_pattern: found {count} instances ({', '.join(instances[:5])}{'...' if count > 5 else ''})"
        elif tool_name == "rename_gate":
            return f"rename_gate: '{data.get('old_name')}' → '{data.get('new_name')}'"
        elif tool_name == "rename_wire":
            return f"rename_wire: '{data.get('old_name')}' → '{data.get('new_name')}'"
        elif tool_name == "replace_gate":
            return f"replace_gate: {data.get('replaced')} → {data.get('new_type')}"
        elif tool_name == "replace_pattern":
            return f"replace_pattern: {data.get('replacements', 0)} replacements"
        elif tool_name == "insert_buffers_for_fanout":
            return f"insert_buffers_for_fanout: {data.get('buffers_inserted', 0)} buffers"
        elif tool_name == "insert_dedicated_buffers_for_loads":
            return (
                f"insert_dedicated_buffers_for_loads: "
                f"{data.get('buffers_inserted', 0)} buffers on {data.get('net_name')}"
            )
        elif tool_name == "balance_depth":
            return f"balance_depth: {data.get('buffers_inserted', 0)} buffers"
        elif tool_name == "remove_dangling_gates":
            return f"remove_dangling_gates: {data.get('gates_removed', 0)} gates removed"
        elif tool_name == "collapse_inverter_pairs":
            return (
                f"collapse_inverter_pairs: {data.get('pairs_collapsed', 0)} pairs collapsed, "
                f"{data.get('gates_removed', 0)} gates removed"
            )
        elif tool_name == "replace_gate_type_in_cone":
            ok = data.get("success", False)
            if ok:
                replaced = data.get("replaced", 0)
                skipped  = data.get("skipped", 0)
                src   = data.get("source_type", "?")
                tgt   = data.get("target_types", [])
                scope = data.get("output_signal") or "whole design"
                return (
                    f"replace_gate_type_in_cone: replaced {replaced}x '{src}' with {tgt} "
                    f"in cone of {scope}" +
                    (f" ({skipped} skipped)" if skipped else "")
                )
            else:
                return f"replace_gate_type_in_cone: FAILED — {data.get('reason', 'unknown error')}"
        elif tool_name == "remap_cone_with_gates":
            ok = data.get("success", False)
            if ok:
                before = data.get("gates_before", "?")
                after  = data.get("gates_after", "?")
                sig    = data.get("output_signal", "?")
                gates  = data.get("allowed_gates", [])
                return f"remap_cone_with_gates: cone of {sig} remapped to {gates} — {before} gates → {after} gates"
            else:
                return f"remap_cone_with_gates: FAILED — {data.get('reason', 'unknown error')}"
        elif tool_name == "remap_design_with_gates":
            if data.get("success", False):
                return (
                    f"remap_design_with_gates: whole design remapped to "
                    f"{data.get('allowed_gates', [])} — "
                    f"{data.get('gates_before', '?')} gates → {data.get('gates_after', '?')} gates"
                )
            return f"remap_design_with_gates: FAILED — {data.get('reason', 'unknown error')}"
        elif tool_name == "fraig_merge_equivalent_gates":
            if data.get("success", False):
                return (
                    f"fraig_merge_equivalent_gates: "
                    f"{data.get('gates_before', '?')} gates → {data.get('gates_after', '?')} gates "
                    f"(reduction {data.get('gate_reduction', '?')})"
                )
            return f"fraig_merge_equivalent_gates: FAILED"
        elif tool_name == "check_signal_equivalence":
            return f"check_signal_equivalence: equivalent={data.get('equivalent')}"
        elif tool_name == "check_signal_constant":
            answer = "yes" if data.get("always_equal") else "no"
            return (
                f"check_signal_constant: {data.get('signal')} always equals "
                f"{data.get('value')} = {answer}"
            )
        elif tool_name == "check_design_equivalence":
            return (
                f"check_design_equivalence: {data.get('status', 'UNKNOWN')} "
                f"against {data.get('original_netlist', 'original design')}"
            )
        elif tool_name == "optimize_cone_depth":
            return f"optimize_cone_depth: {'OK' if data.get('success') else 'FAILED'}"
        elif tool_name == "reduce_critical_path":
            before = data.get('depth_before', '?')
            after = data.get('depth_after', '?')
            imp = data.get('improvement', '?')
            ok = data.get('success', False)
            restriction = data.get('allowed_gates')
            restricted = f" using only {restriction}" if restriction else ""
            return f"reduce_critical_path: depth {before} → {after} (improved by {imp}){restricted} {'OK' if ok else 'NO IMPROVEMENT'}"
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
            if context_summary and self._developer_mode:
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

                        # Track operation in context history
                        summary = self._summarize_tool_result(
                            tool_name, result_str
                        )
                        self._context_history.append(summary)

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
            return f"Design loaded from {eng.original_netlist_path!r}"

        if tool_name == "write_design":
            filepath = str(args.get("filepath") or "").strip()
            if not filepath:
                base_name = self._current_case_name or "design"
                filepath = f"{base_name}_out.v"
            eng.save(filepath)
            return f"Design written to {filepath!r}"

        if tool_name == "count_gates":
            return eng.count_gates()


        if tool_name == "set_testcase_name":
            case_name: str = args["case_name"]
            log_path: str = str(args.get("log_path") or "").strip()
            if not log_path:
                log_path = f"{case_name}.log"
            if self._io_handler is not None:
                self._io_handler.set_log_file(log_path)
            self._current_case_name = case_name
            return f"Testcase name set to {case_name!r}; log file: {log_path!r}"

        if tool_name == "get_max_depth":
            depth, path = eng.get_max_depth(args["source"], args["sink"])
            return {"depth": depth, "path": path}

        if tool_name == "get_max_depth_between_endpoint_classes":
            return eng.get_max_depth_between_endpoint_classes(
                args["source_class"], args["sink_class"]
            )

        if tool_name == "get_fanin_cone_depth":
            return eng.get_fanin_cone_depth(
                args["output_signal"]
            )
        if tool_name == "count_outputs_by_logic_depth":
            return eng.count_outputs_by_logic_depth(
                args["operator"],
                int(args["threshold"])
            )
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
        if tool_name == "find_all_paths":
            return eng.find_all_paths(
                args["source"],
                args["sink"],
                args.get("inline_limit") or 5,
            )
        if tool_name == "find_register_to_register_paths":
            return eng.find_register_to_register_paths()
        if tool_name == "resolve_name_type":
            return eng.resolve_name_type(args["name"])
        if tool_name == "get_gate_info":
            return eng.get_gate_info(args["gate_name"])
        if tool_name == "get_net_fanout":
            return eng.get_fanout_report(args["net_name"])
        if tool_name == "get_reachable_gates_from_net":
            return eng.get_reachable_gates_from_net(args["net_name"])
        if tool_name == "get_reachable_gates_from_gate":
            return eng.get_reachable_gates_from_gate(args["gate_name"])
        if tool_name == "list_signals":
            return eng.list_signals()
        if tool_name == "get_gate_output_fanout":
            return eng.get_gate_fanout_report(args["gate_name"])
        if tool_name == "are_same_clock_domain":
            same = eng.are_same_clock_domain(args["dff1"], args["dff2"])
            return {"same_clock_domain": same}

        if tool_name == "insert_gate_before":
            new_inst = eng.insert_gate_before(
                args["target_instance"], args["gate_type"], args["extra_input"]
            )
            return {"new_instance": new_inst}

        if tool_name == "rename_gate":
            eng.rename_gate(args["old_name"], args["new_name"])
            return {"old_name": args["old_name"], "new_name": args["new_name"]}

        if tool_name == "rename_wire":
            eng.rename_wire(args["old_name"], args["new_name"])
            return {"old_name": args["old_name"], "new_name": args["new_name"]}

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

        if tool_name == "insert_dedicated_buffers_for_loads":
            n = eng.insert_dedicated_buffers_for_loads(args["net_name"])
            return {"net_name": args["net_name"], "buffers_inserted": n}

        if tool_name == "auto_insert_buffers":
            max_fanout = int(args.get("max_fanout", 4))
            nets = args.get("nets")
            processed = []
            per_net = {}
            total = 0

            if not nets:
                sigs = eng.list_signals()
                # Candidate nets: wires + gate_outputs + dff_q + dff_d
                nets = list(sigs.get("wires", [])) + list(sigs.get("gate_outputs", [])) + list(sigs.get("dff_q", [])) + list(sigs.get("dff_d", []))

            for net in nets:
                try:
                    fanout = eng.get_fanout(net)
                except Exception:
                    fanout = []
                cnt = len(fanout)
                if cnt > max_fanout:
                    inserted = eng.insert_buffers_for_fanout(net, max_fanout)
                    per_net[net] = {"before": cnt, "buffers_inserted": inserted}
                    total += inserted
                    processed.append(net)

            return {"nets_processed": len(processed), "buffers_inserted": total, "per_net": per_net}

        if tool_name == "balance_depth":
            n = eng.balance_depth(args["source"], args["sinks"])
            return {"buffers_inserted": n}

        if tool_name == "remove_dangling_gates":
            n = eng.remove_dangling_gates()
            return {"gates_removed": n}

        if tool_name == "collapse_inverter_pairs":
            return eng.collapse_inverter_pairs()

        if tool_name == "reduce_critical_path":
            return eng.reduce_critical_path(args.get("allowed_gates"))

        if tool_name == "replace_gate_type_in_cone":
            return eng.replace_gate_type_in_cone(
                args["source_type"],
                args["target_types"],
                args.get("output_signal"),
            )

        if tool_name == "remap_cone_with_gates":
            return eng.remap_cone_with_gates(
                args["output_signal"],
                args["allowed_gates"],
            )

        if tool_name == "remap_design_with_gates":
            return eng.remap_design_with_gates(args["allowed_gates"])

        if tool_name == "fraig_merge_equivalent_gates":
            return eng.fraig_merge_equivalent_gates()

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

        if tool_name == "check_signal_equivalence":
            equiv = eng.check_signal_equivalence(args["sig1"], args["sig2"])
            return {"equivalent": equiv}

        if tool_name == "check_signal_constant":
            return eng.check_signal_constant(args["signal_name"], args["value"])

        if tool_name == "check_design_equivalence":
            return eng.check_design_equivalence()

        raise ValueError(f"Unknown tool: {tool_name!r}")
