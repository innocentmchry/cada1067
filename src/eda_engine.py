"""EDA Engine — netlist analysis and transformation operations."""

from __future__ import annotations

import copy
import itertools
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections import deque
from typing import Dict, List, Optional, Set, Tuple

from .netlist_parser import (
    GateNode,
    Netlist,
    ParseError,
    WireInfo,
    ONE_INPUT_GATES,
    PRIMITIVE_GATES,
    TWO_INPUT_GATES,
    parse_verilog,
    write_verilog,
)


# ---------------------------------------------------------------------------
# Module-level template cache for replace_gate_type_in_cone.
# Key: (source_type: str, frozenset(target_types))
# Value: dict with keys: inputs (List[str]), output (str),
#        gates (List[GateNode]), internal_wires (List[str])
# ---------------------------------------------------------------------------
_SUBSTITUTION_TEMPLATE_CACHE: Dict[Tuple, dict] = {}


def _workspace_temp_dir() -> str:
    """Return the writable temporary root inside the current working directory."""
    root = os.path.abspath(os.path.join(os.getcwd(), "temp"))
    os.makedirs(root, exist_ok=True)
    return root


def _temp_subprocess_env() -> Dict[str, str]:
    """Force Yosys/ABC and child processes to use the workspace temp root."""
    root = _workspace_temp_dir()
    env = os.environ.copy()
    env.update({"TMPDIR": root, "TEMP": root, "TMP": root})
    return env


def _yosys_binary() -> str:
    """Resolve Yosys from YOSYS_BIN or PATH and validate executability."""
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
    raise RuntimeError(
        "Yosys executable not found. Set YOSYS_BIN or add yosys to PATH."
    )


def _parse_blif_template(blif_path: str, target_types: List[str], strip_buf: bool) -> Optional[dict]:
    """Parse ABC BLIF output into a gate template dict.

    Direct ABC emits .gate lines and Yosys emits equivalent .subckt lines:
        .gate not  A=A Y=new_n4_
        .subckt nand A=new_n4_ B=new_n5_ Y=Y
    .inputs / .outputs declare port names.
    Returns None if the file cannot be parsed.
    """
    try:
        with open(blif_path) as fh:
            lines = fh.readlines()
    except OSError:
        return None

    inputs: List[str] = []
    outputs: List[str] = []
    gates: List[GateNode] = []
    g_counter = 0

    for line in lines:
        line = line.strip()
        if line.startswith(".inputs"):
            inputs = line.split()[1:]
        elif line.startswith(".outputs"):
            outputs = line.split()[1:]
        elif line.startswith((".gate", ".subckt")):
            parts = line.split()
            gate_type = parts[1].lower()
            # Build port→signal map
            port_sig: Dict[str, str] = {}
            for tok in parts[2:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    port_sig[k.upper()] = v
            # Determine output and inputs
            out_sig = port_sig.get("Y", "")
            in_sigs = [port_sig.get("A", ""), port_sig.get("B", "")] if gate_type not in ONE_INPUT_GATES else [port_sig.get("A", "")]
            in_sigs = [s for s in in_sigs if s]  # drop empty
            gates.append(GateNode(name=f"_t{g_counter}", gate_type=gate_type,
                                  inputs=in_sigs, output=out_sig))
            g_counter += 1

    if not gates or not outputs:
        return None

    # Determine primary output (last output declared)
    tmpl_output = outputs[0]

    # If strip_buf, inline any buf gates (pass-throughs)
    if strip_buf:
        buf_map: Dict[str, str] = {}
        keep = []
        for g in gates:
            if g.gate_type == "buf":
                buf_map[g.output] = g.inputs[0]
            else:
                keep.append(g)
        # Apply renames through all signals
        def remap(sig: str) -> str:
            while sig in buf_map:
                sig = buf_map[sig]
            return sig
        for g in keep:
            g.inputs = [remap(i) for i in g.inputs]
            g.output = remap(g.output)
        if tmpl_output in buf_map:
            tmpl_output = remap(tmpl_output)
        gates = keep

    # Internal wires = signals not in inputs or outputs
    all_sigs: set = set()
    for g in gates:
        all_sigs.add(g.output)
        all_sigs.update(g.inputs)
    port_sigs = set(inputs) | {tmpl_output}
    internal_wires = [s for s in all_sigs if s not in port_sigs and s not in {"$false", "$true", "$undef"}]

    return {
        "inputs":          inputs,
        "output":          tmpl_output,
        "gates":           gates,
        "internal_wires":  internal_wires,
    }


# ---------------------------------------------------------------------------
# EDA Engine class
# ---------------------------------------------------------------------------

class EDAEngine:
    """Wraps a Netlist and exposes analysis and transformation operations.

    All methods that accept signal / instance names validate them against
    the current netlist and raise ValueError if they are not found.
    """

    def __init__(self) -> None:
        self._netlist: Optional[Netlist] = None
        self._snapshots: Dict[str, Netlist] = {}
        self._instance_counter: int = 0
        self._allowed_gates_constraint: Optional[List[str]] = None
        self._original_netlist_path: Optional[str] = None

    # ------------------------------------------------------------------
    # Netlist lifecycle
    # ------------------------------------------------------------------

    def load(self, filepath: str) -> Netlist:
        """Load a Verilog file into the engine and return the Netlist."""
        self._netlist = parse_verilog(filepath)
        self._original_netlist_path = os.path.abspath(filepath)
        self._allowed_gates_constraint = None
        return self._netlist

    def save(self, filepath: str) -> None:
        """Write the current netlist back to a Verilog file."""
        self._require_netlist()
        write_verilog(self._netlist, filepath)

    def add_snapshot(self, label: str = "default") -> None:
        """Save a deep-copy snapshot of the current netlist under *label*."""
        self._require_netlist()
        self._snapshots[label] = copy.deepcopy(self._netlist)

    def restore_snapshot(self, label: str = "default") -> None:
        """Restore the netlist from a previously saved snapshot."""
        if label not in self._snapshots:
            raise ValueError(f"No snapshot with label {label!r}")
        self._netlist = copy.deepcopy(self._snapshots[label])

    @property
    def netlist(self) -> Netlist:
        """The currently loaded Netlist."""
        self._require_netlist()
        return self._netlist  # type: ignore[return-value]

    @property
    def original_netlist_path(self) -> Optional[str]:
        """Absolute path of the design most recently loaded from disk."""
        return self._original_netlist_path

    def _require_netlist(self) -> None:
        if self._netlist is None:
            raise ValueError("No netlist loaded. Call read_design first.")

    # ------------------------------------------------------------------
    # Internal graph helpers
    # ------------------------------------------------------------------

    def _build_output_to_gate(self) -> Dict[str, str]:
        """Map each output net name → gate instance name that drives it."""
        mapping: Dict[str, str] = {}
        for inst_name, node in self._netlist.nodes.items():  # type: ignore[union-attr]
            mapping[node.output] = inst_name
        for inst_name, dff in self._netlist.dffs.items():  # type: ignore[union-attr]
            mapping[dff.q] = inst_name
        return mapping

    def _build_fanout_map(self) -> Dict[str, List[str]]:
        """Map each net name → list of gate instance names that consume it."""
        fanout: Dict[str, List[str]] = {}
        for inst_name, node in self._netlist.nodes.items():  # type: ignore[union-attr]
            for inp in node.inputs:
                fanout.setdefault(inp, []).append(inst_name)
        for inst_name, dff in self._netlist.dffs.items():  # type: ignore[union-attr]
            for sig in (dff.ck, dff.rn, dff.sn, dff.d):
                if sig:
                    fanout.setdefault(sig, []).append(inst_name)
        return fanout

    def _resolve_signal(self, name: str) -> str:
        """Validate that *name* is a known signal; return it unchanged."""
        nl = self._netlist
        assert nl is not None
        known = (
            set(nl.primary_inputs)
            | set(nl.primary_outputs)
            | set(nl.wires.keys())
            | {n.output for n in nl.nodes.values()}
            | {inp for n in nl.nodes.values() for inp in n.inputs}
            | {dff.q for dff in nl.dffs.values()}
            | {dff.d for dff in nl.dffs.values()}
            | {"1'b0", "1'b1"}
        )
        if name not in known:
            raise ValueError(f"Signal {name!r} not found in netlist.")
        return name

    def _resolve_instance(self, inst_name: str) -> str:
        """Validate that *inst_name* is a known gate or DFF instance."""
        nl = self._netlist
        assert nl is not None
        if inst_name not in nl.nodes and inst_name not in nl.dffs:
            raise ValueError(f"Instance {inst_name!r} not found in netlist.")
        return inst_name

    def _next_inst_name(self, prefix: str = "U_gen") -> str:
        """Generate a unique gate instance name."""
        self._instance_counter += 1
        return f"{prefix}_{self._instance_counter}"

    def _next_wire_name(self, prefix: str = "w_gen") -> str:
        """Generate a unique internal wire name."""
        self._instance_counter += 1
        return f"{prefix}_{self._instance_counter}"

    def _add_wire(self, name: str) -> None:
        """Register an internal wire in the netlist."""
        assert self._netlist is not None
        self._netlist.wires[name] = WireInfo(name=name, width=1, is_bus=False)

    # ------------------------------------------------------------------
    # Analysis: signal-level graph (treating each signal as a node)
    # ------------------------------------------------------------------

    def _forward_successors(self, signal: str) -> List[str]:
        """Return the output signals of all gates driven by *signal*.

        DFF q-to-d arcs are treated as a *cut* (do not follow through DFF).
        """
        nl = self._netlist
        assert nl is not None
        result: List[str] = []
        for node in nl.nodes.values():
            if signal in node.inputs:
                result.append(node.output)
        
        return result

    def _backward_predecessors(self, signal: str) -> List[str]:
        """Return all input signals of the gate whose output is *signal*."""
        nl = self._netlist
        assert nl is not None
        out2gate = self._build_output_to_gate()
        driver = out2gate.get(signal)
        if driver is None:
            return []
        if driver in nl.nodes:
            return list(nl.nodes[driver].inputs)
        return []  # DFF — treat as cut

    # ==================================================================
    # ANALYSIS OPERATIONS
    # ==================================================================

    def count_gates(self):
        """Return gate counts grouped by type."""

        if self.netlist is None:
            return {"error": "No design loaded"}

        counts = {
            "AND": 0,
            "OR": 0,
            "NOT": 0,
            "NAND": 0,
            "NOR": 0,
            "XOR": 0,
            "XNOR": 0,
            "BUF": 0,
            "DFF": 0,
        }

        # Count combinational gates
        for node in self.netlist.nodes.values():
            gt = node.gate_type.upper()

            if gt in counts:
                counts[gt] += 1

        # Count DFFs separately
        counts["DFF"] = len(self.netlist.dffs)

        total = sum(counts.values())

        return {
            "total_gates": total,
            "breakdown": counts,
        }
    def _driver_of(self, signal: str):
        """Return gate driving this signal, or None."""

        for node in self.netlist.nodes.values():
            if node.output == signal:
                return node

        return None
    
    def _fanin_depth(self, signal: str, memo=None) -> int:
        """Return max logic depth ending at signal."""

        if memo is None:
            memo = {}

        if signal in memo:
            return memo[signal]

        # Primary input
        if signal in self.netlist.primary_inputs:
            memo[signal] = 0
            return 0

        driver = self._driver_of(signal)

        # No driver
        if driver is None:
            memo[signal] = 0
            return 0

        depth = 1 + max(
            self._fanin_depth(inp, memo)
            for inp in driver.inputs
        )

        memo[signal] = depth
        return depth
    
    def get_fanin_cone_depth(self, output_signal: str):
        """Return max logic depth of output fanin cone."""

        self._require_netlist()
        self._resolve_signal(output_signal)

        return {
            "output": output_signal,
            "depth": self._fanin_depth(output_signal)
        }
        
    def get_max_depth(
        self, source: str, sink: str
    ) -> Tuple[int, List[str]]:
        """Return (depth, path) of the longest combinational path from source to sink.

        Traverses forward through gate outputs; DFF q-to-d is treated as a cut.
        Returns (-1, []) if no path exists.

        Args:
            source: Starting signal name (primary input or gate output).
            sink:   Ending signal name (primary output or gate output).

        Returns:
            A tuple (depth, path) where path is a list of signal names.
        """
        self._require_netlist()
        self._resolve_signal(source)
        self._resolve_signal(sink)

        # Topological longest-path via DFS with memoisation
        # Nodes are signal names; edges follow _forward_successors.

        # First collect all reachable signals from source (forward BFS)
        reachable: Set[str] = set()
        queue: deque[str] = deque([source])
        while queue:
            cur = queue.popleft()
            if cur in reachable:
                continue
            reachable.add(cur)
            for nxt in self._forward_successors(cur):
                if nxt not in reachable:
                    queue.append(nxt)

        if sink not in reachable:
            return (-1, [])

        # DFS with explicit stack for longest path tracking
        # dist[s] = longest path length (in gate hops) from source to s
        dist: Dict[str, int] = {source: 0}
        parent: Dict[str, Optional[str]] = {source: None}

        # Topological sort within reachable
        visited: Set[str] = set()
        topo_order: List[str] = []

        def dfs_topo(node: str) -> None:
            visited.add(node)
            for nxt in self._forward_successors(node):
                if nxt in reachable and nxt not in visited:
                    dfs_topo(nxt)
            topo_order.append(node)

        dfs_topo(source)
        topo_order.reverse()
        # print(f"Topo Order is: ")
        # print(topo_order)

        for sig in topo_order:
            for nxt in self._forward_successors(sig):
                if nxt not in reachable:
                    continue
                new_dist = dist.get(sig, -1) + 1
                if new_dist > dist.get(nxt, -1):
                    dist[nxt] = new_dist
                    parent[nxt] = sig

        if sink not in dist:
            return (-1, [])

        # print(dist)
        # print(parent)

        # Reconstruct path
        path: List[str] = []
        cur: Optional[str] = sink
        while cur is not None:
            path.append(cur)
            cur = parent.get(cur)
        path.reverse()

        return (dist[sink], path)

    def get_max_depth_between_endpoint_classes(
        self,
        source_class: str,
        sink_class: str,
    ) -> dict:
        """Return the longest combinational path between endpoint classes."""
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        source_class = source_class.upper()
        sink_class = sink_class.upper()
        if source_class != "PI":
            raise ValueError("Currently supported source_class is 'PI'.")
        if sink_class not in {"DFF_D", "PO"}:
            raise ValueError("sink_class must be 'DFF_D' or 'PO'.")

        def expand_declared(name: str) -> List[str]:
            wi = nl.wires.get(name)
            if wi and wi.is_bus:
                lo, hi = sorted((wi.lsb, wi.msb))
                return [f"{name}[{bit}]" for bit in range(lo, hi + 1)]
            return [name]

        pi_signals: Set[str] = set()
        for name in nl.primary_inputs:
            pi_signals.update(expand_declared(name))
            pi_signals.add(name)

        comb_driver = {node.output: node for node in nl.nodes.values()}
        memo: Dict[str, Optional[int]] = {}
        parent: Dict[str, str] = {}
        active: Set[str] = set()

        def longest_from_pi(signal_name: str) -> Optional[int]:
            if signal_name in memo:
                return memo[signal_name]
            if signal_name in pi_signals:
                memo[signal_name] = 0
                return 0
            if signal_name in {"1'b0", "1'b1"} or signal_name in active:
                memo[signal_name] = None
                return None

            node = comb_driver.get(signal_name)
            if node is None:
                memo[signal_name] = None
                return None

            active.add(signal_name)
            candidates = [
                (depth, inp)
                for inp in node.inputs
                if (depth := longest_from_pi(inp)) is not None
            ]
            active.discard(signal_name)
            if not candidates:
                memo[signal_name] = None
                return None

            input_depth, input_signal = max(candidates, key=lambda item: item[0])
            memo[signal_name] = input_depth + 1
            parent[signal_name] = input_signal
            return memo[signal_name]

        if sink_class == "DFF_D":
            endpoints = [
                (inst_name, dff.d)
                for inst_name, dff in sorted(nl.dffs.items())
                if dff.d
            ]
        else:
            endpoints = [
                (signal_name, signal_name)
                for output in nl.primary_outputs
                for signal_name in expand_declared(output)
            ]

        best: Optional[Tuple[int, str, str]] = None
        for endpoint_name, endpoint_signal in endpoints:
            depth = longest_from_pi(endpoint_signal)
            if depth is None:
                continue
            candidate = (depth, endpoint_name, endpoint_signal)
            if best is None or candidate[0] > best[0]:
                best = candidate

        if best is None:
            return {
                "source_class": source_class,
                "sink_class": sink_class,
                "depth": -1,
                "path": [],
            }

        depth, endpoint_name, endpoint_signal = best
        path = [endpoint_signal]
        while path[-1] in parent:
            path.append(parent[path[-1]])
        path.reverse()
        return {
            "source_class": source_class,
            "sink_class": sink_class,
            "depth": depth,
            "source_signal": path[0],
            "sink_name": endpoint_name,
            "sink_signal": endpoint_signal,
            "path": path,
        }

    def path_passes_through(
        self, source: str, sink: str, node: str
    ) -> bool:
        """Return True if ALL paths from source to sink pass through node.

        Args:
            source: Starting signal.
            sink:   Ending signal.
            node:   Candidate mandatory waypoint signal.
        """
        self._require_netlist()
        self._resolve_signal(source)
        self._resolve_signal(sink)
        self._resolve_signal(node)

        # If there is ANY path from source to sink that avoids node → False
        avoiding = self.find_path_avoiding(source, sink, node)
        if avoiding is not None:
            return False

        # Check that at least one path exists through node
        depth, _ = self.get_max_depth(source, sink)
        return depth >= 0

    def find_path_avoiding(
        self, source: str, sink: str, avoid: str
    ) -> Optional[List[str]]:
        """Return one path from source to sink that does NOT pass through avoid.

        Returns None if no such path exists.

        Args:
            source: Starting signal.
            sink:   Ending signal.
            avoid:  Signal that must not appear on the path.
        """
        self._require_netlist()
        self._resolve_signal(source)
        self._resolve_signal(sink)
        self._resolve_signal(avoid)

        if source == avoid or sink == avoid:
            return None

        # BFS avoiding the forbidden signal
        queue: deque[List[str]] = deque([[source]])
        visited: Set[str] = {source}

        while queue:
            path = queue.popleft()
            cur = path[-1]
            if cur == sink:
                return path
            for nxt in self._forward_successors(cur):
                if nxt not in visited and nxt != avoid:
                    visited.add(nxt)
                    queue.append(path + [nxt])
        return None

    def get_logic_cone(self, output_signal: str) -> List[str]:
        """Return all gate instance names that transitively feed output_signal.

        Args:
            output_signal: The target output net name.
        """
        self._require_netlist()
        self._resolve_signal(output_signal)
        nl = self._netlist
        assert nl is not None

        out2gate = self._build_output_to_gate()

        cone_gates: List[str] = []
        visited_signals: Set[str] = set()
        queue: deque[str] = deque([output_signal])

        while queue:
            sig = queue.popleft()
            if sig in visited_signals:
                continue
            visited_signals.add(sig)
            driver = out2gate.get(sig)
            if driver is None or driver not in nl.nodes:
                continue  # primary input or DFF q
            gate = nl.nodes[driver]
            if driver not in cone_gates:
                cone_gates.append(driver)
            for inp in gate.inputs:
                if inp not in visited_signals:
                    queue.append(inp)

        return cone_gates

    def count_cone_gates(self, output_signal: str) -> int:
        """Return the number of gates in the logic cone of output_signal.

        Args:
            output_signal: The target output net name.
        """
        return len(self.get_logic_cone(output_signal))
    def _nodes_reaching_sink(self, sink: str) -> Set[str]:
        """Return all signals that can reach sink."""

        reachable: Set[str] = set()
        queue: deque[str] = deque([sink])

        while queue:

            cur = queue.popleft()

            if cur in reachable:
                continue

            reachable.add(cur)

            for prev in self._backward_predecessors(cur):
                queue.append(prev)

        return reachable
    def find_all_paths(
        self,
        source: str,
        sink: str,
        max_paths: int = 500
    ):

        self._require_netlist()

        sink_reachable = self._nodes_reaching_sink(sink)

        paths = []

        self._dfs_paths(
            current=source,
            sink=sink,
            path=[source],
            paths=paths,
            max_paths=max_paths,
            sink_reachable=sink_reachable,
        )

        return {
            "source": source,
            "sink": sink,
            "count": len(paths),
            "paths": paths,
            "truncated": len(paths) >= max_paths,
        }

    def find_register_to_register_paths(
        self,
        inline_limit: int = 10,
        max_paths: int = 100000,
    ) -> dict:
        """Enumerate combinational paths from every DFF Q pin to every DFF D pin."""
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        consumers: Dict[str, List[str]] = {}
        for gate_name, gate in nl.nodes.items():
            for input_signal in gate.inputs:
                consumers.setdefault(input_signal, []).append(gate_name)

        dff_sinks: Dict[str, List[str]] = {}
        for dff_name, dff in nl.dffs.items():
            if dff.d:
                dff_sinks.setdefault(dff.d, []).append(dff_name)

        # Prune branches that cannot reach any DFF D pin.
        out2gate = self._build_output_to_gate()
        reaches_dff_d: Set[str] = set(dff_sinks)
        queue: deque[str] = deque(reaches_dff_d)
        while queue:
            signal_name = queue.popleft()
            driver_name = out2gate.get(signal_name)
            if not driver_name or driver_name not in nl.nodes:
                continue
            for input_signal in nl.nodes[driver_name].inputs:
                if input_signal not in reaches_dff_d:
                    reaches_dff_d.add(input_signal)
                    queue.append(input_signal)

        report = tempfile.NamedTemporaryFile(
            "w",
            prefix="register_to_register_paths_",
            suffix=".txt",
            dir=_workspace_temp_dir(),
            delete=False,
        )
        report_path = os.path.abspath(report.name)
        inline_paths: List[dict] = []
        count = 0
        truncated = False

        def record_path(
            source_dff: str,
            source_q: str,
            sink_dff: str,
            sink_d: str,
            gate_path: List[str],
        ) -> None:
            nonlocal count
            count += 1
            entry = {
                "source_dff": source_dff,
                "source_pin": "Q",
                "source_signal": source_q,
                "sink_dff": sink_dff,
                "sink_pin": "D",
                "sink_signal": sink_d,
                "gates": list(gate_path),
            }
            if count <= inline_limit:
                inline_paths.append(entry)
            middle = " -> ".join(gate_path)
            if middle:
                middle = f" -> {middle}"
            report.write(
                f"{count}. {source_dff}.Q({source_q}){middle} "
                f"-> {sink_dff}.D({sink_d})\n"
            )

        def walk(
            source_dff: str,
            source_q: str,
            signal_name: str,
            gate_path: List[str],
            visited_signals: Set[str],
        ) -> None:
            nonlocal truncated
            for sink_dff in dff_sinks.get(signal_name, []):
                if count >= max_paths:
                    truncated = True
                    return
                record_path(source_dff, source_q, sink_dff, signal_name, gate_path)

            for gate_name in consumers.get(signal_name, []):
                if count >= max_paths:
                    truncated = True
                    return
                output_signal = nl.nodes[gate_name].output
                if output_signal not in reaches_dff_d or output_signal in visited_signals:
                    continue
                visited_signals.add(output_signal)
                gate_path.append(gate_name)
                walk(source_dff, source_q, output_signal, gate_path, visited_signals)
                gate_path.pop()
                visited_signals.remove(output_signal)
                if truncated:
                    return

        try:
            report.write("# Register-to-register combinational paths\n")
            for source_dff, dff in nl.dffs.items():
                if truncated:
                    break
                if not dff.q or dff.q not in reaches_dff_d:
                    continue
                walk(source_dff, dff.q, dff.q, [], {dff.q})
        finally:
            if truncated:
                report.write(
                    f"# Truncated after {max_paths} paths (safety limit reached).\n"
                )
            report.close()

        result = {"count": count, "truncated": truncated}
        if count <= inline_limit:
            result["paths"] = inline_paths
            try:
                os.unlink(report_path)
            except OSError:
                pass
        else:
            result["file_path"] = report_path
        return result

    def _dfs_paths(
        self,
        current: str,
        sink: str,
        path: list[str],
        paths: list[list[str]],
        max_paths: int,
        sink_reachable: Set[str],
    ):

        if len(paths) >= max_paths:
            return

        if current == sink:
            paths.append(path.copy())
            return

        for nxt in self._forward_successors(current):

            # prune impossible branches
            if nxt not in sink_reachable:
                continue

            # avoid cycles
            if nxt in path:
                continue

            path.append(nxt)

            self._dfs_paths(
                current=nxt,
                sink=sink,
                path=path,
                paths=paths,
                max_paths=max_paths,
                sink_reachable=sink_reachable,
            )

            path.pop()
            

    def get_fanout(self, net_name: str) -> List[str]:
        """Return all gate instance names driven by net_name.

        Args:
            net_name: The net to query.
        """
        self._require_netlist()
        self._resolve_signal(net_name)
        return self._build_fanout_map().get(net_name, [])

    def get_fanout_report(self, net_name: str, inline_limit: int = 10) -> dict:
        """Return a compact fanout report, spilling large lists to a CWD file."""
        fanout = sorted(set(self.get_fanout(net_name)))
        result = {"net_name": net_name, "count": len(fanout)}
        if len(fanout) <= inline_limit:
            result["fanout"] = fanout
            return result

        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", net_name).strip("_") or "net"
        report = tempfile.NamedTemporaryFile(
            "w",
            prefix=f"fanout_{safe_name}_",
            suffix=".txt",
            dir=_workspace_temp_dir(),
            delete=False,
        )
        with report:
            report.write(f"# Direct fanout gates for {net_name} (count: {len(fanout)})\n")
            for gate_name in fanout:
                report.write(f"{gate_name}\n")
        result["file_path"] = os.path.abspath(report.name)
        return result

    def resolve_name_type(self, name: str) -> dict:
        """Classify a name as a combinational gate, DFF, or signal."""
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        kinds: List[str] = []
        if name in nl.nodes:
            kinds.append("combinational_gate")
        if name in nl.dffs:
            kinds.append("dff")
        known_signals = (
            set(nl.wires)
            | set(nl.primary_inputs)
            | set(nl.primary_outputs)
            | {node.output for node in nl.nodes.values()}
            | {sig for node in nl.nodes.values() for sig in node.inputs}
            | {dff.q for dff in nl.dffs.values()}
            | {dff.d for dff in nl.dffs.values()}
        )
        if name in known_signals:
            kinds.append("signal")

        if not kinds:
            raise ValueError(f"Unknown gate, DFF, or signal name: {name!r}")
        if len(kinds) > 1:
            return {"name": name, "type": "ambiguous", "matches": kinds}
        kind = kinds[0]
        result = {"name": name, "type": kind}
        if kind == "combinational_gate":
            result["output_net"] = nl.nodes[name].output
        elif kind == "dff":
            result["output_net"] = nl.dffs[name].q
        else:
            result["output_net"] = name
        return result

    def get_gate_info(self, gate_name: str) -> dict:
        """Return a gate instance's primitive type and logical pin connections."""
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        if gate_name in nl.nodes:
            node = nl.nodes[gate_name]
            pins = {"Y": node.output}
            for index, net_name in enumerate(node.inputs):
                pin_name = chr(ord("A") + index) if index < 26 else f"I{index}"
                pins[pin_name] = net_name
            return {
                "instance": gate_name,
                "instance_type": "combinational_gate",
                "gate_type": node.gate_type.upper(),
                "pins": pins,
            }

        if gate_name in nl.dffs:
            dff = nl.dffs[gate_name]
            return {
                "instance": gate_name,
                "instance_type": "dff",
                "gate_type": "DFF",
                "pins": {
                    "CK": dff.ck,
                    "RN": dff.rn,
                    "SN": dff.sn,
                    "D": dff.d,
                    "Q": dff.q,
                },
            }

        raise ValueError(f"Gate or DFF instance {gate_name!r} not found in netlist.")

    def get_gate_fanout_report(self, gate_name: str, inline_limit: int = 10) -> dict:
        """Return direct consumers of a combinational gate or DFF output."""
        resolved = self.resolve_name_type(gate_name)
        if resolved["type"] not in {"combinational_gate", "dff"}:
            raise ValueError(f"{gate_name!r} is not a gate instance.")
        report = self.get_fanout_report(resolved["output_net"], inline_limit)
        report.update({
            "source_name": gate_name,
            "source_type": resolved["type"],
            "output_net": resolved["output_net"],
        })
        return report

    def _reachable_gates_report(
        self,
        source_name: str,
        source_type: str,
        start_nets: List[str],
        inline_limit: int = 10,
    ) -> dict:
        """Traverse transitive combinational fanout and report reached gates."""
        nl = self._netlist
        assert nl is not None
        fanout_map = self._build_fanout_map()
        queue: deque[str] = deque(start_nets)
        visited_nets: Set[str] = set()
        reached: Set[str] = set()

        while queue:
            net = queue.popleft()
            if net in visited_nets:
                continue
            visited_nets.add(net)
            for instance in fanout_map.get(net, []):
                if instance in reached:
                    continue
                reached.add(instance)
                if instance in nl.nodes:
                    queue.append(nl.nodes[instance].output)
                # DFFs are included as reached endpoints but traversal stops.

        gates = sorted(reached)
        result = {
            "source_name": source_name,
            "source_type": source_type,
            "start_nets": start_nets,
            "count": len(gates),
        }
        if len(gates) <= inline_limit:
            result["gates"] = gates
            return result

        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_name).strip("_") or "source"
        report = tempfile.NamedTemporaryFile(
            "w",
            prefix=f"reachable_{safe_name}_",
            suffix=".txt",
            dir=_workspace_temp_dir(),
            delete=False,
        )
        with report:
            report.write(
                f"# Gates reachable from {source_name} ({source_type}); count: {len(gates)}\n"
            )
            for gate_name in gates:
                report.write(f"{gate_name}\n")
        result["file_path"] = os.path.abspath(report.name)
        return result

    def get_reachable_gates_from_net(self, net_name: str) -> dict:
        """Return all gates transitively reachable from a signal or bus."""
        resolved = self.resolve_name_type(net_name)
        if resolved["type"] != "signal":
            raise ValueError(f"{net_name!r} is not an unambiguous signal or wire.")
        start_nets = [net_name]
        wi = self._netlist.wires.get(net_name)  # type: ignore[union-attr]
        if wi and wi.is_bus:
            lo, hi = sorted((wi.lsb, wi.msb))
            start_nets.extend(f"{net_name}[{bit}]" for bit in range(lo, hi + 1))
        return self._reachable_gates_report(
            net_name, "signal", list(dict.fromkeys(start_nets))
        )

    def get_reachable_gates_from_gate(self, gate_name: str) -> dict:
        """Return all downstream gates reachable from a gate or DFF output."""
        resolved = self.resolve_name_type(gate_name)
        if resolved["type"] not in {"combinational_gate", "dff"}:
            raise ValueError(f"{gate_name!r} is not an unambiguous gate instance.")
        return self._reachable_gates_report(
            gate_name, resolved["type"], [resolved["output_net"]]
        )
    
    def get_gate_fanout(self, gate_name: str):
        nl = self._netlist
        assert nl is not None

        if gate_name not in nl.nodes:
            raise ValueError(f"Unknown gate: {gate_name}")

        output_signal = nl.nodes[gate_name].output

        return self.get_fanout(output_signal)

    def list_signals(self) -> dict:
        """Return a dictionary of available signal name lists in the current netlist.

        Returns keys: `primary_inputs`, `primary_outputs`, `wires`, `gate_outputs`, `dff_q`, `dff_d`.
        """
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        gate_outputs = [n.output for n in nl.nodes.values()]
        dff_q = [d.q for d in nl.dffs.values()]
        dff_d = [d.d for d in nl.dffs.values()]

        return {
            "primary_inputs": list(nl.primary_inputs),
            "primary_outputs": list(nl.primary_outputs),
            "wires": list(nl.wires.keys()),
            "gate_outputs": gate_outputs,
            "dff_q": dff_q,
            "dff_d": dff_d,
        }

    def are_same_clock_domain(self, dff1: str, dff2: str) -> bool:
        """Return True if both DFFs share the same clock net.

        Args:
            dff1: First DFF instance name.
            dff2: Second DFF instance name.
        """
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        for d in (dff1, dff2):
            if d not in nl.dffs:
                raise ValueError(f"DFF instance {d!r} not found in netlist.")

        return nl.dffs[dff1].ck == nl.dffs[dff2].ck

    def check_signal_equivalence(self, sig1: str, sig2: str) -> bool:
        """Check if two signals in the current netlist are functionally equivalent.

        Uses Yosys SAT solver to verify equivalence.
        Returns True if both signals produce identical logic for all inputs.

        Args:
            sig1: First signal name to compare.
            sig2: Second signal name to compare.
        """
        self._require_netlist()
        nl = self._netlist
        assert nl is not None
        constant_aliases = {
            "0": "1'b0", "1": "1'b1", "'0": "1'b0", "'1": "1'b1"
        }
        sig1 = constant_aliases.get(sig1.strip().lower(), sig1)
        sig2 = constant_aliases.get(sig2.strip().lower(), sig2)
        self._resolve_signal(sig1)
        self._resolve_signal(sig2)

        # Signal equivalence is combinational: primary inputs and DFF-Q values
        # are independent boundaries. Replace each DFF-Q with a fresh PI so SAT
        # never depends on a sequential-cell model.
        comb_nl = copy.deepcopy(nl)
        q_aliases: Dict[str, str] = {}
        for index, dff in enumerate(comb_nl.dffs.values()):
            if not dff.q or dff.q in q_aliases:
                continue
            alias = f"_signal_equiv_q_{index}"
            q_aliases[dff.q] = alias
            comb_nl.wires[alias] = WireInfo(name=alias)
            comb_nl.primary_inputs.append(alias)
        for node in comb_nl.nodes.values():
            node.inputs = [q_aliases.get(sig, sig) for sig in node.inputs]
        comb_nl.dffs.clear()

        sat_sig1 = q_aliases.get(sig1, sig1)
        sat_sig2 = q_aliases.get(sig2, sig2)
        if sat_sig1 == sat_sig2:
            return True

        # `prep` removes unobserved internal wires before the SAT pass. Expose
        # queried internal signals as outputs in this temporary copy so their
        # names and drivers remain available to `sat -set`.
        for signal_name in (sat_sig1, sat_sig2):
            if (
                signal_name not in {"1'b0", "1'b1"}
                and signal_name not in comb_nl.primary_inputs
                and signal_name not in comb_nl.primary_outputs
            ):
                comb_nl.primary_outputs.append(signal_name)

        # Write netlist to temporary file
        tf = tempfile.NamedTemporaryFile(
            "w", suffix=".v", dir=_workspace_temp_dir(), delete=False
        )
        netlist_path = tf.name
        tf.close()
        write_verilog(comb_nl, netlist_path)

        try:
            return self._yosys_check_signals_equiv(
                netlist_path,
                comb_nl.module_name,
                sat_sig1,
                sat_sig2,
            )
        finally:
            import os
            try:
                os.unlink(netlist_path)
            except OSError:
                pass

    def _yosys_check_signals_equiv(
        self, netlist_verilog: str, top: str, sig1: str, sig2: str
    ) -> bool:
        """Use Yosys SAT to check if two signals are equivalent.
        
        Returns True if signals are equivalent, False otherwise.
        """

        def check_sat(v1, v2):
            """Check if the constraint sig1={v1} AND sig2={v2} is satisfiable."""
            constraints: List[str] = []
            for sig, value in ((sig1, v1), (sig2, v2)):
                if sig in {"1'b0", "1'b1"}:
                    if int(sig[-1]) != value:
                        return False
                    continue
                constraints.append(f"-set {sig} {value}")
            script = f"""
read_verilog {netlist_verilog}
prep -top {top}
sat {' '.join(constraints)}
"""

            with tempfile.NamedTemporaryFile(
                "w", suffix=".ys", dir=_workspace_temp_dir(), delete=False
            ) as f:
                f.write(script)
                f.flush()
                script_path = f.name

            try:
                result = subprocess.run(
                    [_yosys_binary(), "-s", script_path],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=_temp_subprocess_env(),
                )

                combined = (result.stdout or "") + "\n" + (result.stderr or "")
                if result.returncode != 0:
                    raise RuntimeError(
                        "Yosys signal-equivalence SAT check failed:\n"
                        + combined[-3000:]
                    )
                # If model found, SAT is satisfiable
                lower = combined.lower()
                if "sat solving finished - no model found" in lower:
                    return False
                if "sat solving finished - model found" in lower:
                    return True
                raise RuntimeError(
                    "Could not determine Yosys SAT result:\n" + combined[-3000:]
                )
            finally:
                import os
                try:
                    os.unlink(script_path)
                except OSError:
                    pass

        # Test both scenarios:
        # sat01: sig1=0 AND sig2=1 (both different and contradictory)
        # sat10: sig1=1 AND sig2=0 (both different and contradictory)
        sat01 = check_sat(0, 1)
        sat10 = check_sat(1, 0)

        # Signals are equivalent if neither contradictory scenario is satisfiable
        return not sat01 and not sat10

    def check_signal_constant(self, signal_name: str, value: object) -> dict:
        """Prove that a scalar or vector signal always equals a constant value."""
        import textwrap

        self._require_netlist()
        nl = self._netlist
        assert nl is not None
        self._resolve_signal(signal_name)

        if "[" in signal_name:
            width = 1
        else:
            wi = nl.wires.get(signal_name)
            width = wi.width if wi else 1

        raw = str(value).strip().lower().replace("_", "")
        if raw in {"'0", "1'b0"}:
            constant = 0
        elif raw == "'1":
            constant = (1 << width) - 1
        elif raw == "1'b1":
            constant = 1
        else:
            binary = re.fullmatch(r"(\d+)'b([01]+)", raw)
            if binary:
                constant = int(binary.group(2), 2)
            else:
                try:
                    constant = int(raw, 0)
                except ValueError as exc:
                    raise ValueError(
                        f"Unsupported constant {value!r}; use an integer or Verilog binary literal."
                    ) from exc

        if constant < 0 or constant >= (1 << width):
            raise ValueError(
                f"Constant {value!r} does not fit signal {signal_name!r} width {width}."
            )

        dff_qs = {dff.q for dff in nl.dffs.values() if dff.q}
        queried_bits = {signal_name}
        if width > 1 and "[" not in signal_name:
            queried_bits = {f"{signal_name}[{bit}]" for bit in range(width)}
        if queried_bits & dff_qs:
            return {
                "always_equal": False,
                "signal": signal_name,
                "value": constant,
                "width": width,
                "reason": "Signal includes an unconstrained DFF-Q boundary.",
            }

        comb_nl = copy.deepcopy(nl)
        q_aliases: Dict[str, str] = {}
        for index, dff in enumerate(comb_nl.dffs.values()):
            if not dff.q or dff.q in q_aliases:
                continue
            alias = f"_constant_check_q_{index}"
            q_aliases[dff.q] = alias
            comb_nl.wires[alias] = WireInfo(name=alias)
            comb_nl.primary_inputs.append(alias)
        for node in comb_nl.nodes.values():
            node.inputs = [q_aliases.get(sig, sig) for sig in node.inputs]
        comb_nl.dffs.clear()

        tmp_dir = tempfile.mkdtemp(
            prefix="signal_constant_", dir=_workspace_temp_dir()
        )
        netlist_path = os.path.join(tmp_dir, "netlist.v")
        script_path = os.path.join(tmp_dir, "prove.ys")
        write_verilog(comb_nl, netlist_path)
        literal = f"{width}'d{constant}"
        script = textwrap.dedent(f"""\
            read_verilog {netlist_path}
            prep -top {comb_nl.module_name}
            sat -prove {signal_name} {literal} -verify -show-inputs -show {signal_name}
        """)
        with open(script_path, "w") as fh:
            fh.write(script)

        try:
            result = subprocess.run(
                [_yosys_binary(), "-s", script_path],
                capture_output=True,
                text=True,
                timeout=120,
                env=_temp_subprocess_env(),
            )
            combined = (result.stdout or "") + "\n" + (result.stderr or "")
            if result.returncode == 0:
                return {
                    "always_equal": True,
                    "signal": signal_name,
                    "value": constant,
                    "width": width,
                }
            lower = combined.lower()
            if "proof did fail" in lower or "model found" in lower:
                return {
                    "always_equal": False,
                    "signal": signal_name,
                    "value": constant,
                    "width": width,
                }
            raise RuntimeError(
                "Yosys constant-property check failed:\n" + combined[-3000:]
            )
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def check_design_equivalence(self) -> dict:
        """Prove the current netlist equivalent to the originally loaded design."""
        import shutil
        import textwrap

        self._require_netlist()
        nl = self._netlist
        assert nl is not None
        original_path = self._original_netlist_path
        if not original_path:
            raise ValueError("No original design path is available. Call read_design first.")
        if not os.path.exists(original_path):
            raise FileNotFoundError(f"Original netlist not found: {original_path!r}")

        original_top = parse_verilog(original_path).module_name
        tmp_dir = tempfile.mkdtemp(
            prefix="design_equiv_", dir=_workspace_temp_dir()
        )
        current_path = os.path.join(tmp_dir, "current.v")
        dff_path = os.path.join(tmp_dir, "dff.v")
        script_path = os.path.join(tmp_dir, "equiv.ys")
        log_file = tempfile.NamedTemporaryFile(
            "w",
            prefix="design_equiv_",
            suffix=".log",
            dir=_workspace_temp_dir(),
            delete=False,
        )
        log_path = log_file.name
        log_file.close()

        write_verilog(nl, current_path)
        with open(dff_path, "w") as fh:
            fh.write(textwrap.dedent("""\
                module dff (CK, RN, SN, D, Q);
                  input CK, RN, SN, D;
                  output reg Q;
                  always @(posedge CK or negedge RN or negedge SN) begin
                    if (!RN) Q <= 1'b0;
                    else if (!SN) Q <= 1'b1;
                    else Q <= D;
                  end
                endmodule
            """))

        script = textwrap.dedent(f"""\
            read_verilog -sv {dff_path} {original_path}
            hierarchy -top {original_top}
            proc
            async2sync
            flatten
            opt_clean
            rename {original_top} gold
            design -stash gold

            read_verilog -sv {dff_path} {current_path}
            hierarchy -top {nl.module_name}
            proc
            async2sync
            flatten
            opt_clean
            rename {nl.module_name} gate
            design -stash gate

            design -copy-from gold -as gold gold
            design -copy-from gate -as gate gate
            equiv_make gold gate equiv
            hierarchy -top equiv
            clean -purge
            equiv_simple
            equiv_induct -seq 12
            equiv_status -assert
        """)
        with open(script_path, "w") as fh:
            fh.write(script)

        try:
            try:
                result = subprocess.run(
                    [_yosys_binary(), "-s", script_path],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    env=_temp_subprocess_env(),
                )
            except subprocess.TimeoutExpired as exc:
                with open(log_path, "w") as fh:
                    fh.write(script)
                    fh.write("\n\nYosys equivalence timed out after 300 seconds.\n")
                    if exc.stdout:
                        fh.write(str(exc.stdout))
                    if exc.stderr:
                        fh.write(str(exc.stderr))
                return {
                    "equivalent": False,
                    "status": "TIMEOUT",
                    "original_netlist": original_path,
                    "log_path": log_path,
                }

            combined = (result.stdout or "") + "\n" + (result.stderr or "")
            with open(log_path, "w") as fh:
                fh.write("Yosys script:\n")
                fh.write(script)
                fh.write("\nYosys output:\n")
                fh.write(combined)

            equivalent = result.returncode == 0
            status = "PASS" if equivalent else (
                "FAIL" if "unproven $equiv" in combined.lower() else "ERROR"
            )
            return {
                "equivalent": equivalent,
                "status": status,
                "original_netlist": original_path,
                "log_path": log_path,
            }
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def find_instances_by_name_pattern(
        self, gate_type: str, name_pattern: str
    ) -> List[str]:
        """Return instance names matching gate_type and name_pattern regex.

        Pass an empty string for gate_type to match all gate types.

        Args:
            gate_type:    Gate type filter (e.g. "buf"), or "" for any.
            name_pattern: Python regex applied to instance names.
        """
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        try:
            rx = re.compile(name_pattern)
        except re.error as exc:
            raise ValueError(f"Invalid regex {name_pattern!r}: {exc}") from exc

        results: List[str] = []
        for inst_name, node in nl.nodes.items():
            type_match = (not gate_type) or node.gate_type == gate_type.lower()
            if type_match and rx.search(inst_name):
                results.append(inst_name)
        return results

    # ==================================================================
    # TRANSFORMATION OPERATIONS
    # ==================================================================

    def insert_gate_before(
        self, target_instance: str, gate_type: str, extra_input: str
    ) -> str:
        """Insert a new gate before target_instance.

        The original driver of target_instance is wired into input[0] of the
        new gate; extra_input is wired into input[1].  The new gate's output
        replaces the original signal feeding target_instance.

        Args:
            target_instance: Existing gate to insert before.
            gate_type:       Type of the new gate to insert.
            extra_input:     Second input signal for the new gate.

        Returns:
            The instance name of the newly created gate.
        """
        self._require_netlist()
        nl = self._netlist
        assert nl is not None
        self._resolve_instance(target_instance)

        gate_type = gate_type.lower()
        if gate_type not in TWO_INPUT_GATES:
            raise ValueError(
                f"insert_gate_before requires a 2-input gate, got {gate_type!r}"
            )

        target = nl.nodes[target_instance]
        original_input = target.inputs[0]

        new_wire = self._next_wire_name("ins_w")
        new_inst = self._next_inst_name("ins_g")

        self._add_wire(new_wire)
        new_gate = GateNode(
            name=new_inst,
            gate_type=gate_type,
            inputs=[original_input, extra_input],
            output=new_wire,
        )
        nl.nodes[new_inst] = new_gate
        target.inputs[0] = new_wire

        self.remove_dangling_gates()
        return new_inst

    def replace_gate(
        self,
        instance_name: str,
        new_gate_type: str,
        extra_input: Optional[str] = None,
    ) -> None:
        """Replace the gate type of instance_name in-place.

        When upgrading from a 1-input gate (buf/not) to a 2-input gate,
        supply *extra_input* to provide the second input signal.  Raises
        ValueError if the port counts are incompatible and no extra_input
        is provided.

        Args:
            instance_name: Existing gate instance to replace.
            new_gate_type: New gate type string.
            extra_input:   Optional second input signal when changing from
                           a 1-input gate to a 2-input gate.
        """
        self._require_netlist()
        nl = self._netlist
        assert nl is not None
        self._resolve_instance(instance_name)

        new_gate_type = new_gate_type.lower()
        if new_gate_type not in PRIMITIVE_GATES:
            raise ValueError(f"Unknown gate type: {new_gate_type!r}")

        node = nl.nodes[instance_name]
        old_in_count = len(node.inputs)
        new_in_count = 1 if new_gate_type in ONE_INPUT_GATES else 2

        if old_in_count == new_in_count:
            node.gate_type = new_gate_type
        elif old_in_count < new_in_count:
            # Upgrading input count — require extra_input
            if extra_input is None:
                raise ValueError(
                    f"Gate type {new_gate_type!r} requires {new_in_count} input(s), "
                    f"but {instance_name!r} has {old_in_count}. "
                    "Provide 'extra_input' to supply the additional signal."
                )
            node.gate_type = new_gate_type
            while len(node.inputs) < new_in_count:
                node.inputs.append(extra_input)
            # Register extra_input as a known signal if needed
            if extra_input not in nl.wires and extra_input not in nl.primary_inputs:
                self._add_wire(extra_input)
        else:
            # Downgrading input count — drop extra inputs
            node.gate_type = new_gate_type
            node.inputs = node.inputs[:new_in_count]

    def insert_buffer(self, net_name: str, after_gate: str) -> str:
        """Insert a buf on net_name after after_gate.

        Args:
            net_name:   Net to buffer.
            after_gate: Gate instance that drives net_name.

        Returns:
            The new buffer instance name.
        """
        self._require_netlist()
        nl = self._netlist
        assert nl is not None
        self._resolve_signal(net_name)
        self._resolve_instance(after_gate)

        new_wire = self._next_wire_name("buf_w")
        buf_inst = self._next_inst_name("buf_g")

        self._add_wire(new_wire)

        # Rename original net to new_wire in after_gate's output
        if after_gate in nl.nodes:
            nl.nodes[after_gate].output = new_wire
        else:
            nl.dffs[after_gate].q = new_wire

        # Create the buffer
        buf_node = GateNode(
            name=buf_inst,
            gate_type="buf",
            inputs=[new_wire],
            output=net_name,
        )
        nl.nodes[buf_inst] = buf_node
        return buf_inst

    def insert_buffers_for_fanout(self, net_name: str, max_fanout: int) -> int:
        """Insert buffer trees so no net has fanout > max_fanout.

        Args:
            net_name:   Net to examine and buffer.
            max_fanout: Maximum allowed fanout per net.

        Returns:
            Number of buffers inserted.
        """
        self._require_netlist()
        self._resolve_signal(net_name)
        if max_fanout < 1:
            raise ValueError("max_fanout must be >= 1")

        nl = self._netlist
        assert nl is not None

        total_inserted = 0
        # Build list of nets that still need splitting
        nets_to_process = [net_name]

        while nets_to_process:
            cur_net = nets_to_process.pop()
            fanout_list = self._build_fanout_map().get(cur_net, [])
            if len(fanout_list) <= max_fanout:
                continue

            # Split into groups of max_fanout
            groups = [
                fanout_list[i : i + max_fanout]
                for i in range(0, len(fanout_list), max_fanout)
            ]

            for grp in groups[1:]:  # first group keeps original net
                new_wire = self._next_wire_name("fo_w")
                buf_inst = self._next_inst_name("fo_buf")
                self._add_wire(new_wire)
                buf_node = GateNode(
                    name=buf_inst,
                    gate_type="buf",
                    inputs=[cur_net],
                    output=new_wire,
                )
                nl.nodes[buf_inst] = buf_node
                total_inserted += 1

                # Reconnect group's consumers to new_wire
                for consumer_inst in grp:
                    if consumer_inst in nl.nodes:
                        node = nl.nodes[consumer_inst]
                        node.inputs = [
                            new_wire if s == cur_net else s for s in node.inputs
                        ]
                    elif consumer_inst in nl.dffs:
                        dff = nl.dffs[consumer_inst]
                        if dff.d == cur_net:
                            dff.d = new_wire
                        if dff.ck == cur_net:
                            dff.ck = new_wire
                        if dff.rn == cur_net:
                            dff.rn = new_wire
                        if dff.sn == cur_net:
                            dff.sn = new_wire
                # New buffer net may still have high fanout — queue it
                nets_to_process.append(new_wire)

        return total_inserted

    def balance_depth(self, source: str, sinks: List[str]) -> int:
        """Add buffers to equalise path lengths from source to all sinks.

        Uses minimal buffer insertion.

        Args:
            source: Starting signal.
            sinks:  List of sink signal names.

        Returns:
            Number of buffers inserted.
        """
        self._require_netlist()
        self._resolve_signal(source)
        for s in sinks:
            self._resolve_signal(s)

        nl = self._netlist
        assert nl is not None

        depths: Dict[str, int] = {}
        for sink in sinks:
            d, _ = self.get_max_depth(source, sink)
            depths[sink] = d

        valid_depths = [d for d in depths.values() if d >= 0]
        if not valid_depths:
            return 0

        target_depth = max(valid_depths)
        total_inserted = 0

        for sink, depth in depths.items():
            if depth < 0 or depth == target_depth:
                continue
            bufs_needed = target_depth - depth
            out2gate = self._build_output_to_gate()
            driver_inst = out2gate.get(sink)
            cur_net = sink
            for _ in range(bufs_needed):
                if driver_inst:
                    new_buf = self.insert_buffer(cur_net, driver_inst)
                    total_inserted += 1
                    # After insert_buffer, original driver now drives a new net
                    # and buf drives cur_net; the buf's input is the new net
                    buf_node = nl.nodes[new_buf]
                    driver_inst = new_buf
                    cur_net = buf_node.output

        return total_inserted

    def rename_gate(self, old_name: str, new_name: str) -> None:
        """Rename a gate instance from old_name to new_name.

        Raises ValueError if old_name does not exist or new_name is already taken.
        """
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        if old_name not in nl.nodes and old_name not in nl.dffs:
            raise ValueError(f"Instance '{old_name}' not found in netlist.")
        if new_name in nl.nodes or new_name in nl.dffs:
            raise ValueError(f"Instance name '{new_name}' already exists in netlist.")

        if old_name in nl.nodes:
            nl.nodes[new_name] = nl.nodes.pop(old_name)
            nl.nodes[new_name].name = new_name
        else:
            nl.dffs[new_name] = nl.dffs.pop(old_name)
            nl.dffs[new_name].name = new_name

    def _replace_signal_refs(
        self,
        old_name: str,
        new_name: str,
        *,
        include_drivers: bool = False,
    ) -> None:
        """Replace signal references across gate and DFF connectivity."""
        assert self._netlist is not None

        def rename(sig: str) -> str:
            if sig == old_name:
                return new_name
            if "[" in sig:
                return re.sub(rf"^{re.escape(old_name)}(?=\[)", new_name, sig)
            return sig

        for node in self._netlist.nodes.values():
            node.inputs = [rename(sig) for sig in node.inputs]
            if include_drivers:
                node.output = rename(node.output)

        for dff in self._netlist.dffs.values():
            dff.ck = rename(dff.ck)
            dff.rn = rename(dff.rn)
            dff.sn = rename(dff.sn)
            dff.d = rename(dff.d)
            dff.q = rename(dff.q)

    def rename_wire(self, old_name: str, new_name: str) -> None:
        """Rename a wire/signal and update all references."""
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        if old_name not in nl.wires:
            raise ValueError(f"Wire '{old_name}' not found in netlist.")
        if new_name in nl.wires:
            raise ValueError(f"Wire name '{new_name}' already exists in netlist.")
        if new_name in nl.nodes or new_name in nl.dffs:
            raise ValueError(f"Name '{new_name}' is already used by an instance.")

        wi = nl.wires.pop(old_name)
        wi.name = new_name
        nl.wires[new_name] = wi
        nl.primary_inputs = [new_name if n == old_name else n for n in nl.primary_inputs]
        nl.primary_outputs = [new_name if n == old_name else n for n in nl.primary_outputs]
        self._replace_signal_refs(old_name, new_name, include_drivers=True)

    def _restore_dff_boundary_nets(
        self,
        original_dffs: Dict[str, "DFFNode"],
        original_wires: Dict[str, WireInfo],
    ) -> int:
        """Keep DFF port net names stable after whole-design ABC optimization."""
        nl = self._netlist
        assert nl is not None
        inserted_buffers = 0

        def ensure_wire(sig: str) -> None:
            if sig in {"", "1'b0", "1'b1"}:
                return
            base_m = re.match(r"^(\w+)\[", sig)
            key = base_m.group(1) if base_m else sig
            if key in nl.wires:
                return
            if key in original_wires:
                nl.wires[key] = copy.deepcopy(original_wires[key])
            else:
                nl.wires[key] = WireInfo(name=key)

        def unique_inst(base: str) -> str:
            candidate = base
            idx = 0
            while candidate in nl.nodes or candidate in nl.dffs:
                idx += 1
                candidate = f"{base}_{idx}"
            return candidate

        driven = {node.output for node in nl.nodes.values()}

        for inst_name, original in original_dffs.items():
            current = nl.dffs.get(inst_name)
            if current is None:
                continue

            if current.q and current.q != original.q:
                self._replace_signal_refs(current.q, original.q, include_drivers=False)
                current.q = original.q
                ensure_wire(original.q)

            current.ck = original.ck
            current.rn = original.rn
            current.sn = original.sn
            ensure_wire(current.ck)
            ensure_wire(current.rn)
            ensure_wire(current.sn)

            if current.d != original.d:
                optimized_d = current.d
                ensure_wire(original.d)
                if original.d not in driven and optimized_d not in {"", original.d}:
                    inst = unique_inst(f"_keep_dff_d_{inst_name}")
                    nl.nodes[inst] = GateNode(
                        name=inst,
                        gate_type="buf",
                        inputs=[optimized_d],
                        output=original.d,
                    )
                    driven.add(original.d)
                    inserted_buffers += 1
                current.d = original.d

        return inserted_buffers

    def remove_dangling_gates(self) -> int:
        """Remove all gates and nets that do not feed any primary output or DFF input.

        Returns:
            Number of gates removed.
        """
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        # Collect all signals that are "needed"
        needed_signals: Set[str] = set(nl.primary_outputs)
        for dff in nl.dffs.values():
            needed_signals.add(dff.d)
            if dff.ck:
                needed_signals.add(dff.ck)
            if dff.rn:
                needed_signals.add(dff.rn)
            if dff.sn:
                needed_signals.add(dff.sn)

        # Expand bus PO names (e.g. "n16") to their actual bit-slice drivers
        # (e.g. "n16[0]"..."n16[7]") so the BFS reaches those gates.
        out2gate = self._build_output_to_gate()
        expanded_needed: Set[str] = set()
        for sig in needed_signals:
            if sig in out2gate:
                expanded_needed.add(sig)
            else:
                for k in out2gate:
                    if k.split('[')[0] == sig:
                        expanded_needed.add(k)
                if sig not in expanded_needed:
                    expanded_needed.add(sig)  # keep as-is (PI or constant)

        # Backward BFS: find all gate outputs that transitively feed needed signals
        useful_outputs: Set[str] = set()
        queue: deque[str] = deque(expanded_needed)

        while queue:
            sig = queue.popleft()
            if sig in useful_outputs or sig in nl.primary_inputs:
                continue
            if sig in {"1'b0", "1'b1"}:
                continue
            useful_outputs.add(sig)
            driver = out2gate.get(sig)
            if driver and driver in nl.nodes:
                gate = nl.nodes[driver]
                for inp in gate.inputs:
                    if inp not in useful_outputs:
                        queue.append(inp)

        # Remove gates whose output is not useful
        to_remove = [
            inst
            for inst, node in nl.nodes.items()
            if node.output not in useful_outputs
            and node.output not in nl.primary_outputs
        ]
        for inst in to_remove:
            del nl.nodes[inst]

        # Remove orphan internal wires
        used_wires: Set[str] = set()
        for node in nl.nodes.values():
            used_wires.add(node.output)
            used_wires |= set(node.inputs)
        for dff in nl.dffs.values():
            extra = {dff.d, dff.q}
            if dff.ck:
                extra.add(dff.ck)
            if dff.rn:
                extra.add(dff.rn)
            if dff.sn:
                extra.add(dff.sn)
            used_wires.update(extra)

        # Also keep bus base names (e.g. "n16") when only bit-slices
        # (e.g. "n16[7]") appear in used_wires — otherwise write_verilog
        # won't emit the bus declaration and Yosys sees out-of-bounds selects.
        used_bus_bases: Set[str] = set()
        for sig in used_wires:
            m = re.match(r'^(\w+)\[', sig)
            if m:
                used_bus_bases.add(m.group(1))

        declared = set(nl.primary_inputs) | set(nl.primary_outputs)
        nl.wires = {
            k: v
            for k, v in nl.wires.items()
            if k in declared or k in used_wires or k in used_bus_bases
        }

        return len(to_remove)

    def optimize_cone_depth(self, output_signal: str, max_depth: int) -> bool:
        """Restructure the logic cone of output_signal so its depth ≤ max_depth.

        Uses a greedy rebalancing strategy (associativity / commutativity).
        Preserves functional equivalence.

        Args:
            output_signal: Target output net.
            max_depth:     Maximum allowed combinational depth.

        Returns:
            True if the depth constraint is met after transformation, False otherwise.
        """
        self._require_netlist()
        self._resolve_signal(output_signal)

        # Save snapshot for equivalence check
        self.add_snapshot("pre_opt")

        nl = self._netlist
        assert nl is not None
        out2gate = self._build_output_to_gate()

        # Collect all inputs to the cone
        cone = self.get_logic_cone(output_signal)
        current_depth, _ = self.get_max_depth(
            _find_cone_primary_input(nl, output_signal, out2gate) or output_signal,
            output_signal,
        )
        if current_depth <= max_depth:
            return True

        # Simple rebalancing: flatten trees of associative gates then rebuild
        rebuilt = _rebalance_associative_tree(nl, output_signal, out2gate, max_depth)
        if not rebuilt:
            return False

        # Verify equivalence
        if not self.check_signal_equivalence(output_signal, "pre_opt"):
            self.restore_snapshot("pre_opt")
            return False

        self.remove_dangling_gates()
        return True

    def reduce_critical_path(
        self,
        allowed_gates: Optional[List[str]] = None,
    ) -> dict:
        """Reduce the maximum combinational depth of the loaded netlist using
        Yosys + ABC logic restructuring (no retiming, no DFF movement).

        Workflow:
          1. Measure depth before via get_fanin_cone_depth on each primary output.
          2. Write netlist to a temp Verilog file.
          3. Generate a unit-delay Liberty file for the requested gate set (or
             the active whole-design constraint) and a DFF blackbox so ABC
             never touches sequential elements.
          4. Run Yosys (subprocess) with ABC -liberty and a custom script that
             runs &dch -f (deep combinational restructuring) but omits dretime
             and scorr (both of which alter sequential boundaries).
          5. Parse the ABC-remapped Verilog back into self._netlist.
          6. Measure depth after and return a comparison dict.

        Returns:
            dict with keys depth_before, depth_after, improvement, success.

        Raises:
            RuntimeError: if Yosys is not found or the run fails.
        """
        import os
        import textwrap

        self._require_netlist()
        nl = self._netlist
        assert nl is not None
        valid_gates = {"nand", "nor", "or", "and", "not", "xor", "xnor", "buf"}
        requested_gates = allowed_gates or self._allowed_gates_constraint
        effective_allowed: Optional[List[str]] = None
        if requested_gates:
            effective_allowed = list(dict.fromkeys(g.lower() for g in requested_gates))
            bad = sorted(set(effective_allowed) - valid_gates)
            if bad:
                raise ValueError(f"Unknown gate types in allowed_gates: {bad}")
            if not effective_allowed:
                raise ValueError("allowed_gates cannot be empty.")

        original_netlist = copy.deepcopy(nl)
        original_dffs = copy.deepcopy(nl.dffs)
        original_wires = copy.deepcopy(nl.wires)

        # ------------------------------------------------------------------
        # Step 1 — measure depth BEFORE
        # Iterate over gate node outputs (always concrete scalar signals) so
        # bus primary outputs like n5[0]..n5[15] are handled correctly.
        # Using a shared memo dict avoids redundant re-computation.
        # ------------------------------------------------------------------
        memo_before: Dict[str, int] = {}
        depth_before = max(
            (self._fanin_depth(node.output, memo_before) for node in nl.nodes.values()),
            default=0,
        )

        # ------------------------------------------------------------------
        # Step 2 — write current netlist to temp input file
        # ------------------------------------------------------------------
        tmp_dir = tempfile.mkdtemp(prefix="yosys_opt_", dir=_workspace_temp_dir())
        tmp_input = os.path.join(tmp_dir, "input.v")
        tmp_output = os.path.join(tmp_dir, "output.v")
        tmp_lib = os.path.join(tmp_dir, "prim.lib")
        tmp_bb = os.path.join(tmp_dir, "dff_blackbox.v")
        tmp_script = os.path.join(tmp_dir, "run.ys")

        write_verilog(nl, tmp_input)

        # ------------------------------------------------------------------
        # Step 3a — DFF blackbox (prevents ABC from touching flip-flops)
        # ------------------------------------------------------------------
        with open(tmp_bb, "w") as fh:
            fh.write(textwrap.dedent("""\
                (* blackbox *)
                module dff (CK, RN, SN, D, Q);
                  input CK, RN, SN, D;
                  output Q;
                endmodule
            """))

        # ------------------------------------------------------------------
        # Step 3b — unit-delay Liberty file for primitive gates
        # All cells have identical delay so ABC minimises depth purely.
        # ------------------------------------------------------------------
        def _cell(name: str, func: str, inputs: List[str]) -> str:
            input_decls = "\n".join(
                f'    pin({p}) {{ direction : input; }}'  for p in inputs
            )
            timing_pins = " ".join(inputs)
            return textwrap.dedent(f"""\
              cell({name}) {{
            {input_decls}
                pin(Y) {{
                  direction : output;
                  function : "{func}";
                  timing() {{
                    related_pin : "{timing_pins}";
                    cell_rise(scalar)   {{ values("1.0"); }}
                    cell_fall(scalar)   {{ values("1.0"); }}
                    rise_transition(scalar) {{ values("0.0"); }}
                    fall_transition(scalar) {{ values("0.0"); }}
                  }}
                }}
              }}
            """)

        cell_specs = {
            "and":  ("A*B",     ["A", "B"]),
            "nand": ("!(A*B)",  ["A", "B"]),
            "nor":  ("!(A+B)",  ["A", "B"]),
            "or":   ("A+B",     ["A", "B"]),
            "xor":  ("A^B",     ["A", "B"]),
            "xnor": ("!(A^B)",  ["A", "B"]),
            "not":  ("!A",      ["A"]),
            "buf":  ("A",       ["A"]),
        }
        library_gates = effective_allowed or [
            "nand", "nor", "or", "xor", "xnor", "not", "buf"
        ]
        # ABC's delay mapper rejects libraries with fewer than three cell
        # classes. BUF is an internal mapping aid only; opt_clean removes it,
        # and the postcondition below still enforces the user-visible set.
        if effective_allowed and len(library_gates) < 3 and "buf" not in library_gates:
            library_gates = [*library_gates, "buf"]
        liberty_content = (
            'library(prim) {\n'
            '  time_unit : "1ns";\n'
            '  voltage_unit : "1V";\n'
            '  current_unit : "1mA";\n'
            '  leakage_power_unit : "1nW";\n'
            '  capacitive_load_unit(1, pf);\n'
            '  pulling_resistance_unit : "1kohm";\n'
            '  delay_model : generic_cmos;\n'
            + "".join(_cell(gate, *cell_specs[gate]) for gate in library_gates)
            + '}\n'
        )
        with open(tmp_lib, "w") as fh:
            fh.write(liberty_content)

        # ------------------------------------------------------------------
        # Step 4 — Yosys script
        # Custom ABC script removes dretime (retiming) and scorr (sequential
        # redundancy removal) from the Yosys default.  Only combinational
        # restructuring passes remain.
        # ------------------------------------------------------------------
        abc_script = "+strash;&get,-n;&fraig,-x;&put;dc2;strash;&get,-n;&dch,-f;&nf,{D};&put"
        abc_command = (
            f"abc -liberty {tmp_lib} -D 1"
            if effective_allowed
            else f'abc -liberty {tmp_lib} -script "{abc_script}"'
        )
        yosys_script = textwrap.dedent(f"""\
            read_verilog -lib {tmp_bb}
            read_verilog {tmp_input}
            hierarchy -check -top {nl.module_name}
            flatten
            techmap
            {abc_command}
            opt_clean -purge
            write_verilog -noattr -nohex {tmp_output}
        """)
        with open(tmp_script, "w") as fh:
            fh.write(yosys_script)

        try:
            result = subprocess.run(
                [_yosys_binary(), "-s", tmp_script],
                capture_output=True,
                text=True,
                timeout=300,
                env=_temp_subprocess_env(),
            )
        except FileNotFoundError:
            raise RuntimeError(
                "yosys not found. Install Yosys (apt install yosys) and retry."
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Yosys timed out after 300 s.")

        if result.returncode != 0:
            raise RuntimeError(
                f"Yosys failed (exit {result.returncode}).\n"
                f"STDOUT:\n{result.stdout[-3000:]}\n"
                f"STDERR:\n{result.stderr[-1000:]}"
            )

        if not os.path.exists(tmp_output):
            raise RuntimeError(
                "Yosys completed but output Verilog was not written.\n"
                f"STDOUT:\n{result.stdout[-3000:]}\n"
                f"STDERR:\n{result.stderr[-1000:]}"
            )

        # ------------------------------------------------------------------
        # Step 5 — parse ABC output back into Netlist object
        # ------------------------------------------------------------------
        from .netlist_parser import parse_verilog as _parse_verilog
        self._netlist = _parse_verilog(tmp_output)
        boundary_buffers = self._restore_dff_boundary_nets(original_dffs, original_wires)
        helper_buffers_lowered = 0

        if effective_allowed:
            # ABC needs BUF as a third internal cell class for two-gate
            # libraries, and DFF-boundary restoration may also add a BUF to
            # retain an original D net. Lower those wire helpers into the
            # requested gate set before enforcing the public postcondition.
            if "buf" not in effective_allowed and any(
                node.gate_type == "buf" for node in self._netlist.nodes.values()
            ):
                lowered = self.replace_gate_type_in_cone(
                    "buf",
                    effective_allowed,
                    output_signal=None,
                )
                if not lowered.get("success"):
                    self._netlist = original_netlist
                    raise RuntimeError(
                        "Could not lower optimization BUF helpers into the "
                        f"requested gate set {effective_allowed}: "
                        f"{lowered.get('reason', 'unknown mapping failure')}"
                    )
                helper_buffers_lowered = int(lowered.get("replaced", 0))

            remaining_types = sorted({
                node.gate_type
                for node in self._netlist.nodes.values()
                if node.gate_type not in set(effective_allowed)
            })
            if remaining_types:
                self._netlist = original_netlist
                raise RuntimeError(
                    "Restricted depth optimization produced disallowed gate types: "
                    f"{remaining_types}"
                )
            self._allowed_gates_constraint = effective_allowed

        # Clean up temp dir
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

        # ------------------------------------------------------------------
        # Step 6 — measure depth AFTER (same approach as before)
        # ------------------------------------------------------------------
        memo_after: Dict[str, int] = {}
        depth_after = max(
            (self._fanin_depth(node.output, memo_after) for node in self._netlist.nodes.values()),
            default=0,
        )

        return {
            "depth_before": depth_before,
            "depth_after": depth_after,
            "improvement": depth_before - depth_after,
            "success": depth_after < depth_before,
            "dff_boundary_buffers_inserted": boundary_buffers,
            "helper_buffers_lowered": helper_buffers_lowered,
            "allowed_gates": effective_allowed,
        }

    def remap_cone_with_gates(
        self,
        output_signal: str,
        allowed_gates: List[str],
    ) -> dict:
        """Re-implement the fanin cone of output_signal using only allowed_gates.

        Extracts the combinational cone of output_signal as a standalone module,
        runs Yosys+ABC with a restricted Liberty (only the allowed gate types),
        then splices the remapped cone back into the full netlist.

        Args:
            output_signal: Target net whose fanin cone is remapped (e.g. "n11[0]").
            allowed_gates: List of permitted gate types (e.g. ["nand", "not"]).

        Returns:
            dict with keys: success (bool), gates_before (int), gates_after (int),
            output_signal (str), allowed_gates (list), and on failure: reason (str).
        """
        import os, shutil, textwrap

        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        # Validate allowed_gates
        valid = {"nand", "nor", "or", "and", "not", "xor", "xnor", "buf"}
        bad = [g for g in allowed_gates if g not in valid]
        if bad:
            return {"success": False, "reason": f"Unknown gate types: {bad}"}

        # ------------------------------------------------------------------
        # Step 1 — BFS backward to collect cone gate instances + boundary inputs
        # If output_signal is a DFF Q pin, we remap the cone feeding its D input
        # instead (since Q itself has no combinational fanin).
        # ------------------------------------------------------------------
        out2gate = self._build_output_to_gate()

        # Check if output_signal is a DFF Q — if so, resolve to its D input
        dff_q_to_d: Dict[str, str] = {dff.q: dff.d for dff in nl.dffs.values()}
        actual_output = output_signal
        if output_signal in dff_q_to_d:
            actual_output = dff_q_to_d[output_signal]

        # Resolve bit-slice: if actual_output is still not driven by a comb gate
        if actual_output not in out2gate:
            if output_signal == nl.module_name:
                return {
                    "success": False,
                    "reason": (
                        f"'{output_signal}' is the module name, not a driven signal. "
                        "Use remap_design_with_gates for a whole-design reconstruction."
                    ),
                }
            return {
                "success": False,
                "reason": (
                    f"Signal '{output_signal}' (resolved to '{actual_output}') "
                    f"has no driving combinational gate in netlist."
                ),
            }

        pi_set = set(nl.primary_inputs)
        dff_outputs = {dff.q for dff in nl.dffs.values()}

        cone_insts: Set[str] = set()
        boundary_inputs: Set[str] = set()
        visited: Set[str] = set()
        queue = [actual_output]

        while queue:
            sig = queue.pop()
            if sig in visited:
                continue
            visited.add(sig)

            if sig in {"1'b0", "1'b1"} or sig in pi_set or sig in dff_outputs:
                boundary_inputs.add(sig)
                continue

            driver = out2gate.get(sig)
            if driver is None or driver not in nl.nodes:
                boundary_inputs.add(sig)
                continue

            cone_insts.add(driver)
            queue.extend(nl.nodes[driver].inputs)

        # Boundary inputs = signals consumed by cone but produced outside
        # (Remove constants — they'll be inlined as literals)
        boundary_inputs.discard("1'b0")
        boundary_inputs.discard("1'b1")

        gates_before = len(cone_insts)
        if gates_before == 0:
            return {"success": False, "reason": "Cone has no remappable gates."}

        # Prefer local, output-preserving substitutions. Keeping each original
        # gate output as a proof point is friendlier to equiv_make/equiv_simple
        # than replacing a large sequential boundary cone in one shot, and it
        # also leaves shared fanout logic untouched.
        allowed_set = set(allowed_gates)
        source_types = sorted({
            nl.nodes[inst].gate_type
            for inst in cone_insts
            if nl.nodes[inst].gate_type not in allowed_set
        })
        templates = {
            source_type: self._get_substitution_template(source_type, allowed_gates)
            for source_type in source_types
        }
        if all(template is not None for template in templates.values()):
            replaced = 0
            for source_type in source_types:
                result = self.replace_gate_type_in_cone(
                    source_type,
                    allowed_gates,
                    output_signal,
                )
                if not result.get("success"):
                    return result
                replaced += int(result.get("replaced", 0))

            remapped_out2gate = self._build_output_to_gate()
            remapped_q_to_d = {dff.q: dff.d for dff in nl.dffs.values()}
            remapped_output = remapped_q_to_d.get(output_signal, output_signal)
            remapped_insts: Set[str] = set()
            seen_signals: Set[str] = set()
            stack = [remapped_output]
            while stack:
                sig = stack.pop()
                if sig in seen_signals:
                    continue
                seen_signals.add(sig)
                driver = remapped_out2gate.get(sig)
                if driver is None or driver not in nl.nodes or driver in remapped_insts:
                    continue
                remapped_insts.add(driver)
                stack.extend(nl.nodes[driver].inputs)

            return {
                "success": True,
                "output_signal": output_signal,
                "allowed_gates": allowed_gates,
                "gates_before": gates_before,
                "gates_after": len(remapped_insts),
                "gates_replaced": replaced,
                "strategy": "local_substitution",
            }

        # ------------------------------------------------------------------
        # Step 2 — Write cone as standalone Verilog submodule
        # Wire names with '[' are illegal as port names → sanitise to '_'
        # Keep a map so we can unsanitise after ABC.
        # ------------------------------------------------------------------
        def sanitise(name: str) -> str:
            return re.sub(r'[\[\]]', '_', name)

        # Build sanitise maps (check for collisions — very unlikely but safe)
        san_map: Dict[str, str] = {}   # original → sanitised
        rev_map: Dict[str, str] = {}   # sanitised → original
        all_signals: set = boundary_inputs | {actual_output}
        for sig in all_signals:
            s = sanitise(sig)
            # Ensure uniqueness
            base = s
            idx = 0
            while s in rev_map and rev_map[s] != sig:
                s = f"{base}_{idx}"
                idx += 1
            san_map[sig] = s
            rev_map[s] = sig

        # Sanitise internal cone wire names too
        cone_wires: set = set()
        for inst in cone_insts:
            node = nl.nodes[inst]
            cone_wires.add(node.output)
            cone_wires.update(node.inputs)
        # Internal = in cone but not boundary and not output
        internal_wires = cone_wires - boundary_inputs - {actual_output} - {"1'b0", "1'b1"}
        for sig in internal_wires:
            s = sanitise(sig)
            base = s
            idx = 0
            while s in rev_map and rev_map[s] != sig:
                s = f"{base}_{idx}"
                idx += 1
            san_map[sig] = s
            rev_map[s] = sig

        def s(sig: str) -> str:
            """Return sanitised name, or original if not in map (constant)."""
            return san_map.get(sig, sig)

        sorted_bi = sorted(boundary_inputs)
        out_san = s(actual_output)

        sub_lines: List[str] = []
        port_list = ", ".join(sorted_bi + [actual_output])
        port_san  = ", ".join([s(p) for p in sorted_bi] + [out_san])
        sub_lines.append(f"module cone_top ({port_san});")
        for bi in sorted_bi:
            sub_lines.append(f"  input wire {s(bi)};")
        sub_lines.append(f"  output wire {out_san};")

        # Declare internal wires
        for w in sorted(internal_wires):
            sub_lines.append(f"  wire {s(w)};")
        sub_lines.append("")

        # Gate instances — positional style
        for inst in sorted(cone_insts):
            node = nl.nodes[inst]
            if node.gate_type in ONE_INPUT_GATES:
                ports = f"{s(node.output)}, {s(node.inputs[0])}"
            else:
                ports = f"{s(node.output)}, {s(node.inputs[0])}, {s(node.inputs[1])}"
            sub_lines.append(f"  {node.gate_type} {inst} ({ports});")

        sub_lines.append("endmodule")

        tmp_dir = tempfile.mkdtemp(prefix="yosys_cone_", dir=_workspace_temp_dir())
        sub_v   = os.path.join(tmp_dir, "cone.v")
        out_v   = os.path.join(tmp_dir, "cone_out.v")
        lib_f   = os.path.join(tmp_dir, "restricted.lib")
        script_f = os.path.join(tmp_dir, "run.ys")

        with open(sub_v, "w") as fh:
            fh.write("\n".join(sub_lines) + "\n")

        # ------------------------------------------------------------------
        # Step 3 — restricted Liberty: only allowed_gates get cell entries
        # ------------------------------------------------------------------
        def _cell(name: str, func: str, inputs: List[str]) -> str:
            input_decls = "\n".join(
                f'    pin({p}) {{ direction : input; }}' for p in inputs
            )
            timing_pins = " ".join(inputs)
            return textwrap.dedent(f"""\
              cell({name}) {{
            {input_decls}
                pin(Y) {{
                  direction : output;
                  function : "{func}";
                  timing() {{
                    related_pin : "{timing_pins}";
                    cell_rise(scalar)   {{ values("1.0"); }}
                    cell_fall(scalar)   {{ values("1.0"); }}
                    rise_transition(scalar) {{ values("0.0"); }}
                    fall_transition(scalar) {{ values("0.0"); }}
                  }}
                }}
              }}
            """)

        all_cells = {
            "nand":  ("!(A*B)",  ["A", "B"]),
            "nor":   ("!(A+B)",  ["A", "B"]),
            "or":    ("A+B",     ["A", "B"]),
            "xor":   ("A^B",     ["A", "B"]),
            "xnor":  ("!(A^B)",  ["A", "B"]),
            "not":   ("!A",      ["A"]),
            "buf":   ("A",       ["A"]),
        }
        lib_body = (
            'library(restricted) {\n'
            '  time_unit : "1ns";\n'
            '  voltage_unit : "1V";\n'
            '  current_unit : "1mA";\n'
            '  leakage_power_unit : "1nW";\n'
            '  capacitive_load_unit(1, pf);\n'
            '  pulling_resistance_unit : "1kohm";\n'
            '  delay_model : generic_cmos;\n'
        )
        for g in allowed_gates:
            if g in all_cells:
                func, ins = all_cells[g]
                lib_body += _cell(g, func, ins)
        # ABC's map command requires a buf cell for internal wiring even if the
        # user didn't request it.  We always include it; any generated buf
        # instances that aren't in allowed_gates are removed as trivial pass-throughs.
        if "buf" not in allowed_gates:
            func, ins = all_cells["buf"]
            lib_body += _cell("buf", func, ins)
        lib_body += '}\n'

        with open(lib_f, "w") as fh:
            fh.write(lib_body)

        yosys_script = textwrap.dedent(f"""\
            read_verilog {sub_v}
            hierarchy -check -top cone_top
            flatten
            techmap
            abc -liberty {lib_f} -D 1
            opt_clean -purge
            write_verilog -noattr -nohex {out_v}
        """)
        with open(script_f, "w") as fh:
            fh.write(yosys_script)

        try:
            result = subprocess.run(
                [_yosys_binary(), "-s", script_f],
                capture_output=True, text=True, timeout=300,
                env=_temp_subprocess_env(),
            )
        except FileNotFoundError:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise RuntimeError("yosys not found. Install Yosys and retry.")
        except subprocess.TimeoutExpired:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise RuntimeError("Yosys timed out after 300 s.")

        if result.returncode != 0 or not os.path.exists(out_v):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return {
                "success": False,
                "reason": (
                    f"Yosys failed (exit {result.returncode}).\n"
                    f"STDOUT:\n{result.stdout[-2000:]}\n"
                    f"STDERR:\n{result.stderr[-500:]}"
                ),
            }

        # ------------------------------------------------------------------
        # Step 4 — parse ABC output, unsanitise wire names, splice back
        # ------------------------------------------------------------------
        from .netlist_parser import parse_verilog as _parse_verilog
        cone_nl = _parse_verilog(out_v)
        shutil.rmtree(tmp_dir, ignore_errors=True)

        # Build a prefix for new intermediate wires to avoid collisions
        safe_out = re.sub(r'\W', '_', actual_output)
        prefix = f"_rc_{safe_out}_"

        # Collect existing wire names in full netlist to detect collisions
        existing_wires = set(nl.wires.keys())

        # Only ports keep their original names. ABC's internal signals must be
        # fresh because the original cone can contain shared nodes that remain
        # live for consumers outside this remapped output cone.
        wire_rename: Dict[str, str] = {}
        for orig in boundary_inputs | {actual_output}:
            wire_rename[san_map[orig]] = orig

        # New internal wires from ABC get prefixed names
        for wname, wi in cone_nl.wires.items():
            if wname not in wire_rename:
                candidate = prefix + wname
                # Ensure no collision
                while candidate in existing_wires:
                    candidate = candidate + "_"
                wire_rename[wname] = candidate
                existing_wires.add(candidate)

        def rw(sig: str) -> str:
            """Rename a signal from cone_nl namespace to full netlist namespace."""
            # Handle constants
            if sig in {"1'b0", "1'b1"}:
                return sig
            return wire_rename.get(sig, prefix + sig)

        # Disconnect only the old root. Keeping the rest temporarily preserves
        # side fanouts; remove_dangling_gates below prunes nodes that truly have
        # no remaining consumer after the duplicate cone is installed.
        old_root = out2gate[actual_output]
        nl.nodes.pop(old_root, None)

        # Add new gate instances from ABC-remapped cone
        gates_after = 0
        for inst_name, node in cone_nl.nodes.items():
            new_name = prefix + inst_name
            # Ensure no collision with existing instances
            while new_name in nl.nodes or new_name in nl.dffs:
                new_name = new_name + "_"
            new_node = GateNode(
                name=new_name,
                gate_type=node.gate_type,
                inputs=[rw(i) for i in node.inputs],
                output=rw(node.output),
            )
            nl.nodes[new_name] = new_node
            gates_after += 1

        # If buf was added only for ABC's internal needs (not in allowed_gates),
        # eliminate any generated buf instances by rewiring consumers directly
        # to the buf's input — buf(X) = X, so it's a pure pass-through.
        if "buf" not in allowed_gates:
            buf_insts = [n for n, nd in nl.nodes.items() if nd.gate_type == "buf"
                         and n.startswith(prefix)]
            if buf_insts:
                # Build output→signal map for quick substitution
                buf_out_to_in: Dict[str, str] = {
                    nl.nodes[n].output: nl.nodes[n].inputs[0] for n in buf_insts
                }
                # Rewire all gate inputs that consume a buf output
                for node in nl.nodes.values():
                    node.inputs = [buf_out_to_in.get(i, i) for i in node.inputs]
                for dff in nl.dffs.values():
                    dff.d  = buf_out_to_in.get(dff.d,  dff.d)
                    dff.ck = buf_out_to_in.get(dff.ck, dff.ck)
                    dff.rn = buf_out_to_in.get(dff.rn, dff.rn)
                    dff.sn = buf_out_to_in.get(dff.sn, dff.sn)
                # Remove buf instances
                for n in buf_insts:
                    nl.nodes.pop(n, None)
                    gates_after -= 1

        # Add new internal wires to nl.wires
        for wname, wi in cone_nl.wires.items():
            final_name = wire_rename.get(wname, prefix + wname)
            # Only add if it's a new internal wire (not a boundary / existing)
            if final_name not in nl.wires:
                nl.wires[final_name] = WireInfo(name=final_name)

        self.remove_dangling_gates()

        return {
            "success": True,
            "output_signal": output_signal,
            "allowed_gates": allowed_gates,
            "gates_before": gates_before,
            "gates_after": gates_after,
        }

    def remap_design_with_gates(self, allowed_gates: List[str]) -> dict:
        """Rebuild every combinational gate using only ``allowed_gates``.

        Uses local output-preserving templates so DFF boundaries, primary
        outputs, and shared fanout nets retain their original connectivity.
        """
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        valid = {"nand", "nor", "or", "and", "not", "xor", "xnor", "buf"}
        normalized = [gate.lower() for gate in allowed_gates]
        bad = sorted(set(normalized) - valid)
        if bad:
            return {"success": False, "reason": f"Unknown gate types: {bad}"}
        if not normalized:
            return {"success": False, "reason": "allowed_gates cannot be empty."}

        allowed_set = set(normalized)
        gates_before = len(nl.nodes)
        source_types = sorted({
            node.gate_type
            for node in nl.nodes.values()
            if node.gate_type not in allowed_set
        })

        unavailable = [
            source_type
            for source_type in source_types
            if self._get_substitution_template(source_type, normalized) is None
        ]
        if unavailable:
            return {
                "success": False,
                "reason": (
                    f"Cannot map gate types {unavailable} using only {normalized}. "
                    "The requested gate set may not be functionally complete."
                ),
            }

        replaced = 0
        for source_type in source_types:
            result = self.replace_gate_type_in_cone(
                source_type,
                normalized,
                output_signal=None,
            )
            if not result.get("success"):
                return result
            replaced += int(result.get("replaced", 0))

        remaining = sorted({
            node.gate_type
            for node in nl.nodes.values()
            if node.gate_type not in allowed_set
        })
        if remaining:
            return {
                "success": False,
                "reason": f"Disallowed gate types remain after remapping: {remaining}",
            }

        self._allowed_gates_constraint = normalized

        return {
            "success": True,
            "allowed_gates": normalized,
            "gates_before": gates_before,
            "gates_after": len(nl.nodes),
            "gates_replaced": replaced,
        }

    def fraig_merge_equivalent_gates(self) -> dict:
        """Merge functionally equivalent combinational nodes using ABC FRAIG.

        The ABC script intentionally contains only AIG construction, FRAIG,
        and the final technology map required to return gate instances to
        Yosys. It does not run dc2, dch, balancing, or retiming.
        """
        import os
        import shutil
        import textwrap

        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        original_netlist = copy.deepcopy(nl)
        original_dffs = copy.deepcopy(nl.dffs)
        original_wires = copy.deepcopy(nl.wires)
        gates_before = len(nl.nodes)

        valid = ["and", "nand", "or", "nor", "xor", "xnor", "not", "buf"]
        target_gates = self._allowed_gates_constraint or valid
        genlib_funcs = {
            "and":  "Y=A*B;    PIN A NONINV 1 1 1 1 1 1; PIN B NONINV 1 1 1 1 1 1",
            "nand": "Y=!(A*B); PIN A NONINV 1 1 1 1 1 1; PIN B NONINV 1 1 1 1 1 1",
            "or":   "Y=A+B;    PIN A NONINV 1 1 1 1 1 1; PIN B NONINV 1 1 1 1 1 1",
            "nor":  "Y=!(A+B); PIN A NONINV 1 1 1 1 1 1; PIN B NONINV 1 1 1 1 1 1",
            "xor":  "Y=A^B;    PIN A NONINV 1 1 1 1 1 1; PIN B NONINV 1 1 1 1 1 1",
            "xnor": "Y=!(A^B); PIN A NONINV 1 1 1 1 1 1; PIN B NONINV 1 1 1 1 1 1",
            "not":  "Y=!A;     PIN A NONINV 1 1 1 1 1 1",
            "buf":  "Y=A;      PIN A NONINV 1 1 1 1 1 1",
        }

        # ABC map needs an inverter and a wire cell even when they are not
        # user-visible members of a restricted gate set. They are lowered or
        # rejected after import.
        library_gates = list(dict.fromkeys([*target_gates, "not", "buf"]))
        tmp_dir = tempfile.mkdtemp(prefix="yosys_fraig_", dir=_workspace_temp_dir())
        input_v = os.path.join(tmp_dir, "input.v")
        output_v = os.path.join(tmp_dir, "output.v")
        dff_bb = os.path.join(tmp_dir, "dff_blackbox.v")
        genlib_f = os.path.join(tmp_dir, "fraig.genlib")
        script_f = os.path.join(tmp_dir, "run.ys")

        write_verilog(nl, input_v)
        with open(dff_bb, "w") as fh:
            fh.write(textwrap.dedent("""\
                (* blackbox *)
                module dff (CK, RN, SN, D, Q);
                  input CK, RN, SN, D;
                  output Q;
                endmodule
            """))
        with open(genlib_f, "w") as fh:
            for gate in library_gates:
                fh.write(f"GATE {gate} 1 {genlib_funcs[gate]};\n")

        # map is only the AIG-to-gate output encoding step. No other ABC
        # optimization commands are included after FRAIG.
        abc_script = "+strash;&get,-n;&fraig,-x;&put;map"
        yosys_script = textwrap.dedent(f"""\
            read_verilog -lib {dff_bb}
            read_verilog {input_v}
            hierarchy -check -top {nl.module_name}
            flatten
            techmap
            abc -genlib {genlib_f} -script "{abc_script}"
            opt_clean -purge
            write_verilog -noattr -nohex {output_v}
        """)
        with open(script_f, "w") as fh:
            fh.write(yosys_script)

        try:
            result = subprocess.run(
                [_yosys_binary(), "-s", script_f],
                capture_output=True,
                text=True,
                timeout=300,
                env=_temp_subprocess_env(),
            )
            if result.returncode != 0 or not os.path.exists(output_v):
                raise RuntimeError(
                    f"Yosys FRAIG failed (exit {result.returncode}).\n"
                    f"STDOUT:\n{result.stdout[-3000:]}\n"
                    f"STDERR:\n{result.stderr[-1000:]}"
                )

            self._netlist = parse_verilog(output_v)
            boundary_buffers = self._restore_dff_boundary_nets(original_dffs, original_wires)

            helpers_lowered = 0
            if self._allowed_gates_constraint and "buf" not in self._allowed_gates_constraint:
                lowered = self.replace_gate_type_in_cone(
                    "buf", self._allowed_gates_constraint, output_signal=None
                )
                if not lowered.get("success"):
                    raise RuntimeError(lowered.get("reason", "Could not lower FRAIG buffers."))
                helpers_lowered = int(lowered.get("replaced", 0))

            if self._allowed_gates_constraint:
                allowed_set = set(self._allowed_gates_constraint)
                remaining = sorted({
                    node.gate_type
                    for node in self._netlist.nodes.values()
                    if node.gate_type not in allowed_set
                })
                if remaining:
                    raise RuntimeError(
                        f"FRAIG produced disallowed gate types: {remaining}"
                    )

            gates_after = len(self._netlist.nodes)
            return {
                "success": True,
                "gates_before": gates_before,
                "gates_after": gates_after,
                "gate_reduction": gates_before - gates_after,
                "dff_boundary_buffers_inserted": boundary_buffers,
                "helper_buffers_lowered": helpers_lowered,
                "allowed_gates": self._allowed_gates_constraint,
                "abc_operations": ["strash", "fraig", "map"],
            }
        except Exception:
            self._netlist = original_netlist
            raise
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _get_substitution_template(
        self,
        source_type: str,
        target_types: List[str],
    ) -> Optional[dict]:
        """Compile (and cache) a gate substitution template via ABC.

        Builds a 1-gate dummy module of source_type, maps it with a
        restricted Liberty containing only target_types (+buf for ABC internals),
        and returns the resulting gate-list as a reusable template.

        Returns:
            dict with keys:
                inputs       : list of input port names  (e.g. ["A","B"])
                output       : output port name          (e.g. "Y")
                gates        : List[GateNode]  — template gates (ports as placeholders)
                internal_wires: List[str]      — new wire names needed per substitution
            Returns None if ABC cannot map source_type into target_types.
        """
        import os, shutil, textwrap

        cache_key = (source_type, frozenset(target_types))
        if cache_key in _SUBSTITUTION_TEMPLATE_CACHE:
            return _SUBSTITUTION_TEMPLATE_CACHE[cache_key]

        # ---- Hardcoded fallbacks for cases ABC cannot handle ----
        # ABC's map command requires an inverter cell; when 'not' is absent
        # from target_types, self-tied NAND/NOR are the only option and ABC
        # crashes. We provide these trivially-known templates directly.
        _hardcoded: Dict[Tuple, dict] = {
            # not → nand(A,A)
            ("not", frozenset({"nand"})): {
                "inputs": ["A"], "output": "Y",
                "gates": [GateNode("_t0", "nand", ["A", "A"], "Y")],
                "internal_wires": [],
            },
            # not → nor(A,A)
            ("not", frozenset({"nor"})): {
                "inputs": ["A"], "output": "Y",
                "gates": [GateNode("_t0", "nor", ["A", "A"], "Y")],
                "internal_wires": [],
            },
            # buf → not(not(A))
            ("buf", frozenset({"not"})): {
                "inputs": ["A"], "output": "Y",
                "gates": [
                    GateNode("_t0", "not", ["A"],    "_w0"),
                    GateNode("_t1", "not", ["_w0"],  "Y"),
                ],
                "internal_wires": ["_w0"],
            },
            # buf → nand(nand(A,A),nand(A,A))  [double inversion]
            ("buf", frozenset({"nand"})): {
                "inputs": ["A"], "output": "Y",
                "gates": [
                    GateNode("_t0", "nand", ["A",   "A"],    "_w0"),
                    GateNode("_t1", "nand", ["_w0", "_w0"],  "Y"),
                ],
                "internal_wires": ["_w0"],
            },
        }
        # Also support nand+not or nor+not for "not" target (just use not directly — no-op)
        # Actually mapping "not" to {"not"} is trivially identity — skip.

        hk = (source_type, frozenset(target_types))
        # Try exact match
        if hk in _hardcoded:
            _SUBSTITUTION_TEMPLATE_CACHE[cache_key] = _hardcoded[hk]
            return _hardcoded[hk]
        # Try subset match (e.g. target={"nand","not"} matches hardcoded {"nand"})
        for (hs, ht), tmpl in _hardcoded.items():
            if hs == source_type and ht.issubset(frozenset(target_types)):
                _SUBSTITUTION_TEMPLATE_CACHE[cache_key] = tmpl
                return tmpl

        # genlib format: each gate on one line — works with ABC's map command
        # and avoids the Liberty scalar-timing crash in abc -genlib path.
        genlib_funcs = {
            "nand": "Y=!(A*B); PIN A NONINV 1 1 1 1 1 1; PIN B NONINV 1 1 1 1 1 1",
            "nor":  "Y=!(A+B); PIN A NONINV 1 1 1 1 1 1; PIN B NONINV 1 1 1 1 1 1",
            "or":   "Y=A+B;    PIN A NONINV 1 1 1 1 1 1; PIN B NONINV 1 1 1 1 1 1",
            "and":  "Y=A*B;    PIN A NONINV 1 1 1 1 1 1; PIN B NONINV 1 1 1 1 1 1",
            "xor":  "Y=A^B;    PIN A NONINV 1 1 1 1 1 1; PIN B NONINV 1 1 1 1 1 1",
            "xnor": "Y=!(A^B); PIN A NONINV 1 1 1 1 1 1; PIN B NONINV 1 1 1 1 1 1",
            "not":  "Y=!A;     PIN A NONINV 1 1 1 1 1 1",
            "buf":  "Y=A;      PIN A NONINV 1 1 1 1 1 1",
        }

        is_one_input = source_type in ONE_INPUT_GATES

        tmp_dir   = tempfile.mkdtemp(prefix="abc_tmpl_", dir=_workspace_temp_dir())
        dummy_v   = os.path.join(tmp_dir, "dummy.v")
        out_blif  = os.path.join(tmp_dir, "out.blif")
        genlib_f  = os.path.join(tmp_dir, "tgt.genlib")
        yosys_f   = os.path.join(tmp_dir, "map.ys")
        process_tmp = os.path.join(tmp_dir, "process_tmp")
        os.makedirs(process_tmp, exist_ok=True)

        # ---- 1. Write dummy.v ----
        if is_one_input:
            dummy_src = f"module dummy (A, Y);\n  input A;\n  output Y;\n  {source_type} g0 (Y, A);\nendmodule\n"
        else:
            dummy_src = f"module dummy (A, B, Y);\n  input A, B;\n  output Y;\n  {source_type} g0 (Y, A, B);\nendmodule\n"
        with open(dummy_v, "w") as fh:
            fh.write(dummy_src)

        # ---- 2. Build genlib ----
        genlib_lines = []
        for g in target_types:
            if g in genlib_funcs:
                genlib_lines.append(f"GATE {g} 1 {genlib_funcs[g]};")
        # buf always included so ABC has a trivial pass-through available
        if "buf" not in target_types:
            genlib_lines.append(f"GATE buf 1 {genlib_funcs['buf']};")
        with open(genlib_f, "w") as fh:
            fh.write("\n".join(genlib_lines) + "\n")

        # ---- 3. Map through Yosys's ABC pass ----
        # Keep ABC process discovery inside Yosys instead of invoking the
        # yosys-abc executable directly from the framework.
        yosys_script = textwrap.dedent(f"""\
            read_verilog {dummy_v}
            hierarchy -check -top dummy
            flatten
            techmap
            abc -nocleanup -genlib {genlib_f} -script "+strash;dc2;map"
            opt_clean -purge
            write_blif {out_blif}
        """)
        with open(yosys_f, "w") as fh:
            fh.write(yosys_script)
        process_env = _temp_subprocess_env()
        process_env.update({"TMPDIR": process_tmp, "TEMP": process_tmp, "TMP": process_tmp})
        result = subprocess.run(
            [_yosys_binary(), "-s", yosys_f],
            capture_output=True,
            text=True,
            timeout=30,
            env=process_env,
        )
        if result.returncode != 0 or not os.path.exists(out_blif):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            _SUBSTITUTION_TEMPLATE_CACHE[cache_key] = None
            return None

        # ---- 4. Parse BLIF output directly (avoid Yosys round-trip) ----
        # BLIF .gate lines: .gate <type> <port>=<sig> ... <out_port>=<out>
        template = _parse_blif_template(out_blif, target_types, "buf" not in target_types)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        _SUBSTITUTION_TEMPLATE_CACHE[cache_key] = template
        return template

    def replace_gate_type_in_cone(
        self,
        source_type: str,
        target_types: List[str],
        output_signal: Optional[str] = None,
    ) -> dict:
        """Replace all source_type gates (in the cone of output_signal, or the
        whole design if output_signal is None) with an ABC-derived equivalent
        built from target_types only.

        Each matching gate is replaced individually using a precompiled template
        from ABC.  Non-matching gates are untouched.

        Args:
            source_type:   Gate type to replace (e.g. "or").
            target_types:  Allowed gate types in the replacement (e.g. ["nand","not"]).
            output_signal: Cone root signal.  None / "*" = whole design.

        Returns:
            dict with keys: success, replaced, skipped, source_type,
            target_types, output_signal.
        """
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        # ---- 1. Compile/fetch template ----
        template = self._get_substitution_template(source_type, target_types)
        if template is None:
            return {
                "success": False,
                "reason": (
                    f"ABC could not map '{source_type}' using only {target_types}. "
                    f"The target gate set may not be functionally complete for this operation."
                ),
                "replaced": 0, "skipped": 0,
            }

        tmpl_inputs       = template["inputs"]        # e.g. ["A","B"] or ["A"]
        tmpl_output       = template["output"]        # e.g. "Y"
        tmpl_gates        = template["gates"]         # List[GateNode]
        tmpl_int_wires    = template["internal_wires"]

        # ---- 2. Collect target gate instances ----
        if output_signal is None or output_signal == "*":
            # Whole design
            target_insts = [
                inst for inst, node in nl.nodes.items()
                if node.gate_type == source_type
            ]
        else:
            # Resolve DFF Q → D
            dff_q_to_d = {d.q: d.d for d in nl.dffs.values()}
            actual_out = dff_q_to_d.get(output_signal, output_signal)

            out2gate = self._build_output_to_gate()
            if actual_out not in out2gate:
                return {
                    "success": False,
                    "reason": f"Signal '{output_signal}' has no driving combinational gate.",
                    "replaced": 0, "skipped": 0,
                }

            # BFS backward
            cone_insts: Set[str] = set()
            queue = [actual_out]
            visited: Set[str] = set()
            pi_set = set(nl.primary_inputs)
            dff_q_set = {d.q for d in nl.dffs.values()}

            while queue:
                sig = queue.pop()
                if sig in visited:
                    continue
                visited.add(sig)
                if sig in {"1'b0", "1'b1"} or sig in pi_set or sig in dff_q_set:
                    continue
                driver = out2gate.get(sig)
                if driver and driver in nl.nodes:
                    cone_insts.add(driver)
                    for inp in nl.nodes[driver].inputs:
                        if inp not in visited:
                            queue.append(inp)

            target_insts = [
                inst for inst in cone_insts
                if nl.nodes[inst].gate_type == source_type
            ]

        if not target_insts:
            return {
                "success": True,
                "replaced": 0,
                "skipped": 0,
                "reason": f"No '{source_type}' gates found in the specified scope.",
                "source_type": source_type,
                "target_types": target_types,
                "output_signal": output_signal,
            }

        # ---- 3. Apply template to each matching gate ----
        replaced = 0
        skipped  = 0
        sub_counter = itertools.count(0)

        # Snapshot existing wire names to avoid collisions
        existing_wires = set(nl.wires.keys())
        existing_insts = set(nl.nodes.keys()) | set(nl.dffs.keys())

        for inst_name in target_insts:
            node = nl.nodes.get(inst_name)
            if node is None:
                skipped += 1
                continue

            idx = next(sub_counter)
            prefix = f"_rs_{idx}_"

            # Map template placeholders → real signal names
            # tmpl_inputs[0] → node.inputs[0], etc.
            port_map: Dict[str, str] = {}
            for i, tp in enumerate(tmpl_inputs):
                if i < len(node.inputs):
                    port_map[tp] = node.inputs[i]
            port_map[tmpl_output] = node.output

            # Assign fresh names to internal wires
            wire_map: Dict[str, str] = {}
            for tw in tmpl_int_wires:
                candidate = prefix + tw
                while candidate in existing_wires:
                    candidate += "_"
                wire_map[tw] = candidate
                existing_wires.add(candidate)
                nl.wires[candidate] = WireInfo(name=candidate)

            def resolve(sig: str) -> str:
                if sig in port_map:
                    return port_map[sig]
                if sig in wire_map:
                    return wire_map[sig]
                return sig

            # Remove original gate
            del nl.nodes[inst_name]

            # Insert template gates (skip any buf that wasn't in target_types)
            for tg in tmpl_gates:
                if tg.gate_type == "buf" and "buf" not in target_types:
                    # buf is a pass-through: rewire consumers to its input
                    buf_out = resolve(tg.output)
                    buf_in  = resolve(tg.inputs[0])
                    # Replace all uses of buf_out with buf_in
                    for n in nl.nodes.values():
                        n.inputs = [buf_in if x == buf_out else x for x in n.inputs]
                    for dff in nl.dffs.values():
                        for attr in ("d", "ck", "rn", "sn"):
                            if getattr(dff, attr) == buf_out:
                                setattr(dff, attr, buf_in)
                    # Remove the intermediate wire if it was freshly created
                    nl.wires.pop(buf_out, None)
                    continue

                new_inst = prefix + tg.name
                while new_inst in existing_insts:
                    new_inst += "_"
                existing_insts.add(new_inst)

                new_node = GateNode(
                    name=new_inst,
                    gate_type=tg.gate_type,
                    inputs=[resolve(i) for i in tg.inputs],
                    output=resolve(tg.output),
                )
                nl.nodes[new_inst] = new_node

            replaced += 1

        return {
            "success": True,
            "replaced": replaced,
            "skipped":  skipped,
            "source_type":   source_type,
            "target_types":  target_types,
            "output_signal": output_signal,
        }

    def collapse_inverter_pairs(self) -> dict:
        """Collapse every NOT-to-NOT chain into a direct connection.

        Consumers of the second inverter are rewired to the first inverter's
        input. The first inverter is removed only when it has no other fanout.
        A boundary buffer is retained when the second output is a primary
        output or DFF port, preserving externally significant net names.
        """
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        pairs_collapsed = 0
        gates_removed = 0
        boundary_buffers = 0

        def is_primary_output(sig: str) -> bool:
            return sig in nl.primary_outputs or sig.split("[", 1)[0] in nl.primary_outputs

        def used_by_dff(sig: str) -> bool:
            return any(
                sig in (dff.d, dff.ck, dff.rn, dff.sn)
                for dff in nl.dffs.values()
            )

        def signal_is_used(sig: str) -> bool:
            return (
                is_primary_output(sig)
                or used_by_dff(sig)
                or any(sig in node.inputs for node in nl.nodes.values())
            )

        def discard_unused_scalar_wire(sig: str) -> None:
            if (
                sig in nl.wires
                and sig not in nl.primary_inputs
                and not is_primary_output(sig)
                and not signal_is_used(sig)
                and all(node.output != sig for node in nl.nodes.values())
                and all(dff.q != sig for dff in nl.dffs.values())
            ):
                nl.wires.pop(sig, None)

        while True:
            out2gate = {
                node.output: inst_name
                for inst_name, node in nl.nodes.items()
            }
            candidates: List[Tuple[str, str]] = []
            for second_name, second in nl.nodes.items():
                if second.gate_type != "not" or len(second.inputs) != 1:
                    continue
                first_name = out2gate.get(second.inputs[0])
                if first_name is None or first_name == second_name:
                    continue
                first = nl.nodes.get(first_name)
                if first and first.gate_type == "not" and len(first.inputs) == 1:
                    candidates.append((first_name, second_name))

            if not candidates:
                break

            # Avoid processing overlapping chains from the same snapshot.
            selected: List[Tuple[str, str]] = []
            occupied: Set[str] = set()
            for first_name, second_name in candidates:
                if first_name in occupied or second_name in occupied:
                    continue
                selected.append((first_name, second_name))
                occupied.update((first_name, second_name))

            for first_name, second_name in selected:
                first = nl.nodes.get(first_name)
                second = nl.nodes.get(second_name)
                if first is None or second is None:
                    continue
                if (
                    first.gate_type != "not"
                    or second.gate_type != "not"
                    or second.inputs != [first.output]
                ):
                    continue

                source = first.inputs[0]
                first_output = first.output
                second_output = second.output
                keep_boundary_net = is_primary_output(second_output) or used_by_dff(second_output)

                # Internal combinational consumers can always bypass the pair.
                for node_name, node in nl.nodes.items():
                    if node_name != second_name:
                        node.inputs = [source if sig == second_output else sig for sig in node.inputs]

                if keep_boundary_net:
                    second.gate_type = "buf"
                    second.inputs = [source]
                    boundary_buffers += 1
                else:
                    for dff in nl.dffs.values():
                        for attr in ("d", "ck", "rn", "sn"):
                            if getattr(dff, attr) == second_output:
                                setattr(dff, attr, source)
                    nl.nodes.pop(second_name, None)
                    gates_removed += 1

                if not signal_is_used(first_output):
                    nl.nodes.pop(first_name, None)
                    gates_removed += 1

                discard_unused_scalar_wire(first_output)
                discard_unused_scalar_wire(second_output)
                pairs_collapsed += 1

        return {
            "pairs_collapsed": pairs_collapsed,
            "gates_removed": gates_removed,
            "boundary_buffers_retained": boundary_buffers,
        }

    def replace_pattern(self, pattern: str, replacement: str) -> int:
        """Replace matched structural patterns throughout the netlist.

        pattern and replacement are strings like "inv->buf" or "or->nand+not".
        Supported simple pattern: "<gate_type>" → "<gate_type>".

        Args:
            pattern:     Source gate type or chain (e.g. "buf", "inv->buf").
            replacement: Target gate type or chain (e.g. "not", "nand+not").

        Returns:
            Number of replacements made.
        """
        self._require_netlist()
        nl = self._netlist
        assert nl is not None

        # Parse simple single-gate pattern for now
        # "inv" is alias for "not"
        def normalise(s: str) -> str:
            return "not" if s.strip().lower() == "inv" else s.strip().lower()

        src_type = normalise(pattern.split("->")[0].split("+")[0])
        dst_type = normalise(replacement.split("->")[0].split("+")[0])

        if src_type not in PRIMITIVE_GATES or dst_type not in PRIMITIVE_GATES:
            raise ValueError(
                f"replace_pattern: unsupported gate type(s): {src_type!r}, {dst_type!r}"
            )

        src_inputs = 1 if src_type in ONE_INPUT_GATES else 2
        dst_inputs = 1 if dst_type in ONE_INPUT_GATES else 2
        if src_inputs != dst_inputs:
            raise ValueError(
                f"replace_pattern: port count mismatch between {src_type!r} and {dst_type!r}"
            )

        count = 0
        for node in nl.nodes.values():
            if node.gate_type == src_type:
                node.gate_type = dst_type
                count += 1
        return count


# ===========================================================================
# Standalone functional helpers (used by EDAEngine but not part of its class)
# ===========================================================================

def _find_cone_primary_input(
    nl: Netlist,
    output_signal: str,
    out2gate: Dict[str, str],
) -> Optional[str]:
    """Return the first primary input found in the transitive fanin of output_signal."""
    visited: Set[str] = set()
    queue: deque[str] = deque([output_signal])
    while queue:
        sig = queue.popleft()
        if sig in visited:
            continue
        visited.add(sig)
        if sig in nl.primary_inputs:
            return sig
        driver = out2gate.get(sig)
        if driver and driver in nl.nodes:
            for inp in nl.nodes[driver].inputs:
                queue.append(inp)
    return None


def _rebalance_associative_tree(
    nl: Netlist,
    output_signal: str,
    out2gate: Dict[str, str],
    max_depth: int,
) -> bool:
    """Attempt to rebalance an associative gate tree to meet max_depth.

    This is a best-effort structural rebalancing pass.
    Returns True if the depth is now ≤ max_depth.
    """
    driver_inst = out2gate.get(output_signal)
    if driver_inst is None or driver_inst not in nl.nodes:
        return False

    root_gate = nl.nodes[driver_inst]
    if root_gate.gate_type not in {"and", "or", "xor", "nand", "nor", "xnor"}:
        return False  # Non-associative gate — cannot rebalance

    # Collect all leaves of this associative tree
    leaves: List[str] = []
    _collect_leaves(nl, root_gate.gate_type, output_signal, out2gate, leaves)

    if len(leaves) < 4:
        return False  # Nothing to rebalance

    # Build a balanced binary tree from leaves
    gate_type = root_gate.gate_type
    _build_balanced_tree(nl, leaves, gate_type, output_signal, root_gate)
    return True


def _collect_leaves(
    nl: Netlist,
    gate_type: str,
    sig: str,
    out2gate: Dict[str, str],
    leaves: List[str],
) -> None:
    """DFS to collect inputs of same-type associative gate tree."""
    driver = out2gate.get(sig)
    if driver is None or driver not in nl.nodes:
        leaves.append(sig)
        return
    node = nl.nodes[driver]
    if node.gate_type != gate_type:
        leaves.append(sig)
        return
    for inp in node.inputs:
        _collect_leaves(nl, gate_type, inp, out2gate, leaves)


def _build_balanced_tree(
    nl: Netlist,
    leaves: List[str],
    gate_type: str,
    final_output: str,
    reuse_node: GateNode,
) -> None:
    """Build a balanced binary tree of gate_type over leaves, writing final output."""
    counter = itertools.count(1)
    layer = list(leaves)

    while len(layer) > 2:
        next_layer: List[str] = []
        for i in range(0, len(layer) - 1, 2):
            new_wire = f"_bal_{gate_type}_{next(counter)}"
            nl.wires[new_wire] = WireInfo(name=new_wire)
            new_inst = f"_bal_g_{next(counter)}"
            new_node = GateNode(
                name=new_inst,
                gate_type=gate_type,
                inputs=[layer[i], layer[i + 1]],
                output=new_wire,
            )
            nl.nodes[new_inst] = new_node
            next_layer.append(new_wire)
        if len(layer) % 2 == 1:
            next_layer.append(layer[-1])
        layer = next_layer

    # Final gate reuses the root node's slot
    if len(layer) == 2:
        reuse_node.inputs = [layer[0], layer[1]]
    elif len(layer) == 1:
        reuse_node.gate_type = "buf"
        reuse_node.inputs = [layer[0]]
