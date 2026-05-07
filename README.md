# cada0001_alpha

LLM-assisted EDA netlist exploration and transformation system.  
ICCAD 2026 Contest Problem A — Reference Implementation.

---

## Installation

### 1. Prerequisites

- Python 3.9 or later
- An OpenAI API key

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure the API key

Edit `config.yaml` and replace `YOUR_OPENAI_API_KEY_HERE` with your actual key:

```yaml
provider: "openai"
openai:
  api_key: "sk-..."
  model: "gpt-4o-mini"
```

Alternatively, export the environment variable:

```bash
export OPENAI_API_KEY="sk-..."
```

---

## Running the tool

```bash
./cada0001_alpha -config config.yaml
```

The tool reads natural-language requests from **stdin** (one per line) and
writes tagged responses to **stdout**.

### Piping a test case

```bash
./cada0001_alpha -config config.yaml < examples/test8_stdin.txt
```

---

## Output format

Every response is wrapped in `#RESPONSE` / `#END` tags:

```
#RESPONSE 1
Testcase name has been set to 'test8'. Log file: test8.log
#END 1

#RESPONSE 2
Design loaded from 'design/netlist/test8.v'.
#END 2

#RESPONSE 3
The maximum logic depth from in0 to out3 is 5 gate levels.
Path: in0 → n_in0_n → n1 → n2 → n3 → out3
#END 3
```

Responses are also mirrored to `<case_name>.log` once the testcase name
is set (via the `set_testcase_name` tool call triggered by the first request).

---

## Running the integration tests

```bash
bash run_examples.sh
```

The script runs both example testcases and checks that the corresponding
`.log` files are created.  It prints `PASS` or `FAIL` for each.

---

## Supported EDA operations

| Tool | Description |
|------|-------------|
| `read_design` | Load a gate-level Verilog netlist |
| `write_design` | Write the (modified) netlist back to disk |
| `set_testcase_name` | Set the active case name and open the log file |
| `get_max_depth` | Longest combinational path between two signals |
| `path_passes_through` | Check if all paths pass through a waypoint |
| `find_path_avoiding` | Find a path that avoids a specific signal |
| `get_logic_cone` | All gates in the transitive fanin of an output |
| `count_cone_gates` | Gate count of a logic cone |
| `get_fanout` | All gates driven by a net |
| `are_same_clock_domain` | Check if two DFFs share the same clock |
| `insert_gate_before` | Insert a new gate before an existing gate |
| `replace_gate` | Change the type of an existing gate in-place |
| `insert_buffers_for_fanout` | Add buffer trees to limit net fanout |
| `balance_depth` | Equalise path depths with buffers |
| `remove_dangling_gates` | Remove unreachable gates and nets |
| `optimize_cone_depth` | Restructure a cone to meet a depth constraint |
| `replace_pattern` | Bulk gate-type replacement by pattern |
| `find_instances_by_name_pattern` | Search instances by type and name regex |
| `check_equivalence` | Verify functional equivalence against a snapshot |

---

## Supported Verilog subset

The parser handles flat, single-module gate-level Verilog with:

- Primitive gates: `and`, `or`, `nand`, `nor`, `not`, `buf`, `xor`, `xnor`
- Flip-flops: `dff` instances with ports `(clk, rst_n, d, q)`
- Scalar and bus wire / port declarations (e.g. `wire [31:0] a`)
- Constants: `1'b0`, `1'b1`

---

## Project structure

```
cada0001_alpha/
├── cada0001_alpha          ← main executable
├── config.yaml             ← LLM config (set your API key here)
├── requirements.txt
├── run_examples.sh         ← integration test runner
├── src/
│   ├── __init__.py
│   ├── agent.py            ← LLM agent (OpenAI function-calling)
│   ├── eda_engine.py       ← EDA analysis & transformation
│   ├── netlist_parser.py   ← Verilog parser / writer
│   ├── io_handler.py       ← stdin/stdout/log handler
│   └── tools.py            ← OpenAI tool schemas
├── design/
│   └── netlist/
│       ├── test8.v
│       └── test35.v
└── examples/
    ├── test8_stdin.txt
    └── test35_stdin.txt
```
