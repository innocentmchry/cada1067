"""Gate-level Verilog netlist parser and writer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class ParseError(Exception):
    """Raised when the Verilog source cannot be parsed."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class WireInfo:
    """Metadata for a single wire or bus declaration."""
    name: str
    width: int = 1
    is_bus: bool = False
    msb: int = 0
    lsb: int = 0


@dataclass
class GateNode:
    """Represents a primitive gate instance."""
    name: str
    gate_type: str
    inputs: List[str] = field(default_factory=list)
    output: str = ""


@dataclass
class DFFNode:
    """Represents a flip-flop (dff) instance."""
    name: str
    clk: str = ""
    rst_n: str = ""
    d: str = ""
    q: str = ""


@dataclass
class Netlist:
    """Complete netlist representation as a directed graph."""
    module_name: str = ""
    primary_inputs: List[str] = field(default_factory=list)
    primary_outputs: List[str] = field(default_factory=list)
    wires: Dict[str, WireInfo] = field(default_factory=dict)
    nodes: Dict[str, GateNode] = field(default_factory=dict)
    dffs: Dict[str, DFFNode] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Primitive gate definitions
# ---------------------------------------------------------------------------

PRIMITIVE_GATES = {"and", "or", "nand", "nor", "not", "buf", "xor", "xnor"}

# Gates with exactly 2 inputs
TWO_INPUT_GATES = {"and", "or", "nand", "nor", "xor", "xnor"}

# Gates with exactly 1 input
ONE_INPUT_GATES = {"not", "buf"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strip_comments(text: str) -> str:
    """Remove // and /* */ style comments from Verilog source."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def _tokenize_ports(port_str: str) -> List[str]:
    """Split a comma-separated port list, ignoring whitespace."""
    return [p.strip() for p in port_str.split(",") if p.strip()]


def _parse_wire_range(range_str: str) -> tuple[int, int]:
    """Parse [msb:lsb] and return (msb, lsb)."""
    m = re.match(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", range_str.strip())
    if not m:
        raise ParseError(f"Cannot parse wire range: {range_str!r}")
    return int(m.group(1)), int(m.group(2))


def _resolve_port_signal(token: str) -> str:
    """Normalise a port connection token.

    Handles:
    - Plain identifiers   →  returned as-is
    - Bus slices  a[3]   →  returned as-is (a[3])
    - Constants  1'b0 / 1'b1  →  returned as-is
    """
    return token.strip()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_verilog(filepath: str) -> Netlist:
    """Parse a gate-level Verilog file and return a Netlist.

    Args:
        filepath: Path to the .v source file.

    Returns:
        A fully populated Netlist object.

    Raises:
        ParseError: If the file cannot be parsed.
        FileNotFoundError: If the file does not exist.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            source = fh.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"Verilog file not found: {filepath!r}")

    source = _strip_comments(source)

    # Extract module body
    mod_m = re.search(
        r"\bmodule\s+(\w+)\s*\(([^)]*)\)\s*;(.*?)\bendmodule\b",
        source,
        re.DOTALL,
    )
    if not mod_m:
        raise ParseError("No valid 'module ... endmodule' block found.")

    module_name = mod_m.group(1)
    port_list_str = mod_m.group(2)  # may contain ANSI-style port declarations
    body = mod_m.group(3)

    # Combine ANSI port header + body so that both declaration styles are parsed
    # by the same regex later.  We just pretend the ANSI declarations are also
    # written as separate statements by appending semicolons after each port.
    ansi_ports: List[str] = []
    for token in _tokenize_ports(port_list_str):
        token = token.strip()
        # ANSI port looks like "input wire foo" or "output [3:0] bar"
        if re.match(r"^(input|output)\b", token, re.IGNORECASE):
            ansi_ports.append(token + ";")
    if ansi_ports:
        body = "\n".join(ansi_ports) + "\n" + body

    netlist = Netlist(module_name=module_name)

    # -----------------------------------------------------------------------
    # Parse port declarations (input / output)
    # -----------------------------------------------------------------------
    for decl_m in re.finditer(
        r"\b(input|output)\s+(?:wire\s+)?(\[\s*\d+\s*:\s*\d+\s*\]\s*)?([^;]+);",
        body,
    ):
        direction = decl_m.group(1)
        range_part = decl_m.group(2) or ""
        names_str = decl_m.group(3)

        msb, lsb = (0, 0)
        is_bus = False
        if range_part.strip():
            msb, lsb = _parse_wire_range(range_part)
            is_bus = True
            width = abs(msb - lsb) + 1
        else:
            width = 1

        for raw_name in _tokenize_ports(names_str):
            name = raw_name.strip()
            if not name:
                continue
            wi = WireInfo(name=name, width=width, is_bus=is_bus, msb=msb, lsb=lsb)
            netlist.wires[name] = wi
            if direction == "input":
                netlist.primary_inputs.append(name)
            else:
                netlist.primary_outputs.append(name)

    # -----------------------------------------------------------------------
    # Parse wire declarations
    # -----------------------------------------------------------------------
    for decl_m in re.finditer(
        r"\bwire\s+(\[\s*\d+\s*:\s*\d+\s*\]\s*)?([^;]+);",
        body,
    ):
        range_part = decl_m.group(1) or ""
        names_str = decl_m.group(2)

        msb, lsb = (0, 0)
        is_bus = False
        if range_part.strip():
            msb, lsb = _parse_wire_range(range_part)
            is_bus = True
            width = abs(msb - lsb) + 1
        else:
            width = 1

        for raw_name in _tokenize_ports(names_str):
            name = raw_name.strip()
            if not name:
                continue
            if name not in netlist.wires:
                netlist.wires[name] = WireInfo(
                    name=name, width=width, is_bus=is_bus, msb=msb, lsb=lsb
                )

    # -----------------------------------------------------------------------
    # Parse gate instances  (primitives + dff)
    # -----------------------------------------------------------------------
    # Pattern: gate_type  instance_name  ( port, port, ... ) ;
    inst_pattern = re.compile(
        r"\b(\w+)\s+(\w+)\s*\(([^)]*)\)\s*;",
        re.DOTALL,
    )

    skip_keywords = {
        "module", "input", "output", "wire", "reg", "assign",
        "always", "initial", "begin", "end", "if", "else",
    }

    for m in inst_pattern.finditer(body):
        gate_type = m.group(1).lower()
        inst_name = m.group(2)
        port_str = m.group(3)

        if gate_type in skip_keywords:
            continue

        ports = [_resolve_port_signal(p) for p in _tokenize_ports(port_str)]

        if gate_type == "dff":
            _parse_dff(netlist, inst_name, ports)
        elif gate_type in PRIMITIVE_GATES:
            _parse_primitive(netlist, inst_name, gate_type, ports)
        # Unknown instance types are silently skipped (hierarchical cells etc.)

    return netlist


def _parse_dff(netlist: Netlist, inst_name: str, ports: List[str]) -> None:
    """Add a DFF node from an ordered port list (clk, rst_n, d, q)."""
    if len(ports) != 4:
        raise ParseError(
            f"DFF instance {inst_name!r} expects 4 ports (clk, rst_n, d, q), "
            f"got {len(ports)}: {ports}"
        )
    dff = DFFNode(
        name=inst_name,
        clk=ports[0],
        rst_n=ports[1],
        d=ports[2],
        q=ports[3],
    )
    netlist.dffs[inst_name] = dff


def _parse_primitive(
    netlist: Netlist, inst_name: str, gate_type: str, ports: List[str]
) -> None:
    """Add a primitive gate node.

    Verilog primitive port order: output first, then inputs.
    """
    if gate_type in ONE_INPUT_GATES:
        if len(ports) != 2:
            raise ParseError(
                f"Gate {inst_name!r} ({gate_type}) expects 2 ports "
                f"(output, input), got {len(ports)}: {ports}"
            )
        output_port = ports[0]
        input_ports = [ports[1]]
    elif gate_type in TWO_INPUT_GATES:
        if len(ports) != 3:
            raise ParseError(
                f"Gate {inst_name!r} ({gate_type}) expects 3 ports "
                f"(output, in0, in1), got {len(ports)}: {ports}"
            )
        output_port = ports[0]
        input_ports = [ports[1], ports[2]]
    else:
        raise ParseError(f"Unknown primitive gate type: {gate_type!r}")

    node = GateNode(
        name=inst_name,
        gate_type=gate_type,
        inputs=input_ports,
        output=output_port,
    )
    netlist.nodes[inst_name] = node


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def write_verilog(netlist: Netlist, filepath: str) -> None:
    """Write a Netlist back to a gate-level Verilog file.

    Args:
        netlist: The Netlist to serialise.
        filepath: Destination path for the .v file.
    """
    lines: List[str] = []

    # Module header
    all_ports = netlist.primary_inputs + netlist.primary_outputs
    lines.append(f"module {netlist.module_name} (")
    port_decls = []
    for p in all_ports:
        port_decls.append(f"  {p}")
    lines.append(",\n".join(port_decls))
    lines.append(");")
    lines.append("")

    # Input / output declarations
    for name in netlist.primary_inputs:
        wi = netlist.wires.get(name)
        if wi and wi.is_bus:
            lines.append(f"  input wire [{wi.msb}:{wi.lsb}] {name};")
        else:
            lines.append(f"  input wire {name};")

    for name in netlist.primary_outputs:
        wi = netlist.wires.get(name)
        if wi and wi.is_bus:
            lines.append(f"  output wire [{wi.msb}:{wi.lsb}] {name};")
        else:
            lines.append(f"  output wire {name};")

    lines.append("")

    # Internal wire declarations
    declared = set(netlist.primary_inputs) | set(netlist.primary_outputs)
    internal_wires = [
        (n, wi) for n, wi in netlist.wires.items() if n not in declared
    ]
    if internal_wires:
        for name, wi in internal_wires:
            if wi.is_bus:
                lines.append(f"  wire [{wi.msb}:{wi.lsb}] {name};")
            else:
                lines.append(f"  wire {name};")
        lines.append("")

    # Gate instances
    for inst_name, node in netlist.nodes.items():
        if node.gate_type in ONE_INPUT_GATES:
            ports = f"{node.output}, {node.inputs[0]}"
        else:
            ports = f"{node.output}, {node.inputs[0]}, {node.inputs[1]}"
        lines.append(f"  {node.gate_type:<6} {inst_name:<20} ({ports});")

    # DFF instances
    for inst_name, dff in netlist.dffs.items():
        ports = f"{dff.clk}, {dff.rst_n}, {dff.d}, {dff.q}"
        lines.append(f"  dff    {inst_name:<20} ({ports});")

    lines.append("")
    lines.append("endmodule")
    lines.append("")

    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
