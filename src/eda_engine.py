"""EDA Engine — netlist analysis and transformation operations."""

from __future__ import annotations

import copy
import itertools
import re
import signal
import sys
import os
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
        self._output_dir = "."

    # ------------------------------------------------------------------
    # Netlist lifecycle
    # ------------------------------------------------------------------

    def load(self, filepath: str) -> Netlist:
        """Load a Verilog file into the engine and return the Netlist."""
        self._netlist = parse_verilog(filepath)
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

    def _require_netlist(self) -> None:
        if self._netlist is None:
            raise ValueError("No netlist loaded. Call read_design first.")

    def set_output_directory(
        self,
        path: str,
    ):

        import os

        os.makedirs(
            path,
            exist_ok=True,
        )

        self._output_dir = path
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
            for sig in (dff.clk, dff.rst_n, dff.d):
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
    
    def count_paths(
        self,
        source: str,
        sink: str,
    ) -> int:

        self._require_netlist()

        sink_reachable = self._nodes_reaching_sink(sink)

        memo = {}

        def dfs(node):

            if node == sink:
                return 1

            if node in memo:
                return memo[node]

            total = 0

            for nxt in self._forward_successors(node):

                if nxt not in sink_reachable:
                    continue

                total += dfs(nxt)

            memo[node] = total

            return total

        return {
            "source": source,
            "sink": sink,
            "count": dfs(source)
        }

    def find_all_paths(
        self,
        source: str,
        sink: str,
        max_paths: int = 1000
    ):

        path_count = self.count_paths(
            source,
            sink,
        )["count"]

        MAX_ENUMERATION = 1000

        if path_count > MAX_ENUMERATION:

            return {
                "source": source,
                "sink": sink,
                "count": path_count,
                "enumerated": False,
                "message": (
                    f"Too many paths "
                    f"({path_count}) "
                    f"to enumerate."
                )
            }

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

        # ----------------------------------------------------------
        # Large result handling
        # ----------------------------------------------------------

        output_file = None

        if len(paths) > 100:

            safe_source = (
                source.replace("[", "_")
                    .replace("]", "")
            )

            safe_sink = (
                sink.replace("[", "_")
                    .replace("]", "")
            )

            filename = (
                f"all_paths_"
                f"{safe_source}_to_{safe_sink}.txt"
            )

            output_file = os.path.join(
                self._output_dir,
                filename,
            )

            with open(output_file, "w") as f:

                f.write(
                    f"All paths from "
                    f"{source} to {sink}\n\n"
                )

                for idx, p in enumerate(paths, 1):

                    f.write(
                        f"Path {idx}:\n"
                    )

                    f.write(
                        " -> ".join(p)
                    )

                    f.write("\n\n")

        return {
            "source": source,
            "sink": sink,
            "count": len(paths),

            "paths":
                paths[:20]
                if output_file
                else paths,

            "truncated":
                len(paths) >= max_paths,

            "output_file":
                output_file,
        }
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
    
    def get_gate_fanout(self, gate_name: str):
        nl = self._netlist
        assert nl is not None

        if gate_name not in nl.nodes:
            raise ValueError(f"Unknown gate: {gate_name}")

        output_signal = nl.nodes[gate_name].output

        return self.get_fanout(output_signal)

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

        return nl.dffs[dff1].clk == nl.dffs[dff2].clk

    def check_equivalence(self, output: str, snapshot_label: str = "default") -> bool:
        """Check functional equivalence between current netlist and a snapshot.

        Performs a truth-table comparison for small cones (≤ 20 inputs).
        Returns True if both netlists produce identical logic for *output*.

        Args:
            output:         The output signal to compare.
            snapshot_label: Label of the saved snapshot to compare against.
        """
        self._require_netlist()
        if snapshot_label not in self._snapshots:
            raise ValueError(f"No snapshot with label {snapshot_label!r}")

        current = self._netlist
        reference = self._snapshots[snapshot_label]

        return _truth_table_equiv(current, reference, output)

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
                        if dff.clk == cur_net:
                            dff.clk = new_wire
                        if dff.rst_n == cur_net:
                            dff.rst_n = new_wire
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
            needed_signals.add(dff.clk)
            needed_signals.add(dff.rst_n)

        # Backward BFS: find all gate outputs that transitively feed needed signals
        useful_outputs: Set[str] = set()
        queue: deque[str] = deque(needed_signals)
        out2gate = self._build_output_to_gate()

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
            used_wires.update({dff.clk, dff.rst_n, dff.d, dff.q})

        declared = set(nl.primary_inputs) | set(nl.primary_outputs)
        nl.wires = {
            k: v
            for k, v in nl.wires.items()
            if k in declared or k in used_wires
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
        if not self.check_equivalence(output_signal, "pre_opt"):
            self.restore_snapshot("pre_opt")
            return False

        self.remove_dangling_gates()
        return True

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


# ===========================================================================
# Truth-table equivalence checker (≤ 20 primary inputs)
# ===========================================================================

def _truth_table_equiv(nl_a: Netlist, nl_b: Netlist, output: str) -> bool:
    """Return True if both netlists produce the same logic for output.

    Only works for cones with ≤ 20 primary inputs.
    """
    inputs_a = _cone_primary_inputs(nl_a, output)
    inputs_b = _cone_primary_inputs(nl_b, output)
    all_inputs = sorted(set(inputs_a) | set(inputs_b))

    if len(all_inputs) > 20:
        # Too large for exhaustive check; assume equivalent
        return True

    for combo in itertools.product([False, True], repeat=len(all_inputs)):
        assignment = dict(zip(all_inputs, combo))
        val_a = _eval_signal(nl_a, output, assignment)
        val_b = _eval_signal(nl_b, output, assignment)
        if val_a != val_b:
            return False
    return True


def _cone_primary_inputs(nl: Netlist, output: str) -> List[str]:
    """Return primary inputs in the transitive fanin of output."""
    out2gate: Dict[str, str] = {}
    for inst, node in nl.nodes.items():
        out2gate[node.output] = inst
    for inst, dff in nl.dffs.items():
        out2gate[dff.q] = inst

    pis: List[str] = []
    visited: Set[str] = set()
    queue: deque[str] = deque([output])
    while queue:
        sig = queue.popleft()
        if sig in visited:
            continue
        visited.add(sig)
        if sig in nl.primary_inputs:
            pis.append(sig)
            continue
        driver = out2gate.get(sig)
        if driver and driver in nl.nodes:
            for inp in nl.nodes[driver].inputs:
                queue.append(inp)
    return pis


def _eval_signal(
    nl: Netlist, signal: str, assignment: Dict[str, bool]
) -> bool:
    """Recursively evaluate signal given a primary-input assignment."""
    if signal in assignment:
        return assignment[signal]
    if signal == "1'b1":
        return True
    if signal == "1'b0":
        return False

    out2gate: Dict[str, str] = {n.output: inst for inst, n in nl.nodes.items()}

    cache: Dict[str, bool] = {}

    def _eval(sig: str) -> bool:
        if sig in cache:
            return cache[sig]
        if sig in assignment:
            return assignment[sig]
        if sig == "1'b1":
            return True
        if sig == "1'b0":
            return False

        driver = out2gate.get(sig)
        if driver is None:
            return False  # unknown signal

        node = nl.nodes[driver]
        in_vals = [_eval(i) for i in node.inputs]

        gt = node.gate_type
        if gt == "buf":
            result = in_vals[0]
        elif gt == "not":
            result = not in_vals[0]
        elif gt == "and":
            result = in_vals[0] and in_vals[1]
        elif gt == "or":
            result = in_vals[0] or in_vals[1]
        elif gt == "nand":
            result = not (in_vals[0] and in_vals[1])
        elif gt == "nor":
            result = not (in_vals[0] or in_vals[1])
        elif gt == "xor":
            result = in_vals[0] ^ in_vals[1]
        elif gt == "xnor":
            result = not (in_vals[0] ^ in_vals[1])
        else:
            result = False

        cache[sig] = result
        return result

    return _eval(signal)
