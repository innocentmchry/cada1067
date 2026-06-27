# #!/usr/bin/env python3
# """
# Run Yosys equivalence checking between golden (input) and generated (output)
# Verilog netlists laid out as:

#     <input_dir>/<test_name>/<test_name>.v
#     <output_dir>/<test_name>/<test_name>_out.v

# For each matching pair, this script:
#   1. Detects the top module name in each file (best-effort regex).
#   2. Builds a standard Yosys equiv_make / equiv_simple / equiv_induct /
#      equiv_status flow.
#   3. Runs it via `yosys -q -p <script>` and parses PASS/FAIL/ERROR.
#   4. Writes a per-test log and an overall summary.csv.

# Usage:
#     python3 run_equiv_check.py \\
#         --input-dir testcase_initials \\
#         --output-dir testcase_initials_output \\
#         --log-dir equiv_logs
# """

# import argparse
# import re
# import subprocess
# import sys
# from dataclasses import dataclass
# from pathlib import Path
# from typing import Optional


# @dataclass
# class EquivResult:
#     test_name: str
#     status: str  # PASS / FAIL / ERROR / SKIPPED
#     message: str
#     log_path: Path


# def find_module_name(verilog_path: Path) -> Optional[str]:
#     """Best-effort extraction of the first module name declared in a Verilog file."""
#     text = verilog_path.read_text(errors="ignore")
#     text = re.sub(r"//.*", "", text)
#     text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
#     match = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)", text)
#     return match.group(1) if match else None


# def build_yosys_script(gold_file: Path, gate_file: Path, gold_top: str, gate_top: str) -> str:
#     return f"""
# read_verilog -sv {gold_file}
# hierarchy -top {gold_top}
# proc; opt_clean
# rename {gold_top} gold
# design -stash gold

# read_verilog -sv {gate_file}
# hierarchy -top {gate_top}
# proc; opt_clean
# rename {gate_top} gate
# design -stash gate

# design -copy-from gold -as gold gold
# design -copy-from gate -as gate gate

# equiv_make gold gate equiv
# hierarchy -top equiv
# clean -purge
# equiv_simple
# equiv_induct
# equiv_status -assert
# """


# def run_equiv_check(test_name: str, gold_file: Path, gate_file: Path, log_dir: Path, timeout: int = 300) -> EquivResult:
#     gold_top = find_module_name(gold_file)
#     gate_top = find_module_name(gate_file)
#     log_dir.mkdir(parents=True, exist_ok=True)
#     log_path = log_dir / f"{test_name}_equiv.log"

#     if gold_top is None or gate_top is None:
#         log_path.write_text(f"Could not detect module name.\ngold_top={gold_top}\ngate_top={gate_top}\n")
#         return EquivResult(test_name, "SKIPPED", "Could not detect module name", log_path)

#     script = build_yosys_script(gold_file, gate_file, gold_top, gate_top)

#     try:
#         # Note: no "-q" here. With -q, Yosys suppresses normal log() output
#         # (including the "Equivalence successfully proven!" message from
#         # equiv_status), which made text-based PASS detection unreliable.
#         # We rely on the process exit code instead: equiv_status -assert
#         # causes Yosys to exit non-zero if any $equiv cell is unproven.
#         proc = subprocess.run(
#             ["yosys", "-p", script],
#             capture_output=True, text=True, timeout=timeout,
#         )
#     except subprocess.TimeoutExpired:
#         log_path.write_text(f"TIMEOUT after {timeout}s\nScript:\n{script}\n")
#         return EquivResult(test_name, "ERROR", f"Timed out after {timeout}s", log_path)
#     except FileNotFoundError:
#         sys.exit("yosys executable not found. Is Yosys installed and on PATH?")

#     output = proc.stdout + "\n" + proc.stderr
#     log_path.write_text(f"Yosys script:\n{script}\n\n---- Output ----\n{output}")

#     if proc.returncode == 0:
#         return EquivResult(test_name, "PASS", "Equivalence proven", log_path)

#     reason = "Equivalence check failed or assertion error"
#     for line in output.splitlines():
#         if "ERROR" in line or "Failed" in line or "failed" in line:
#             reason = line.strip()
#             break
#     return EquivResult(test_name, "FAIL", reason, log_path)


# def discover_pairs(input_dir: Path, output_dir: Path):
#     pairs = []
#     for test_dir in sorted(input_dir.iterdir()):
#         if not test_dir.is_dir():
#             continue
#         test_name = test_dir.name
#         gold_file = test_dir / f"{test_name}.v"
#         gate_file = output_dir / test_name / f"{test_name}_out.v"
#         if gold_file.exists() and gate_file.exists():
#             pairs.append((test_name, gold_file, gate_file))
#         else:
#             missing = [str(p) for p in (gold_file, gate_file) if not p.exists()]
#             print(f"[WARN] Skipping {test_name}: missing {', '.join(missing)}")
#     return pairs


# def main():
#     parser = argparse.ArgumentParser(description="Run Yosys equivalence checks between paired netlists.")
#     parser.add_argument("--input-dir", default="testcase_initials", help="Directory with golden netlists")
#     parser.add_argument("--output-dir", default="testcase_initials_output", help="Directory with generated/output netlists")
#     parser.add_argument("--log-dir", default="equiv_logs", help="Directory to store per-test yosys logs")
#     parser.add_argument("--timeout", type=int, default=1000, help="Per-test timeout in seconds")
#     args = parser.parse_args()

#     input_dir = Path(args.input_dir)
#     output_dir = Path(args.output_dir)
#     log_dir = Path(args.log_dir)

#     if not input_dir.is_dir():
#         sys.exit(f"Input directory not found: {input_dir}")
#     if not output_dir.is_dir():
#         sys.exit(f"Output directory not found: {output_dir}")

#     pairs = discover_pairs(input_dir, output_dir)
#     if not pairs:
#         sys.exit("No matching test pairs found.")

#     results = []
#     for test_name, gold_file, gate_file in pairs:
#         print(f"[RUN] {test_name}: {gold_file.name} vs {gate_file.name}")
#         result = run_equiv_check(test_name, gold_file, gate_file, log_dir, timeout=args.timeout)
#         results.append(result)
#         print(f"      -> {result.status}: {result.message}")

#     print("\n=== Summary ===")
#     for r in results:
#         print(f"{r.test_name:10s} {r.status:8s} {r.message}")

#     passed = sum(1 for r in results if r.status == "PASS")
#     failed = [r for r in results if r.status == "FAIL"]
#     errored = [r for r in results if r.status == "ERROR"]
#     skipped = [r for r in results if r.status == "SKIPPED"]
#     print(f"\nTotal: {len(results)}  Passed: {passed}  Failed: {len(failed)}  Errored: {len(errored)}  Skipped: {len(skipped)}")

#     log_dir.mkdir(parents=True, exist_ok=True)
#     summary_path = log_dir / "summary.csv"
#     with open(summary_path, "w") as f:
#         f.write("test_name,status,message,log_path\n")
#         for r in results:
#             msg = r.message.replace('"', "'")
#             f.write(f'{r.test_name},{r.status},"{msg}",{r.log_path}\n')
#     print(f"\nSummary CSV written to {summary_path}")

#     if failed or errored:
#         sys.exit(1)


# if __name__ == "__main__":
#     main()

#################################################################################

# #!/usr/bin/env python3
# """
# Run Yosys equivalence checking between golden (input) and generated (output)
# Verilog netlists laid out as:

#     <input_dir>/<test_name>/<test_name>.v
#     <output_dir>/<test_name>/<test_name>_out.v

# This checks combinational equivalence with DFFs treated as boundaries
# (PI->PO, PI->DFF.D, DFF.Q->PO, DFF.Q->DFF.D) rather than full multi-cycle
# sequential equivalence checking. This is correct as long as register
# structure/naming is unchanged between gold and gate (i.e. only
# combinational logic was optimized) -- see equiv_make/equiv_simple below.

# For each matching pair, this script:
#   1. Detects the top module name in each file (best-effort regex).
#   2. Builds a Yosys equiv_make / equiv_simple / equiv_status flow.
#   3. Runs it via `yosys -p <script>` and parses PASS/FAIL/ERROR.
#   4. Writes a per-test log and an overall summary.csv.

# Usage:
#     python3 run_equiv_check.py \\
#         --input-dir testcase_initials \\
#         --output-dir testcase_initials_output \\
#         --log-dir equiv_logs
# """

# import argparse
# import re
# import subprocess
# import sys
# from dataclasses import dataclass
# from pathlib import Path
# from typing import Optional


# @dataclass
# class EquivResult:
#     test_name: str
#     status: str  # PASS / FAIL / ERROR / SKIPPED
#     message: str
#     log_path: Path


# def find_module_name(verilog_path: Path) -> Optional[str]:
#     """Best-effort extraction of the first module name declared in a Verilog file."""
#     text = verilog_path.read_text(errors="ignore")
#     text = re.sub(r"//.*", "", text)
#     text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
#     match = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)", text)
#     return match.group(1) if match else None


# def build_yosys_script(gold_file: Path, gate_file: Path, gold_top: str, gate_top: str) -> str:
#     return f"""
# read_verilog -sv {gold_file}
# hierarchy -top {gold_top}
# proc; opt_clean
# rename {gold_top} gold
# design -stash gold

# read_verilog -sv {gate_file}
# hierarchy -top {gate_top}
# proc; opt_clean
# rename {gate_top} gate
# design -stash gate

# design -copy-from gold -as gold gold
# design -copy-from gate -as gate gate

# equiv_make gold gate equiv
# hierarchy -top equiv
# clean -purge
# equiv_simple
# equiv_status -assert
# """


# def run_equiv_check(test_name: str, gold_file: Path, gate_file: Path, log_dir: Path, timeout: int = 300) -> EquivResult:
#     gold_top = find_module_name(gold_file)
#     gate_top = find_module_name(gate_file)
#     log_dir.mkdir(parents=True, exist_ok=True)
#     log_path = log_dir / f"{test_name}_equiv.log"

#     if gold_top is None or gate_top is None:
#         log_path.write_text(f"Could not detect module name.\ngold_top={gold_top}\ngate_top={gate_top}\n")
#         return EquivResult(test_name, "SKIPPED", "Could not detect module name", log_path)

#     script = build_yosys_script(gold_file, gate_file, gold_top, gate_top)

#     try:
#         # Note: no "-q" here. With -q, Yosys suppresses normal log() output
#         # (including the "Equivalence successfully proven!" message from
#         # equiv_status), which made text-based PASS detection unreliable.
#         # We rely on the process exit code instead: equiv_status -assert
#         # causes Yosys to exit non-zero if any $equiv cell is unproven.
#         proc = subprocess.run(
#             ["yosys", "-p", script],
#             capture_output=True, text=True, timeout=timeout,
#         )
#     except subprocess.TimeoutExpired:
#         log_path.write_text(f"TIMEOUT after {timeout}s\nScript:\n{script}\n")
#         return EquivResult(test_name, "ERROR", f"Timed out after {timeout}s", log_path)
#     except FileNotFoundError:
#         sys.exit("yosys executable not found. Is Yosys installed and on PATH?")

#     output = proc.stdout + "\n" + proc.stderr
#     log_path.write_text(f"Yosys script:\n{script}\n\n---- Output ----\n{output}")

#     if proc.returncode == 0:
#         return EquivResult(test_name, "PASS", "Equivalence proven", log_path)

#     reason = "Equivalence check failed or assertion error"
#     for line in output.splitlines():
#         if "ERROR" in line or "Failed" in line or "failed" in line:
#             reason = line.strip()
#             break
#     return EquivResult(test_name, "FAIL", reason, log_path)


# def discover_pairs(input_dir: Path, output_dir: Path):
#     pairs = []
#     for test_dir in sorted(input_dir.iterdir()):
#         if not test_dir.is_dir():
#             continue
#         test_name = test_dir.name
#         gold_file = test_dir / f"{test_name}.v"
#         gate_file = output_dir / test_name / f"{test_name}_out.v"
#         if gold_file.exists() and gate_file.exists():
#             pairs.append((test_name, gold_file, gate_file))
#         else:
#             missing = [str(p) for p in (gold_file, gate_file) if not p.exists()]
#             print(f"[WARN] Skipping {test_name}: missing {', '.join(missing)}")
#     return pairs


# def main():
#     parser = argparse.ArgumentParser(description="Run Yosys equivalence checks between paired netlists.")
#     parser.add_argument("--input-dir", default="testcase_initials", help="Directory with golden netlists")
#     parser.add_argument("--output-dir", default="testcase_initials_output", help="Directory with generated/output netlists")
#     parser.add_argument("--log-dir", default="equiv_logs", help="Directory to store per-test yosys logs")
#     parser.add_argument("--timeout", type=int, default=1000, help="Per-test timeout in seconds")
#     args = parser.parse_args()

#     input_dir = Path(args.input_dir)
#     output_dir = Path(args.output_dir)
#     log_dir = Path(args.log_dir)

#     if not input_dir.is_dir():
#         sys.exit(f"Input directory not found: {input_dir}")
#     if not output_dir.is_dir():
#         sys.exit(f"Output directory not found: {output_dir}")

#     pairs = discover_pairs(input_dir, output_dir)
#     if not pairs:
#         sys.exit("No matching test pairs found.")

#     results = []
#     for test_name, gold_file, gate_file in pairs:
#         print(f"[RUN] {test_name}: {gold_file.name} vs {gate_file.name}")
#         result = run_equiv_check(test_name, gold_file, gate_file, log_dir, timeout=args.timeout)
#         results.append(result)
#         print(f"      -> {result.status}: {result.message}")

#     print("\n=== Summary ===")
#     for r in results:
#         print(f"{r.test_name:10s} {r.status:8s} {r.message}")

#     passed = sum(1 for r in results if r.status == "PASS")
#     failed = [r for r in results if r.status == "FAIL"]
#     errored = [r for r in results if r.status == "ERROR"]
#     skipped = [r for r in results if r.status == "SKIPPED"]
#     print(f"\nTotal: {len(results)}  Passed: {passed}  Failed: {len(failed)}  Errored: {len(errored)}  Skipped: {len(skipped)}")

#     log_dir.mkdir(parents=True, exist_ok=True)
#     summary_path = log_dir / "summary.csv"
#     with open(summary_path, "w") as f:
#         f.write("test_name,status,message,log_path\n")
#         for r in results:
#             msg = r.message.replace('"', "'")
#             f.write(f'{r.test_name},{r.status},"{msg}",{r.log_path}\n')
#     print(f"\nSummary CSV written to {summary_path}")

#     if failed or errored:
#         sys.exit(1)


# if __name__ == "__main__":
#     main()

###############################################################################

#!/usr/bin/env python3
"""
Run Yosys equivalence checking between golden (input) and generated (output)
Verilog netlists laid out as:

    <input_dir>/<test_name>/<test_name>.v
    <output_dir>/<test_name>/<test_name>_out.v

This checks combinational equivalence with DFFs treated as boundaries
(PI->PO, PI->DFF.D, DFF.Q->PO, DFF.Q->DFF.D) rather than full multi-cycle
sequential equivalence checking. This is correct as long as register
structure/naming is unchanged between gold and gate (i.e. only
combinational logic was optimized) -- see equiv_make/equiv_simple below.

For each matching pair, this script:
  1. Detects the top module name in each file (best-effort regex).
  2. Builds a Yosys equiv_make / equiv_simple / equiv_status flow, optionally
     reading a DFF definition file (--dff-lib, default ./dff/dff.v) alongside
     each netlist so DFF instances resolve to real logic instead of being
     treated as black boxes.
  3. Runs it via `yosys -p <script>` and parses PASS/FAIL/ERROR.
  4. Writes a per-test log and an overall summary.csv.

Usage:
    python3 run_equiv_check.py \\
        --input-dir testcase_initials \\
        --output-dir testcase_initials_output \\
        --log-dir equiv_logs \\
        --dff-lib ./dff/dff.v
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def workspace_temp_env() -> dict:
    """Keep Yosys internal temporary files under ./temp."""
    temp_root = (Path.cwd() / "temp").resolve()
    temp_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"TMPDIR": str(temp_root), "TEMP": str(temp_root), "TMP": str(temp_root)})
    return env


def yosys_binary() -> str:
    """Resolve Yosys from YOSYS_BIN or PATH."""
    configured = os.environ.get("YOSYS_BIN", "").strip()
    if configured:
        resolved = shutil.which(os.path.expanduser(configured))
        if resolved:
            return resolved
        raise RuntimeError(
            f"YOSYS_BIN does not point to an executable Yosys binary: {configured!r}"
        )
    resolved = shutil.which("yosys")
    if resolved:
        return resolved
    raise RuntimeError("Yosys executable not found. Set YOSYS_BIN or add yosys to PATH.")


@dataclass
class EquivResult:
    test_name: str
    status: str  # PASS / FAIL / ERROR / SKIPPED
    message: str
    log_path: Path


def find_module_name(verilog_path: Path) -> Optional[str]:
    """Best-effort extraction of the first module name declared in a Verilog file."""
    text = verilog_path.read_text(errors="ignore")
    text = re.sub(r"//.*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    match = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)", text)
    return match.group(1) if match else None


def build_yosys_script(gold_file: Path, gate_file: Path, gold_top: str, gate_top: str, dff_lib: Optional[Path] = None) -> str:
    # If a DFF definition file is given, read it alongside each netlist so any
    # DFF submodule instances resolve to real logic instead of being treated
    # as black boxes (which would make their D/Q boundaries unprovable).
    # `flatten` then inlines that submodule (and any other submodules) into
    # the top module, so each stashed design is a single flat module -- this
    # also avoids losing submodule definitions when copying across the
    # design -stash / -copy-from boundary below.
    # `async2sync` converts async set/reset FF cells (e.g. $dffsr/$adff) into
    # an equivalent synchronous representation. Without this, equiv_simple's
    # SAT engine errors out on any async FF with "No SAT model available".
    lib_include = f" {dff_lib}" if dff_lib else ""
    return f"""
read_verilog -sv{lib_include} {gold_file}
hierarchy -top {gold_top}
proc
async2sync
flatten
opt_clean
rename {gold_top} gold
design -stash gold

read_verilog -sv{lib_include} {gate_file}
hierarchy -top {gate_top}
proc
async2sync
flatten
opt_clean
rename {gate_top} gate
design -stash gate

design -copy-from gold -as gold gold
design -copy-from gate -as gate gate

equiv_make gold gate equiv
hierarchy -top equiv
clean -purge
equiv_simple
# Logic rewrites around register boundaries can leave equivalent DFF-Q cells
# unproven by the purely local/simple pass. Prove those sequential relations
# inductively before treating them as functional failures.
equiv_induct -seq 12
equiv_status -assert
"""


def run_equiv_check(test_name: str, gold_file: Path, gate_file: Path, log_dir: Path, timeout: int = 1000, dff_lib: Optional[Path] = None) -> EquivResult:
    gold_top = find_module_name(gold_file)
    gate_top = find_module_name(gate_file)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{test_name}_equiv.log"

    if gold_top is None or gate_top is None:
        log_path.write_text(f"Could not detect module name.\ngold_top={gold_top}\ngate_top={gate_top}\n")
        return EquivResult(test_name, "SKIPPED", "Could not detect module name", log_path)

    script = build_yosys_script(gold_file, gate_file, gold_top, gate_top, dff_lib=dff_lib)

    try:
        # Note: no "-q" here. With -q, Yosys suppresses normal log() output
        # (including the "Equivalence successfully proven!" message from
        # equiv_status), which made text-based PASS detection unreliable.
        # We rely on the process exit code instead: equiv_status -assert
        # causes Yosys to exit non-zero if any $equiv cell is unproven.
        proc = subprocess.run(
            [yosys_binary(), "-p", script],
            capture_output=True, text=True, timeout=timeout,
            env=workspace_temp_env(),
        )
    except subprocess.TimeoutExpired:
        log_path.write_text(f"TIMEOUT after {timeout}s\nScript:\n{script}\n")
        return EquivResult(test_name, "ERROR", f"Timed out after {timeout}s", log_path)
    except FileNotFoundError:
        sys.exit("yosys executable not found. Is Yosys installed and on PATH?")

    output = proc.stdout + "\n" + proc.stderr
    log_path.write_text(f"Yosys script:\n{script}\n\n---- Output ----\n{output}")

    if proc.returncode == 0:
        return EquivResult(test_name, "PASS", "Equivalence proven", log_path)

    reason = "Equivalence check failed or assertion error"
    for line in output.splitlines():
        if "ERROR" in line or "Failed" in line or "failed" in line:
            reason = line.strip()
            break
    return EquivResult(test_name, "FAIL", reason, log_path)


def discover_pairs(input_dir: Path, output_dir: Path):
    pairs = []
    for test_dir in sorted(input_dir.iterdir()):
        if not test_dir.is_dir():
            continue
        test_name = test_dir.name
        gold_file = test_dir / f"{test_name}.v"
        gate_file = output_dir / test_name / f"{test_name}_out.v"
        if gold_file.exists() and gate_file.exists():
            pairs.append((test_name, gold_file, gate_file))
        else:
            missing = [str(p) for p in (gold_file, gate_file) if not p.exists()]
            print(f"[WARN] Skipping {test_name}: missing {', '.join(missing)}")
    return pairs


def main():
    parser = argparse.ArgumentParser(description="Run Yosys equivalence checks between paired netlists.")
    parser.add_argument("--input-dir", default="testcase_initials", help="Directory with golden netlists")
    parser.add_argument("--output-dir", default="testcase_initials_output", help="Directory with generated/output netlists")
    parser.add_argument("--log-dir", default="equiv_logs", help="Directory to store per-test yosys logs")
    parser.add_argument("--timeout", type=int, default=1000, help="Per-test timeout in seconds")
    parser.add_argument("--dff-lib", default="./dff/dff.v",
                         help="Verilog file with DFF module definition(s) to read alongside each "
                              "netlist, so DFF instances resolve to real logic instead of being "
                              "treated as black boxes. Pass an empty string to disable.")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    log_dir = Path(args.log_dir)

    dff_lib: Optional[Path] = None
    if args.dff_lib:
        candidate = Path(args.dff_lib)
        if candidate.is_file():
            dff_lib = candidate
        else:
            print(f"[WARN] --dff-lib {candidate} not found, continuing without it "
                  f"(DFF submodules without a definition may be treated as black boxes)")

    if not input_dir.is_dir():
        sys.exit(f"Input directory not found: {input_dir}")
    if not output_dir.is_dir():
        sys.exit(f"Output directory not found: {output_dir}")

    pairs = discover_pairs(input_dir, output_dir)
    if not pairs:
        sys.exit("No matching test pairs found.")

    results = []
    for test_name, gold_file, gate_file in pairs:
        print(f"[RUN] {test_name}: {gold_file.name} vs {gate_file.name}")
        result = run_equiv_check(test_name, gold_file, gate_file, log_dir, timeout=args.timeout, dff_lib=dff_lib)
        results.append(result)
        print(f"      -> {result.status}: {result.message}")

    print("\n=== Summary ===")
    for r in results:
        print(f"{r.test_name:10s} {r.status:8s} {r.message}")

    passed = sum(1 for r in results if r.status == "PASS")
    failed = [r for r in results if r.status == "FAIL"]
    errored = [r for r in results if r.status == "ERROR"]
    skipped = [r for r in results if r.status == "SKIPPED"]
    print(f"\nTotal: {len(results)}  Passed: {passed}  Failed: {len(failed)}  Errored: {len(errored)}  Skipped: {len(skipped)}")

    log_dir.mkdir(parents=True, exist_ok=True)
    summary_path = log_dir / "summary.csv"
    with open(summary_path, "w") as f:
        f.write("test_name,status,message,log_path\n")
        for r in results:
            msg = r.message.replace('"', "'")
            f.write(f'{r.test_name},{r.status},"{msg}",{r.log_path}\n')
    print(f"\nSummary CSV written to {summary_path}")

    if failed or errored:
        sys.exit(1)


if __name__ == "__main__":
    main()
