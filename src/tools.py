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
            "name": "count_gates",
            "description": (
                "Count all gates in the currently loaded design "
                "and return totals grouped by gate type."
                "Invoke this whitout fail when asked for count all the gates "
                "Invoke this whitout fail when asked to Compute the total gate count of the design. "

            ),
            "parameters": {
                "type": "object",
                "properties": {},
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
            "name": "get_fanin_cone_depth",
            "description": (
                "Return the maximum logic depth "
                "of the fanin cone of an output signal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "output_signal": {
                        "type": "string"
                    }
                },
                "required": ["output_signal"]
            }
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_max_depth_between_endpoint_classes",
            "description": (
                "Compute the maximum combinational logic depth across endpoint CLASSES. "
                "Invoke this exact tool for requests such as 'from any primary input to any DFF D-pin' "
                "or 'from any PI to any primary output'. This evaluates all matching endpoints in one "
                "operation. Do not pass literal strings PI, DFF, or PO to get_max_depth."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_class": {
                        "type": "string",
                        "enum": ["PI"],
                        "description": "Source endpoint class.",
                    },
                    "sink_class": {
                        "type": "string",
                        "enum": ["DFF_D", "PO"],
                        "description": "DFF_D means every DFF D-pin; PO means every primary-output bit.",
                    },
                },
                "required": ["source_class", "sink_class"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_max_depth",
            "description": (
                "Find the longest combinational path between two CONCRETE signal names. "
                "Both source and sink must be actual netlist signals such as n0 or n16[2]. "
                "Never pass endpoint classes such as PI, DFF, DFF_D, or PO here; use "
                "get_max_depth_between_endpoint_classes for those requests. "
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
            "name": "find_all_paths",
            "description": (
                "List all combinational paths "
                "between a source signal and sink signal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string"
                    },
                    "sink": {
                        "type": "string"
                    }
                },
                "required": [
                    "source",
                    "sink"
                ]
            }
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_register_to_register_paths",
            "description": (
                "List ALL register-to-register paths through combinational logic. This exact tool must "
                "be used for requests such as 'List all register-to-register paths in this design'. "
                "It traverses from every DFF Q pin to reached DFF D pins and reports the ordered gates. "
                "Do not use list_signals, get_max_depth, or get_max_depth_between_endpoint_classes. "
                "Up to 10 paths are returned inline; larger results are written under ./temp/ and the "
                "result returns the path count and file path. Enumeration has a 100,000-path safety "
                "limit and returns truncated=true when that limit is reached; this must be disclosed."
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
            "name": "resolve_name_type",
            "description": (
                "Determine whether a provided identifier is a combinational gate instance, DFF instance, "
                "or signal/wire. Call this first when the wording is ambiguous, then select the matching "
                "gate-based or net-based fanout/reachability tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Identifier to classify, such as g0 or n16.",
                    }
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_gate_info",
            "description": (
                "Return the exact primitive gate type and every logical pin-to-net connection for a "
                "combinational gate or DFF instance. Use this tool directly for requests such as "
                "'What type of gate is g0? Report its gate type and pin connections.' The result "
                "identifies AND, NAND, NOR, NOT, BUF, OR, XOR, XNOR, or DFF; do not use "
                "resolve_name_type or a fanout tool for this request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "gate_name": {
                        "type": "string",
                        "description": "Combinational gate or DFF instance name, such as g0.",
                    }
                },
                "required": ["gate_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_reachable_gates_from_net",
            "description": (
                "Find ALL gates transitively reachable downstream from a SIGNAL or WIRE. "
                "Use after resolve_name_type identifies the source as a signal. Traversal follows "
                "combinational fanout, includes reached DFFs, and stops at DFF boundaries. "
                "For more than 10 gates, the complete list is written under ./temp/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "net_name": {
                        "type": "string",
                        "description": "Signal or bus from which downstream reachability starts.",
                    }
                },
                "required": ["net_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_reachable_gates_from_gate",
            "description": (
                "Find ALL gates transitively reachable downstream from the OUTPUT of a gate or DFF instance. "
                "Use after resolve_name_type identifies the source as a combinational_gate or dff. "
                "For more than 10 gates, the complete list is written under ./temp/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "gate_name": {
                        "type": "string",
                        "description": "Gate or DFF instance whose output starts the traversal.",
                    }
                },
                "required": ["gate_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_net_fanout",
            "description": (
                "Find all gate instances directly driven by a SIGNAL OR WIRE. Use for wording such as "
                "'fanout of net n0'. Do not pass a gate instance such as g0; use get_gate_output_fanout. "
                "For 10 or fewer gates, returns the names inline so they must be listed in the response. "
                "For more than 10 gates, writes the complete list to a text file in the current working "
                "directory temp folder and returns only the count and file path."
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
            "name": "get_gate_output_fanout",
            "description": (
                "Find every gate directly connected to the OUTPUT NET of a gate or DFF instance. "
                "Use this exact tool for wording such as 'gates connected to the output of g0'. "
                "Do not treat the gate instance name as a signal. Large results are written under ./temp/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "gate_name": {
                        "type": "string"
                    }
                },
                "required": ["gate_name"]
            }
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
            "name": "collapse_inverter_pairs",
            "description": (
                "Find every back-to-back inverter pair (NOT followed directly by NOT) "
                "and collapse the pair into a direct wire connection. Use this exact tool "
                "when asked to remove double inversions or collapse consecutive inverters. "
                "Shared fanouts and primary-output/DFF boundary net names are preserved. "
                "Returns the number of pairs collapsed and gates removed."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # {
    #     "type": "function",
    #     "function": {
    #         "name": "optimize_cone_depth",
    #         "description": (
    #             "Call this to restructure the logic cone of output_signal so its combinational "
    #             "depth is at most max_depth. "
    #             "Preserves functional equivalence. "
    #             "Returns true if the depth constraint is met, false if it cannot be achieved."
    #         ),
    #         "parameters": {
    #             "type": "object",
    #             "properties": {
    #                 "output_signal": {
    #                     "type": "string",
    #                     "description": "The output net whose cone should be optimised.",
    #                 },
    #                 "max_depth": {
    #                     "type": "integer",
    #                     "description": "Maximum allowed combinational depth after optimisation.",
    #                 },
    #             },
    #             "required": ["output_signal", "max_depth"],
    #         },
    #     },
    # },
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
            "name": "check_signal_constant",
            "description": (
                "Prove whether a scalar or complete bus is ALWAYS equal to a constant regardless "
                "of all primary inputs and DFF-Q boundary values. Invoke this exact tool for prompts "
                "such as 'is output n16 always 0?', 'is this signal constant?', or 'regardless of "
                "all inputs'. Do not use check_signal_equivalence for constant-property questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "signal_name": {
                        "type": "string",
                        "description": "Scalar signal, bit-select, or complete bus to prove.",
                    },
                    "value": {
                        "anyOf": [{"type": "integer"}, {"type": "string"}],
                        "description": "Expected constant, such as 0, 1, 8'b0, '0, or '1.",
                    },
                },
                "required": ["signal_name", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_signal_equivalence",
            "description": (
                "Compare TWO SIGNALS inside the current netlist for functional equivalence. "
                "Use only when the request explicitly names two signals; this does not compare designs. "
                "Uses Yosys SAT solver for precise equivalence checking on circuits of any size. "
                "Returns true if the signals are logically equivalent, false otherwise."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sig1": {
                        "type": "string",
                        "description": "First signal name to compare.",
                    },
                    "sig2": {
                        "type": "string",
                        "description": "Second signal name to compare.",
                    }
                },
                "required": ["sig1", "sig2"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_design_equivalence",
            "description": (
                "Prove that the CURRENT TRANSFORMED DESIGN is equivalent to the ORIGINAL NETLIST "
                "loaded by read_design. Invoke this exact non-mutating tool for requests such as "
                "'prove the transformed design is equivalent to the pre-transformation netlist' "
                "or 'verify whole-design equivalence'. It serializes the current netlist and runs "
                "Yosys whole-design sequential equivalence. Never call FRAIG for a proof request."
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
            "name": "rename_gate",
            "description": (
                "Rename a gate instance in the currently loaded netlist. "
                "Only the instance name changes — connections and logic are preserved exactly. "
                "Invoke this when asked to rename, relabel, or change the name of a gate instance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "old_name": {
                        "type": "string",
                        "description": "Current instance name of the gate to rename.",
                    },
                    "new_name": {
                        "type": "string",
                        "description": "New instance name for the gate.",
                    },
                },
                "required": ["old_name", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_wire",
            "description": (
                "Rename a wire or signal in the currently loaded netlist and update all references. "
                "Use this when asked to rename, relabel, or change the identifier of a wire/net. "
                "Only the signal name changes; gate and DFF connectivity is preserved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "old_name": {
                        "type": "string",
                        "description": "Current wire/signal name to rename.",
                    },
                    "new_name": {
                        "type": "string",
                        "description": "New wire/signal name.",
                    },
                },
                "required": ["old_name", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_gate_type_in_cone",
            "description": (
                "Replace all instances of a specific gate type (e.g. OR) in the fanin cone "
                "of an output signal with a functionally equivalent circuit built from the target gate types. "
                "Unlike remap_cone_with_gates, this ONLY touches the specified source gate type — "
                "all other gates in the cone remain unchanged. "
                "Use this when asked to 'replace OR gates with NAND+NOT' or 'convert XOR to NAND-only' "
                "within a cone or across the whole design. "
                "Set output_signal to null to apply across the entire design. "
                "Returns success, replaced (count), skipped (count)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_type": {
                        "type": "string",
                        "description": "Gate type to replace. E.g. 'or', 'xor', 'nor', 'and', 'not', 'buf'.",
                    },
                    "target_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Allowed gate types in the replacement. "
                            "E.g. ['nand', 'not'] or ['nor', 'not']. "
                            "Must form a functionally complete set for the source gate."
                        ),
                    },
                    "output_signal": {
                        "type": "string",
                        "description": (
                            "Cone root signal (e.g. 'n11[0]'). "
                            "Only gates in the fanin cone of this signal are replaced. "
                            "Omit or set to null to apply across the entire design."
                        ),
                    },
                },
                "required": ["source_type", "target_types"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fraig_merge_equivalent_gates",
            "description": (
                "Use Yosys+ABC FRAIG to find, formally prove, and merge functionally equivalent "
                "combinational nodes across the whole design. Invoke this exact tool when asked "
                "to merge functionally equivalent gates or equivalent gate pairs. The ABC flow "
                "runs FRAIG only; it does not run dc2, dch, balancing, depth optimization, or retiming. "
                "DFF boundaries and an active whole-design gate restriction are preserved."
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
            "name": "remap_design_with_gates",
            "description": (
                "Reconstruct the ENTIRE netlist/whole design using only the specified gate types. "
                "Invoke this exact tool when the request says 'entire netlist', 'whole design', "
                "or asks to restrict every combinational gate in the design. Do not pass a module "
                "name to remap_cone_with_gates for whole-design requests. DFFs and their boundary "
                "nets are preserved. Example: allowed_gates=['and', 'not']."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "allowed_gates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "The only combinational gate types permitted afterward. "
                            "Valid values: 'and', 'nand', 'nor', 'or', 'not', "
                            "'xor', 'xnor', and 'buf'."
                        ),
                    },
                },
                "required": ["allowed_gates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remap_cone_with_gates",
            "description": (
                "Re-implement the fanin cone of a given output signal using ONLY the specified gate types. "
                "This is only for a specific driven signal, never an entire module or whole design. "
                "Uses Yosys+ABC with a restricted Liberty library so the result is guaranteed to use "
                "only the allowed gates. Handles any source gate type (OR, NOR, XOR, etc.) automatically. "
                "Use this when asked to replace gates in a cone with a restricted set "
                "(e.g. 'replace all gates in cone of n11[0] with only NAND and NOT gates'). "
                "Returns success, gates_before, gates_after."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "output_signal": {
                        "type": "string",
                        "description": "The output net whose fanin cone will be remapped (e.g. 'n11[0]').",
                    },
                    "allowed_gates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of gate types the cone may use after remapping. "
                            "Valid values: 'nand', 'nor', 'or', 'not', 'xor', 'xnor', 'buf'. "
                            "Example: ['nand', 'not']"
                        ),
                    },
                },
                "required": ["output_signal", "allowed_gates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_signals",
            "description": (
                "Return lists of signal names available in the currently loaded netlist. "
                "Provides primary inputs, primary outputs, internal wires, gate outputs and DFF signals."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reduce_critical_path",
            "description": (
                "Invoke this without fail when asked to reduce, minimize or optimize the critical path depth "
                "or maximum logic depth of the design. "
                "Uses Yosys+ABC logic restructuring to reduce combinational depth. "
                "When the request says the netlist must remain restricted to specific gate types, "
                "pass those exact types in allowed_gates (for example ['and', 'not']). "
                "If omitted, a restriction established by remap_design_with_gates is inherited. "
                "No retiming or sequential changes are made — only combinational logic is restructured. "
                "Returns depth_before, depth_after, improvement and success."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "allowed_gates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional exclusive gate set for the optimized result. "
                            "Use whenever the prompt says the design must remain those gates only."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "auto_insert_buffers",
            "description": (
                "Automatically find nets with fanout > max_fanout and insert buffers so no gate drives more than max_fanout loads. "
                "If 'nets' is provided (array of net names), only those nets are processed; otherwise all known nets are checked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_fanout": {"type": "integer", "description": "Maximum allowed fanout per net."},
                    "nets": {"type": "array", "items": {"type": "string"}, "description": "Optional list of net names to process (default: all)."}
                },
                "required": ["max_fanout"],
            },
        },
    },
]
