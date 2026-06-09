"""Developer-mode conversation logging — writes human-readable logs of LLM conversations.

Only used when ``developer_mode: true`` in config.yaml.  When the flag is
off, ``NoopLogger`` is used instead (zero runtime overhead).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LOGS_DIR: Optional[Path] = None


def get_logs_dir(base_dir: Optional[str | Path] = None) -> Path:
    """Create (if needed) and return the logs/ directory."""
    global _LOGS_DIR
    if _LOGS_DIR is not None:
        return _LOGS_DIR

    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent

    _LOGS_DIR = Path(base_dir) / "logs"
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return _LOGS_DIR


def sanitize_label(label: str) -> str:
    """Return a filesystem-safe version of *label*."""
    safe = re.sub(r"[^a-zA-Z0-9_\-.]", "_", label)
    return safe.strip("_") or "unknown"


def extract_case_name(user_message: str) -> str:
    """Try to extract a testcase name from a user message.

    Looks for patterns like:  case name is 'test8'  or  case name is "test35"
    Falls back to an empty string.
    """
    m = re.search(
        r"case\s+name\s+(?:is\s+)?['\"]([^'\"]+)['\"]",
        user_message,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    return ""


def format_netlist(netlist: Any) -> str:
    """Print each field of the Netlist dataclass as plain text.

    Args:
        netlist: A Netlist instance (from netlist_parser).

    Returns:
        Multi-line string showing all fields.
    """
    lines: List[str] = []

    lines.append(f"module_name    : {getattr(netlist, 'module_name', '?')}")

    pi = getattr(netlist, "primary_inputs", [])
    lines.append(f"primary_inputs : {pi}")

    po = getattr(netlist, "primary_outputs", [])
    lines.append(f"primary_outputs: {po}")

    wires = getattr(netlist, "wires", {})
    lines.append(f"wires          : {len(wires)} entries")
    for name, wi in sorted(wires.items()):
        lines.append(f"  {name}: WireInfo(name={wi.name}, width={wi.width}, is_bus={wi.is_bus}, msb={wi.msb}, lsb={wi.lsb})")

    nodes = getattr(netlist, "nodes", {})
    lines.append(f"nodes          : {len(nodes)} entries")
    for name, g in sorted(nodes.items()):
        lines.append(f"  {name}: GateNode(gate_type={g.gate_type}, inputs={g.inputs}, output={g.output})")

    dffs = getattr(netlist, "dffs", {})
    lines.append(f"dffs           : {len(dffs)} entries")
    for name, d in sorted(dffs.items()):
        lines.append(f"  {name}: DFFNode(ck={d.ck}, rn={d.rn}, sn={d.sn}, d={d.d}, q={d.q})")

    return "\n".join(lines)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Real conversation logger (used in developer mode)
# ---------------------------------------------------------------------------

class ConversationLogger:
    """Writes a human-readable log of a single stdin→LLM conversation.

    One instance per request.  Use as a context manager::

        with ConversationLogger(idx, logs_dir, model, label="test8") as log:
            log.log_user_input(...)
            ...
    """

    def __init__(
        self,
        request_index: int,
        logs_dir: Path,
        model: str,
        label: str = "",
    ) -> None:
        self._index = request_index
        self._model = model
        self._start_time = time.monotonic()

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_label = sanitize_label(label) if label else ""
        if safe_label:
            filename = logs_dir / f"{safe_label}_request_{request_index:04d}_{ts}.log"
        else:
            filename = logs_dir / f"request_{request_index:04d}_{ts}.log"
        self._fh: TextIO = open(filename, "w", encoding="utf-8")

        self._fh.write("═" * 72 + "\n")
        self._fh.write(f"Request #{request_index} — {ts} UTC\n")
        if label:
            self._fh.write(f"Case: {label}\n")
        self._fh.write(f"Model: {model}\n")
        self._fh.write("═" * 72 + "\n\n")

    # ------------------------------------------------------------------
    # Public logging methods
    # ------------------------------------------------------------------

    def log_user_input(self, text: str) -> None:
        """Record the original user message."""
        self._fh.write("┌─ USER INPUT ─────────────────────────────\n")
        self._fh.write(f"{text.rstrip()}\n")
        self._fh.write("└──────────────────────────────────────────\n\n")

    def log_llm_prompt(self, messages: List[Dict[str, Any]], turn: int) -> None:
        """Log the full prompt sent to the LLM for a given turn."""
        self._fh.write(f"┌─ LLM PROMPT (turn {turn}) ───────────────\n")
        for i, m in enumerate(messages):
            role = m.get("role", "?")
            self._fh.write(f"│ [{i}] role: {role}\n")
            if m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    self._fh.write(f"│     tool_call: {fn.get('name', '?')}\n")
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                        args_str = json.dumps(args, indent=6)
                    except (json.JSONDecodeError, TypeError):
                        args_str = str(fn.get("arguments", "{}"))
                    self._fh.write("│     arguments:\n")
                    for line in args_str.splitlines():
                        self._fh.write(f"│       {line}\n")
            else:
                content = m.get("content") or ""
                try:
                    parsed = json.loads(content)
                    content_str = json.dumps(parsed, indent=6)
                    self._fh.write("│     content (JSON):\n")
                    for line in content_str.splitlines():
                        self._fh.write(f"│       {line}\n")
                except (json.JSONDecodeError, TypeError):
                    for line in content.splitlines():
                        self._fh.write(f"│     {line}\n")
        self._fh.write("└──────────────────────────────────────────\n\n")

    def log_llm_response(self, response: Any, turn: int, elapsed: float = 0.0) -> None:
        """Log the LLM's response — raw JSON first, then summary.
        
        Args:
            response: The OpenAI API response object.
            turn: Turn number within this request.
            elapsed: Seconds this LLM call took (wall-clock).
        """
        choice = response.choices[0]
        msg = choice.message
        finish = choice.finish_reason or "?"

        self._fh.write(f"┌─ LLM RESPONSE (turn {turn}) ─────────────\n")
        self._fh.write("│ RAW JSON:\n")
        try:
            raw_dict = response.model_dump(mode="json")
            raw_str = json.dumps(raw_dict, indent=6, ensure_ascii=False)
        except (AttributeError, TypeError):
            raw_str = str(response)
        for line in raw_str.splitlines():
            self._fh.write(f"│   {line}\n")

        self._fh.write("│ ─────────────────────────────────\n")
        self._fh.write("│ SUMMARY:\n")
        self._fh.write(f"│   finish_reason: {finish}\n")

        if msg.content:
            self._fh.write(f"│   text: {json.dumps(msg.content)}\n")

        if msg.tool_calls:
            self._fh.write(f"│   tool_calls: {len(msg.tool_calls)}\n")
            for tc in msg.tool_calls:
                fn = tc.function
                self._fh.write(f"│     → {fn.name}\n")
                self._fh.write(f"│       args: {fn.arguments}\n")

        usage = getattr(response, "usage", None)
        if usage:
            self._fh.write(
                f"│   tokens: prompt={usage.prompt_tokens}  "
                f"completion={usage.completion_tokens}  "
                f"total={usage.total_tokens}\n"
            )

        if elapsed > 0:
            self._fh.write(f"│   elapsed: {elapsed:.2f}s\n")

        self._fh.write("└──────────────────────────────────────────\n\n")

    def log_tool_result(self, tool_name: str, result_str: str) -> None:
        """Log the result of a dispatched tool call."""
        self._fh.write(f"  ── TOOL RESULT: {tool_name} ──\n")
        try:
            parsed = json.loads(result_str)
            result_str = json.dumps(parsed, indent=4)
        except (json.JSONDecodeError, TypeError):
            pass
        for line in result_str.splitlines():
            self._fh.write(f"  {line}\n")
        self._fh.write("  ──────────────────────────────\n\n")

    def log_netlist(self, netlist: Any) -> None:
        """Log the full netlist structure after a read_design call."""
        self._fh.write("  ── NETLIST LOADED ──\n")
        summary = format_netlist(netlist)
        for line in summary.splitlines():
            self._fh.write(f"  {line}\n")
        self._fh.write("  ────────────────────\n\n")

    def log_final_response(self, text: str) -> None:
        """Log the final text response returned to the user."""
        self._fh.write("┌─ FINAL RESPONSE ─────────────────────────\n")
        for line in text.splitlines():
            self._fh.write(f"│ {line}\n")
        self._fh.write("└──────────────────────────────────────────\n\n")

    def close(self) -> None:
        """Close the log file."""
        if not self._fh.closed:
            elapsed = time.monotonic() - self._start_time
            self._fh.write("═" * 72 + "\n")
            self._fh.write(f"END OF REQUEST — total time: {elapsed:.2f}s\n")
            self._fh.write("═" * 72 + "\n")
            self._fh.close()

    def __enter__(self) -> "ConversationLogger":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# No-op logger (production mode — zero overhead)
# ---------------------------------------------------------------------------

class NoopLogger:
    """Drop-in replacement for ConversationLogger that does nothing."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def log_user_input(self, text: str) -> None:
        pass

    def log_llm_prompt(self, messages: List[Dict[str, Any]], turn: int) -> None:
        pass

    def log_llm_response(self, response: Any, turn: int, elapsed: float = 0.0) -> None:
        pass

    def log_tool_result(self, tool_name: str, result_str: str) -> None:
        pass

    def log_netlist(self, netlist: Any) -> None:
        pass

    def log_final_response(self, text: str) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "NoopLogger":
        return self

    def __exit__(self, *args: Any) -> None:
        pass
