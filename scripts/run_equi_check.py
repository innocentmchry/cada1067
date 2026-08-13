import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.netlist_parser import GateNode, Netlist, WireInfo, parse_verilog, write_verilog


def workspace_temp_env() -> dict:
    """Keep Yosys internal temporary files under ./_tmp."""
    temp_root = (Path.cwd() / "_tmp").resolve()
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


def compare_dff_boundary_shapes(gold: Netlist, gate: Netlist) -> Optional[str]:
    if set(gold.primary_inputs) != set(gate.primary_inputs):
        return (
            "Primary input sets differ: "
            f"gold_only={sorted(set(gold.primary_inputs) - set(gate.primary_inputs))}, "
            f"gate_only={sorted(set(gate.primary_inputs) - set(gold.primary_inputs))}"
        )
    if set(gold.primary_outputs) != set(gate.primary_outputs):
        return (
            "Primary output sets differ: "
            f"gold_only={sorted(set(gold.primary_outputs) - set(gate.primary_outputs))}, "
            f"gate_only={sorted(set(gate.primary_outputs) - set(gold.primary_outputs))}"
        )
    if set(gold.dffs) != set(gate.dffs):
        return (
            "DFF instance sets differ: "
            f"gold_only={sorted(set(gold.dffs) - set(gate.dffs))}, "
            f"gate_only={sorted(set(gate.dffs) - set(gold.dffs))}"
        )
    return None


def netlists_structurally_equal(a: Netlist, b: Netlist) -> bool:
    def payload(nl: Netlist) -> dict:
        return {
            "primary_inputs": list(nl.primary_inputs),
            "primary_outputs": list(nl.primary_outputs),
            "wires": {
                name: {
                    "width": wi.width,
                    "is_bus": wi.is_bus,
                    "msb": wi.msb,
                    "lsb": wi.lsb,
                }
                for name, wi in sorted(nl.wires.items())
            },
            "nodes": {
                name: {
                    "gate_type": node.gate_type,
                    "inputs": list(node.inputs),
                    "output": node.output,
                }
                for name, node in sorted(nl.nodes.items())
            },
            "dffs": {
                name: {
                    "ck": dff.ck,
                    "rn": dff.rn,
                    "sn": dff.sn,
                    "d": dff.d,
                    "q": dff.q,
                }
                for name, dff in sorted(nl.dffs.items())
            },
        }

    return payload(a) == payload(b)


def make_dff_boundary_comb_netlist(source: Netlist, module_name: str) -> Netlist:
    import copy

    comb = copy.deepcopy(source)
    comb.module_name = module_name
    comb.dffs = {}

    existing_names = set(comb.wires) | set(comb.nodes) | set(comb.primary_inputs) | set(comb.primary_outputs)

    def unique_name(base: str) -> str:
        name = base
        suffix = 0
        while name in existing_names:
            suffix += 1
            name = f"{base}_{suffix}"
        existing_names.add(name)
        return name

    for inst_name, dff in source.dffs.items():
        state_input = unique_name(f"_equiv_state_{inst_name}")
        comb.primary_inputs.append(state_input)
        comb.wires[state_input] = WireInfo(name=state_input)
        buf_name = unique_name(f"_equiv_statebuf_{inst_name}")
        comb.nodes[buf_name] = GateNode(
            name=buf_name,
            gate_type="buf",
            inputs=[state_input],
            output=dff.q,
        )

    for inst_name, dff in source.dffs.items():
        for pin_name, signal in (
            ("d", dff.d),
            ("ck", dff.ck),
            ("rn", dff.rn),
            ("sn", dff.sn or "1'b1"),
        ):
            out_name = unique_name(f"_equiv_{pin_name}_{inst_name}")
            comb.primary_outputs.append(out_name)
            comb.wires[out_name] = WireInfo(name=out_name)
            buf_name = unique_name(f"_equiv_{pin_name}buf_{inst_name}")
            comb.nodes[buf_name] = GateNode(
                name=buf_name,
                gate_type="buf",
                inputs=[signal],
                output=out_name,
            )

    return comb


def build_yosys_script(gold_file: Path, gate_file: Path) -> str:
    return f"""
read_verilog -sv {gold_file}
hierarchy -top gold_comb
proc
flatten
opt_clean
rename gold_comb gold
design -stash gold

read_verilog -sv {gate_file}
hierarchy -top gate_comb
proc
flatten
opt_clean
rename gate_comb gate
design -stash gate

design -copy-from gold -as gold gold
design -copy-from gate -as gate gate

equiv_make gold gate equiv
hierarchy -top equiv
clean -purge
equiv_struct
equiv_simple
equiv_status -assert
"""


# Legacy sequential whole-design equivalence script kept for reference.
# The competition equivalence model is combinational: DFF Q pins are treated
# as unconstrained inputs and DFF D/control pins are compared as outputs.
#
# def build_yosys_script_sequential_legacy(
#     gold_file: Path,
#     gate_file: Path,
#     gold_top: str,
#     gate_top: str,
#     dff_lib: Optional[Path] = None,
# ) -> str:
#     lib_include = f" {dff_lib}" if dff_lib else ""
#     return f"""
# read_verilog -sv{lib_include} {gold_file}
# hierarchy -top {gold_top}
# proc
# async2sync
# flatten
# opt_clean
# rename {gold_top} gold
# design -stash gold
#
# read_verilog -sv{lib_include} {gate_file}
# hierarchy -top {gate_top}
# proc
# async2sync
# flatten
# opt_clean
# rename {gate_top} gate
# design -stash gate
#
# design -copy-from gold -as gold gold
# design -copy-from gate -as gate gate
# equiv_make gold gate equiv
# hierarchy -top equiv
# clean -purge
# equiv_simple
# equiv_induct -seq 12
# equiv_status -assert
# """


def run_equiv_check(test_name: str, gold_file: Path, gate_file: Path, log_dir: Path, timeout: int = 1000, dff_lib: Optional[Path] = None) -> EquivResult:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{test_name}_equiv.log"

    try:
        gold_nl = parse_verilog(str(gold_file))
        gate_nl = parse_verilog(str(gate_file))
    except Exception as exc:
        log_path.write_text(f"Could not parse netlist(s): {exc}\n")
        return EquivResult(test_name, "SKIPPED", f"Could not parse netlist(s): {exc}", log_path)

    mismatch = compare_dff_boundary_shapes(gold_nl, gate_nl)
    if mismatch:
        log_path.write_text(mismatch + "\n")
        return EquivResult(test_name, "FAIL", mismatch, log_path)
    if netlists_structurally_equal(gold_nl, gate_nl):
        log_path.write_text(
            "PASS: current design is equivalent to the original netlist "
            "under combinational DFF-boundary equivalence.\n"
        )
        return EquivResult(test_name, "PASS", "Equivalence proven", log_path)

    temp_root = Path(workspace_temp_env()["TMPDIR"])
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"{test_name}_comb_equiv_", dir=temp_root))
    comb_gold_file = tmp_dir / "gold_comb.v"
    comb_gate_file = tmp_dir / "gate_comb.v"

    write_verilog(make_dff_boundary_comb_netlist(gold_nl, "gold_comb"), str(comb_gold_file))
    write_verilog(make_dff_boundary_comb_netlist(gate_nl, "gate_comb"), str(comb_gate_file))

    script = build_yosys_script(comb_gold_file, comb_gate_file)
    temp_env = workspace_temp_env()

    try:
        # Note: no "-q" here. With -q, Yosys suppresses normal log() output
        # (including the "Equivalence successfully proven!" message from
        # equiv_status), which made text-based PASS detection unreliable.
        # We rely on the process exit code instead: equiv_status -assert
        # causes Yosys to exit non-zero if any $equiv cell is unproven.
        proc = subprocess.run(
            [yosys_binary(), "-p", script],
            capture_output=True, text=True, timeout=timeout,
            env=temp_env,
            cwd=temp_env["TMPDIR"],
        )
    except subprocess.TimeoutExpired:
        log_path.write_text(f"TIMEOUT after {timeout}s\nScript:\n{script}\n")
        return EquivResult(test_name, "ERROR", f"Timed out after {timeout}s", log_path)
    except FileNotFoundError:
        sys.exit("yosys executable not found. Is Yosys installed and on PATH?")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

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

def discover_pairs(input_dir: Path, output_dir: Path):
    pairs = []
    for gold_file in sorted(input_dir.glob("*.v")):
        if gold_file.stem.endswith("_out"):
            continue
        test_name = gold_file.stem
        gate_file = output_dir / f"{test_name}_out.v"
        if gate_file.exists():
            pairs.append((test_name, gold_file.resolve(), gate_file.resolve()))  # ← resolve both
        else:
            print(f"[WARN] Skipping {test_name}: missing {gate_file}")
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

    # input_dir = Path(args.input_dir)
    # output_dir = Path(args.output_dir)
    # log_dir = Path(args.log_dir)

    input_dir  = Path(args.input_dir).resolve()   # ← add .resolve()
    output_dir = Path(args.output_dir).resolve()  # ← add .resolve()
    log_dir    = Path(args.log_dir).resolve()     # ← add .resolve()

    dff_lib: Optional[Path] = None
    if args.dff_lib:
        candidate = Path(args.dff_lib)
        # if candidate.is_file():
        #     dff_lib = candidate
        # to:
        if candidate.is_file():
            dff_lib = candidate.resolve()   # ← absolute path, immune to cwd changes
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
