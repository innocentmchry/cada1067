#!/usr/bin/env bash
# run.sh — Unified integration test runner for cada0001_alpha
#
# Usage:
#   bash run.sh examples     # runs testcases in examples/
#   bash run.sh testcase     # runs testcases in testcase/
#
# Requires: config.yaml with a valid OpenAI API key, and Python >=3.9.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

INPUT_DIR="$1"

if [[ ! -d "$INPUT_DIR" ]]; then
    echo "ERROR: Input directory not found: $INPUT_DIR"
    exit 1
fi

PASS=0
FAIL=0

run_test() {
    local name="$1"
    local stdin_file="$2"
    local log_file="${name}.log"

    rm -f "$log_file"

    ./cada1067_alpha -config config.yaml < "$stdin_file"

    if [[ -f "$log_file" ]]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
    fi
}

# ---------------------------------------------------------------------------
# Loop over all test folders in the input directory
# ---------------------------------------------------------------------------
for test_dir in "${INPUT_DIR}"/test*/; do
    test_name="$(basename "$test_dir")"
    prompt_file="${test_dir}/prompt.txt"

    if [[ -f "$prompt_file" ]]; then
        run_test "$test_name" "$prompt_file"
    else
        echo "[SKIP] $test_name — no prompt.txt found in $test_dir"
    fi
done

if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
exit 0
