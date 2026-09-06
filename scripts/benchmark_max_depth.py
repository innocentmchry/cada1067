"""Standalone benchmark script for get_max_depth optimization.

Discovers and benchmarks all testcases in the suite that call get_max_depth:
- Compares old reference implementation vs. new optimized engine implementation.
- Verifies path validity, start/end nodes, and path length.
- Tests unreachable pairs and sequential DFF cuts.
- Verifies cache invalidation upon netlist modification.
- Saves formatted report to output/summary/benchmark_depth_results.md and .csv

DO NOT import this into src/eda_engine.py. This script is strictly standalone.
"""

from collections import deque
import argparse
import glob
import os
import re
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

# Ensure workspace root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.eda_engine import EDAEngine  # noqa: E402


# ---------------------------------------------------------------------------
# Preserved Reference Implementation (Verbatim from original engine)
# ---------------------------------------------------------------------------

def reference_get_max_depth(
    engine: EDAEngine, source: str, sink: str
) -> Tuple[int, List[str]]:
    """Original reference get_max_depth implementation for baseline comparison."""
    engine._require_netlist()
    engine._resolve_signal(source)
    engine._resolve_signal(sink)

    nl = engine.netlist
    assert nl is not None

    def unindexed_forward_successors(sig: str) -> List[str]:
        res: List[str] = []
        for node in nl.nodes.values():
            if sig in node.inputs:
                res.append(node.output)
        return res

    # First collect all reachable signals from source (forward BFS)
    reachable: Set[str] = set()
    queue: deque[str] = deque([source])
    while queue:
        cur = queue.popleft()
        if cur in reachable:
            continue
        reachable.add(cur)
        for nxt in unindexed_forward_successors(cur):
            if nxt not in reachable:
                queue.append(nxt)

    if sink not in reachable:
        return (-1, [])

    # DFS with explicit stack for longest path tracking
    dist: Dict[str, int] = {source: 0}
    parent: Dict[str, Optional[str]] = {source: None}

    # Topological sort within reachable
    visited: Set[str] = set()
    topo_order: List[str] = []

    def dfs_topo(node: str) -> None:
        visited.add(node)
        for nxt in unindexed_forward_successors(node):
            if nxt in reachable and nxt not in visited:
                dfs_topo(nxt)
        topo_order.append(node)

    dfs_topo(source)
    topo_order.reverse()

    for sig in topo_order:
        for nxt in unindexed_forward_successors(sig):
            if nxt not in reachable:
                continue
            new_dist = dist.get(sig, -1) + 1
            if new_dist > dist.get(nxt, -1):
                dist[nxt] = new_dist
                parent[nxt] = sig

    if sink not in dist:
        return (-1, [])

    path: List[str] = []
    cur: Optional[str] = sink
    while cur is not None:
        path.append(cur)
        cur = parent.get(cur)
    path.reverse()

    return (dist[sink], path)


# ---------------------------------------------------------------------------
# Path Validity Checker
# ---------------------------------------------------------------------------

def validate_path(
    engine: EDAEngine, source: str, sink: str, depth: int, path: List[str]
) -> Tuple[bool, str]:
    """Validate that path connects source to sink with valid combinational gate arcs."""
    if depth == -1:
        if path == []:
            return True, "Valid unreachable result"
        return False, "Depth is -1 but path is non-empty"

    if len(path) != depth + 1:
        return False, f"Path length {len(path)} != depth + 1 ({depth + 1})"

    if path[0] != source:
        return False, f"Path start {path[0]} != source {source}"

    if path[-1] != sink:
        return False, f"Path end {path[-1]} != sink {sink}"

    nl = engine.netlist
    out2node = {node.output: node for node in nl.nodes.values()}

    for i in range(len(path) - 1):
        u = path[i]
        v = path[i + 1]
        gate = out2node.get(v)
        if gate is None:
            if any(dff.q == v for dff in nl.dffs.values()):
                return False, f"Arc {u} -> {v} passes through a DFF"
            return False, f"Node {v} has no combinational driver"
        if u not in gate.inputs:
            return False, f"Arc {u} -> {v} is invalid; {u} is not an input to {gate.name}"

    return True, "Valid combinational path"


# ---------------------------------------------------------------------------
# Query Discovery from Testcase Prompts
# ---------------------------------------------------------------------------

def discover_relevant_queries(
    testcase_dir: str = "testcase", filter_tests: Optional[List[str]] = None
) -> List[Tuple[str, str, str, str, str]]:
    """Automatically find all testcases that ask for logic depth between signals."""
    queries: List[Tuple[str, str, str, str, str]] = []
    tc_dirs = sorted(glob.glob(os.path.join(testcase_dir, "test*")))

    pattern = re.compile(
        r"(?:maximum logic depth|longest combinational path depth) from (?:input )?(\S+) to (?:output )?(\S+)",
        re.IGNORECASE,
    )

    for d in tc_dirs:
        tc = os.path.basename(d)
        if filter_tests and tc not in filter_tests:
            continue
        v_path = os.path.join(d, f"{tc}.v")
        prompt_p = os.path.join(d, "prompt.txt")
        if not os.path.isfile(v_path) or not os.path.isfile(prompt_p):
            continue

        with open(prompt_p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        for idx, line in enumerate(lines, 1):
            m = pattern.search(line)
            if m:
                src = m.group(1).rstrip(".,;:")
                snk = m.group(2).rstrip(".,;:")
                queries.append((tc, v_path, src, snk, f"Turn {idx}"))

    return queries


# ---------------------------------------------------------------------------
# Benchmark Runner
# ---------------------------------------------------------------------------

def run_benchmarks(
    queries: List[Tuple[str, str, str, str, str]],
    run_old: bool = True,
    output_dir: str = "output/summary",
):
    os.makedirs(output_dir, exist_ok=True)
    results = []
    loaded_engines: Dict[str, EDAEngine] = {}

    print("\n" + "=" * 115, flush=True)
    print("                      GET_MAX_DEPTH OPTIMIZATION BENCHMARK REPORT", flush=True)
    print("=" * 115, flush=True)
    header = (
        f"{'Query':<32} | {'Gates':<7} | {'Old Time':<10} | {'New Time':<10} | "
        f"{'Speedup':<9} | {'Old Depth':<9} | {'New Depth':<9} | {'Correct?'}"
    )
    print(header, flush=True)
    print("-" * 115, flush=True)

    for idx, (tc_name, v_path, src, snk, desc) in enumerate(queries, 1):
        query_label = f"{tc_name} ({src}->{snk})"

        if tc_name not in loaded_engines:
            eng = EDAEngine()
            eng.load(v_path)
            loaded_engines[tc_name] = eng
        else:
            eng = loaded_engines[tc_name]

        gate_count = len(eng.netlist.nodes)

        # 1. Run Old Reference Implementation
        if run_old:
            sys.stdout.write(f"[{idx}/{len(queries)}] {query_label:<30} (running Old... ")
            sys.stdout.flush()
            t0 = time.perf_counter()
            old_depth, old_path = reference_get_max_depth(eng, src, snk)
            old_time = time.perf_counter() - t0
            sys.stdout.write(f"{old_time:.3f}s | running New... ")
            sys.stdout.flush()
        else:
            sys.stdout.write(f"[{idx}/{len(queries)}] {query_label:<30} (running New... ")
            sys.stdout.flush()
            old_depth, old_path = -1, []
            old_time = 0.0

        # 2. Run New Engine Implementation
        t2 = time.perf_counter()
        new_depth, new_path = eng.get_max_depth(src, snk)
        new_time = time.perf_counter() - t2

        speedup_val = (old_time / new_time) if (new_time > 0 and old_time > 0) else 0.0
        speedup_str = f"{speedup_val:8.1f}x" if speedup_val > 0 else "N/A"
        old_time_str = f"{old_time:7.4f}s" if run_old else "SKIPPED"
        new_time_str = f"{new_time:7.4f}s"

        valid, reason = validate_path(eng, src, snk, new_depth, new_path)
        depths_match = (old_depth == new_depth) if run_old else True
        is_correct = "YES" if (valid and depths_match) else f"NO ({reason})"

        sys.stdout.write(f"{new_time:.4f}s -> {speedup_str.strip()}) [{is_correct}]\n")
        sys.stdout.flush()

        results.append({
            "query": query_label,
            "gates": gate_count,
            "old_time": old_time_str,
            "new_time": new_time_str,
            "speedup": speedup_str,
            "old_depth": str(old_depth) if run_old else "N/A",
            "new_depth": str(new_depth),
            "correct": is_correct,
        })

    print("-" * 115, flush=True)
    print(header, flush=True)
    print("-" * 115, flush=True)
    for r in results:
        print(
            f"{r['query']:<32} | {r['gates']:<7} | {r['old_time']:<10} | {r['new_time']:<10} | "
            f"{r['speedup']:<9} | {r['old_depth']:<9} | {r['new_depth']:<9} | {r['correct']}",
            flush=True,
        )
    print("=" * 115, flush=True)

    # Save to CSV
    csv_path = os.path.join(output_dir, "benchmark_depth_results.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("Query,Gates,Old_Time,New_Time,Speedup,Old_Depth,New_Depth,Correct\n")
        for r in results:
            f.write(f"{r['query']},{r['gates']},{r['old_time']},{r['new_time']},{r['speedup']},{r['old_depth']},{r['new_depth']},{r['correct']}\n")
    print(f"\n[SAVED] Benchmark table saved to: {csv_path}", flush=True)


def verify_edge_cases():
    print("\n" + "=" * 80, flush=True)
    print("                     VERIFYING EDGE CASES & BOUNDARIES", flush=True)
    print("=" * 80, flush=True)

    # 1. Unreachable Pair in test12
    eng12 = EDAEngine()
    eng12.load("testcase/test12/test12.v")
    d, p = eng12.get_max_depth("n24[2]", "n26[0]")
    status1 = "PASS" if (d == -1 and p == []) else "FAIL"
    print(f"1. Unreachable Pair (test12 n24[2] -> n26[0]): depth={d}, path={p} [{status1}]", flush=True)

    # 2. Sequential DFF Boundary Cut in test53
    eng53 = EDAEngine()
    eng53.load("testcase/test53/test53.v")
    dff = next(iter(eng53.netlist.dffs.values()))
    d_dff, p_dff = eng53.get_max_depth(dff.d, dff.q)
    status2 = "PASS" if (d_dff == -1 and p_dff == []) else "FAIL"
    print(f"2. Sequential DFF Cut (test53 {dff.name}: D={dff.d} -> Q={dff.q}): depth={d_dff}, path={p_dff} [{status2}]", flush=True)

    # 3. Cache Invalidation on Mutation in test15
    eng15 = EDAEngine()
    eng15.load("testcase/test15/test15.v")
    d_before, _ = eng15.get_max_depth("n0[1]", "n4")
    eng15.insert_dedicated_buffers_for_loads("n0[1]")
    d_after, _ = eng15.get_max_depth("n0[1]", "n4")
    status3 = "PASS" if (d_after == d_before + 1) else "FAIL"
    print(f"3. Cache Invalidation (test15 buffer on n0[1]): before={d_before}, after={d_after} [{status3}]", flush=True)

    print("=" * 80, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Benchmark get_max_depth on relevant testcases.")
    parser.add_argument(
        "--tests",
        nargs="+",
        help="Specific testcases to benchmark (e.g. --tests test10 test12 test15)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Benchmark all relevant testcases discovered across the suite",
    )
    parser.add_argument(
        "--no-old",
        action="store_true",
        help="Skip running the slow old unindexed reference implementation",
    )
    args = parser.parse_args()

    filter_tests = args.tests if args.tests else None
    queries = discover_relevant_queries(filter_tests=filter_tests)

    if not queries:
        print("No matching testcases found with point-to-point depth queries.")
        return

    print(f"Discovered {len(queries)} point-to-point logic depth queries across the benchmark suite.", flush=True)
    run_benchmarks(queries, run_old=not args.no_old)
    verify_edge_cases()


if __name__ == "__main__":
    main()
