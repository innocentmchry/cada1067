#!/usr/bin/env python3
"""Count Verilog primitive gate instances in testcase subdirectories."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


PRIMITIVE_GATES = (
    "and", "nand", "or", "nor", "xor", "xnor", "buf", "not",
    "bufif0", "bufif1", "notif0", "notif1",
    "nmos", "pmos", "cmos", "rnmos", "rpmos", "rcmos",
    "tran", "rtran", "tranif0", "tranif1", "rtranif0", "rtranif1",
    "pullup", "pulldown",
)

COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
GATE_RE = re.compile(
    rf"\b(?:{'|'.join(PRIMITIVE_GATES)})\s+"
    r"(?:\\\S+|[A-Za-z_][A-Za-z0-9_$]*)\s*(?:\[[^\]]+\]\s*)?\(",
    re.IGNORECASE,
)


def natural_key(path: Path) -> list[object]:
    """Sort test2 before test10."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", path.name)]


def count_gates(verilog_file: Path) -> int:
    """Return the number of built-in primitive gate instances in a file."""
    source = verilog_file.read_text(encoding="utf-8", errors="replace")
    source = COMMENT_RE.sub("", source)
    return sum(1 for _ in GATE_RE.finditer(source))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Count primitive gates in each testcase subdirectory."
    )
    parser.add_argument(
        "testcase_root",
        type=Path,
        help="directory containing test01, test02, ... subdirectories",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.testcase_root.expanduser()
    if not root.is_dir():
        print(f"error: testcase directory does not exist: {root}", file=sys.stderr)
        return 2

    test_dirs = sorted((path for path in root.iterdir() if path.is_dir()), key=natural_key)
    rows: list[tuple[str, str, int]] = []
    had_error = False

    for test_dir in test_dirs:
        verilog_files = sorted(test_dir.glob("*.v"), key=natural_key)
        if len(verilog_files) != 1:
            print(
                f"warning: {test_dir} contains {len(verilog_files)} .v files; expected 1",
                file=sys.stderr,
            )
            had_error = True
            continue
        verilog_file = verilog_files[0]
        rows.append((test_dir.name, verilog_file.name, count_gates(verilog_file)))

    if not rows:
        print(f"error: no testcase directories with one .v file found in {root}", file=sys.stderr)
        return 1

    test_width = max(len("testcase"), *(len(row[0]) for row in rows))
    file_width = max(len("verilog file"), *(len(row[1]) for row in rows))
    print(f"{'testcase':<{test_width}}  {'verilog file':<{file_width}}  gates")
    print(f"{'-' * test_width}  {'-' * file_width}  {'-' * 10}")
    for test_name, file_name, gate_count in rows:
        print(f"{test_name:<{test_width}}  {file_name:<{file_width}}  {gate_count}")
    print(f"\nTestcases: {len(rows)}   Total gates: {sum(row[2] for row in rows)}")
    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
