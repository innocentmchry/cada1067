"""OpenAI function-calling tool schemas for the EDA engine."""

from __future__ import annotations

from typing import Any, Dict, List


TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_design",
            "description": (
                "Call this to load a gate-level Verilog netlist from disk into the EDA engine. "
                "Must be called before any analysis or transformation operation. "
                "The filepath argument should include the directory and filename."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to the Verilog (.v) file to load.",
                    }
                },
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_design",
            "description": (
                "Call this to write the current (possibly modified) netlist back to a "
                "Verilog file on disk. Use after any transformation to persist changes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Destination path for the output Verilog file.",
                    }
                },
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_testcase_name",
            "description": (
                "Call this when the user specifies a testcase name (e.g., 'test8', 'test35'). "
                "Sets the active case name and opens the corresponding log file for output mirroring."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "case_name": {
                        "type": "string",
                        "description": "The short identifier for this testcase (e.g. 'test8').",
                    },
                    "log_path": {
                        "type": "string",
                        "description": (
                            "Path to the log file (e.g. 'test8.log'). "
                            "Defaults to '<case_name>.log' in the current directory."
                        ),
                    },
                },
                "required": ["case_name", "log_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_max_depth",
            "description": (
                "Call this to find the longest combinational path (maximum logic depth) "
                "between a source signal and a sink signal. "
                "DFF boundaries are treated as cuts. "
                "Returns the depth (integer) and the list of signals on the longest path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Starting signal name (e.g. a primary input).",
                    },
                    "sink": {
                        "type": "string",
                        "description": "Ending signal name (e.g. a primary output).",
                    },
                },
                "required": ["source", "sink"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "path_passes_through",
            "description": (
                "Call this to determine whether ALL combinational paths from source to sink "
                "pass through a specific intermediate node. "
                "Returns true if node is a mandatory waypoint on every source→sink path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Starting signal name."},
                    "sink": {"type": "string", "description": "Ending signal name."},
                    "node": {
                        "type": "string",
                        "description": "The intermediate signal to test.",
                    },
                },
                "required": ["source", "sink", "node"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_path_avoiding",
            "description": (
                "Call this to find one combinational path from source to sink that does NOT "
                "pass through the specified signal. "
                "Returns the path as a list of signal names, or null if no such path exists."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Starting signal name."},
                    "sink": {"type": "string", "description": "Ending signal name."},
                    "avoid": {
                        "type": "string",
                        "description": "Signal that must not appear on the returned path.",
                    },
                },
                "required": ["source", "sink", "avoid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_logic_cone",
            "description": (
                "Call this to retrieve all gate instance names that directly or transitively "
                "feed (drive) the given output signal. Returns the list of gate instance names."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "output_signal": {
                        "type": "string",
                        "description": "The output net whose transitive fanin is requested.",
                    }
                },
                "required": ["output_signal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_cone_gates",
            "description": (
                "Call this to count the number of gates in the logic cone of an output signal. "
                "Returns an integer gate count."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "output_signal": {
                        "type": "string",
                        "description": "The output net whose logic cone gate count is requested.",
                    }
                },
                "required": ["output_signal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fanout",
            "description": (
                "Call this to find all gate instances that are directly driven by a given net. "
                "Returns a list of gate instance names."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "net_name": {
                        "type": "string",
                        "description": "The net name whose fanout list is requested.",
                    }
                },
                "required": ["net_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "are_same_clock_domain",
            "description": (
                "Call this to check whether two flip-flop (DFF) instances share the same clock net. "
                "Returns true if both DFFs are in the same clock domain."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dff1": {
                        "type": "string",
                        "description": "First DFF instance name.",
                    },
                    "dff2": {
                        "type": "string",
                        "description": "Second DFF instance name.",
                    },
                },
                "required": ["dff1", "dff2"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_gate_before",
            "description": (
                "Call this to insert a new 2-input gate immediately before an existing gate instance. "
                "The original driver signal becomes input[0] of the new gate; "
                "extra_input becomes input[1]. "
                "The new gate's output replaces the original signal feeding the target. "
                "Returns the new gate's instance name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_instance": {
                        "type": "string",
                        "description": "Existing gate instance to insert before.",
                    },
                    "gate_type": {
                        "type": "string",
                        "description": "Type of gate to insert (e.g. 'and', 'or', 'nand').",
                    },
                    "extra_input": {
                        "type": "string",
                        "description": "Second input signal for the new gate.",
                    },
                },
                "required": ["target_instance", "gate_type", "extra_input"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_gate",
            "description": (
                "Call this to replace the gate type of an existing instance in-place. "
                "When replacing a 1-input gate (buf/not) with a 2-input gate (and/or/nand/nor/xor/xnor), "
                "supply extra_input to provide the additional input signal. "
                "Use this to swap e.g. a buf for a not, an and for an or, "
                "or a buf for an and with a gating control signal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instance_name": {
                        "type": "string",
                        "description": "Instance name of the gate to replace.",
                    },
                    "new_gate_type": {
                        "type": "string",
                        "description": "New gate type (e.g. 'and', 'not', 'xor').",
                    },
                    "extra_input": {
                        "type": "string",
                        "description": (
                            "Optional second input signal. Required when upgrading from "
                            "a 1-input gate (buf/not) to a 2-input gate."
                        ),
                    },
                },
                "required": ["instance_name", "new_gate_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_buffers_for_fanout",
            "description": (
                "Call this to insert buffer trees on a net so that no downstream gate "
                "has fanout exceeding max_fanout. "
                "Returns the number of buffers inserted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "net_name": {
                        "type": "string",
                        "description": "The net to apply fanout buffering to.",
                    },
                    "max_fanout": {
                        "type": "integer",
                        "description": "Maximum allowed fanout per net segment.",
                    },
                },
                "required": ["net_name", "max_fanout"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "balance_depth",
            "description": (
                "Call this to add buffers along paths so that all paths from source to each "
                "sink have the same combinational depth. "
                "Uses minimal buffer insertion. "
                "Returns the number of buffers inserted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Starting signal name.",
                    },
                    "sinks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of sink signal names to equalise.",
                    },
                },
                "required": ["source", "sinks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_dangling_gates",
            "description": (
                "Call this to remove all gates and nets that do not transitively feed "
                "any primary output or DFF input. "
                "Returns the number of gates removed. "
                "Automatically called after every transformation."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "optimize_cone_depth",
            "description": (
                "Call this to restructure the logic cone of output_signal so its combinational "
                "depth is at most max_depth. "
                "Preserves functional equivalence. "
                "Returns true if the depth constraint is met, false if it cannot be achieved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "output_signal": {
                        "type": "string",
                        "description": "The output net whose cone should be optimised.",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum allowed combinational depth after optimisation.",
                    },
                },
                "required": ["output_signal", "max_depth"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_pattern",
            "description": (
                "Call this to replace structural gate patterns throughout the entire netlist. "
                "The pattern and replacement are gate type strings such as 'buf', 'not', 'and'. "
                "Simple chain notation 'A->B' or compound 'A+B' is also accepted. "
                "Returns the number of replacements made."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Source gate type or pattern (e.g. 'buf', 'inv->buf').",
                    },
                    "replacement": {
                        "type": "string",
                        "description": "Target gate type or pattern (e.g. 'not', 'nand+not').",
                    },
                },
                "required": ["pattern", "replacement"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_instances_by_name_pattern",
            "description": (
                "Call this to search for gate instances by gate type and/or instance name pattern. "
                "Pass an empty string for gate_type to match all types. "
                "name_pattern is a Python regex applied to instance names. "
                "Returns a list of matching instance names."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "gate_type": {
                        "type": "string",
                        "description": "Gate type filter (e.g. 'buf'). Empty string matches all types.",
                    },
                    "name_pattern": {
                        "type": "string",
                        "description": "Python regex pattern to match against instance names.",
                    },
                },
                "required": ["gate_type", "name_pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_equivalence",
            "description": (
                "Call this to verify that the current netlist is functionally equivalent "
                "to a previously saved snapshot for the given output signal. "
                "Uses truth-table comparison for cones with up to 20 primary inputs. "
                "Returns true if both netlists produce identical logic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "output": {
                        "type": "string",
                        "description": "Output signal name to compare between current netlist and snapshot.",
                    }
                },
                "required": ["output"],
            },
        },
    },
]
