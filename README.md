# cada1067_alpha

---

## Installation

### 1. Prerequisites

- Python 3.10 or later
- An OpenAI API key
- Yosys (for equivalence checking)

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
or
create a .env file with content OPENAI_API_KEY="sk-..."
```

---

## Running the tests

Download the testcase from ICCAD contest page and place it in the root directory

Initially use examples folder for analysing single testcases


```bash
bash run.sh examples
```

The script creates an output folder named examples_output which contains the log files of the folder examples

For running all testcases use the following command after downloading the testcase. (Don't run it always because llm api tokens are limited, Run only once and analyse the results one by one by putting it inside examples folder)

```
bash run.sh testcase

```

The script runs testcases and checks that the corresponding
`.log` files are created.

The script creates an output folder named testcase_output which contains the log files of the folder testcase

---

## Analysing the Log file

The logs directory contains all the LLM tool mode runs for all testcases for developer mode turned on in config

## Running the tool (For bundled version)

```bash
./cada1067_alpha -config config.yaml
```

The tool reads natural-language requests from **stdin** (one per line) and
writes tagged responses to **stdout**.

### Piping a test case

```bash
./cada1067_alpha -config config.yaml < examples/test8_stdin.txt
```

---

## Output format

Every response is wrapped in `#RESPONSE` / `#END` tags:

```
#RESPONSE 1
Testcase name has been set to 'test8'. Log file: test8.log
#END 1

#RESPONSE 2
Design loaded from 'examples/test8/test8.v'.
#END 2

#RESPONSE 3
The maximum logic depth from in0 to out3 is 5 gate levels.
Path: in0 → n_in0_n → n1 → n2 → n3 → out3
#END 3
```


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
| `check_equivalence` | Check if two signals in the netlist are functionally equivalent |

---

## Supported Verilog subset

The parser handles flat, single-module gate-level Verilog with:

- Primitive gates: `and`, `or`, `nand`, `nor`, `not`, `buf`, `xor`, `xnor`
- Flip-flops: `dff` instances with named ports `.RN()`, `.SN()`, `.CK()`, `.D()`, `.Q()` or legacy ordered ports `(clk, rst_n, d, q)`
- Scalar and bus wire / port declarations (e.g. `wire [31:0] a`)
- Constants: `1'b0`, `1'b1`

---

## Project structure

```
cada1067_alpha/
├── cada1067_alpha          ← main executable
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
├── logs                    ← human readable framework input and output logs for analysis (developer mode only)
└── examples/
    └── test1/
        ├── prompts.txt
        └── test1.v
```
