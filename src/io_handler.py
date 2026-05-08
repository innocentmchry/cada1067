"""I/O handler — reads requests from stdin and writes tagged responses to stdout."""

from __future__ import annotations

import sys
from typing import IO, Optional

from .agent import EDAAgent


class IOHandler:
    """Manages the stdin → agent → stdout/log pipeline.

    Args:
        agent: An initialised EDAAgent instance.
    """

    def __init__(self, agent: EDAAgent) -> None:
        self._agent = agent
        self._agent.set_io_handler(self)
        self._log_file: Optional[IO[str]] = None
        self._response_counter: int = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_log_file(self, path: str) -> None:
        """Open (or create) a log file at *path* for appending.

        Any previously open log file is closed first.

        Args:
            path: File-system path of the log file.
        """
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
        self._log_file = open(path, "a", encoding="utf-8")

    def run(self) -> None:
        """Main loop: read lines from stdin, invoke the agent, write responses.

        Reads until EOF.  After printing each ``#END <id>`` block, stdout is
        flushed before blocking on the next line.
        """
        for raw_line in sys.stdin:
            line = raw_line.rstrip("\n")
            if not line:
                continue

            self._response_counter += 1
            resp_id = self._response_counter

            try:
                response_text = self._agent.process_request(line)
            except Exception as exc:
                response_text = f"[ERROR] {exc}"

            self._emit_response(resp_id, response_text)

        # Close log on EOF
        if self._log_file is not None:
            self._log_file.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_response(self, resp_id: int, text: str) -> None:
        """Write a response block to stdout (and optionally the log file).

        Format::

            #RESPONSE <id>
            <text>
            #END <id>

        Args:
            resp_id: Monotonically increasing response identifier.
            text:    The response body text.
        """
        block = f"#RESPONSE {resp_id}\n{text}\n#END {resp_id}\n"
        sys.stdout.write(block)
        sys.stdout.flush()

        if self._log_file is not None:
            self._log_file.write(block)
            self._log_file.flush()
