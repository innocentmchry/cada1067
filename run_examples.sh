#!/usr/bin/env bash
# run_examples.sh — Integration test runner for cada0001_alpha
#
# Usage:
#   bash run_examples.sh
#
# Requires: config.yaml with a valid OpenAI API key, and Python >=3.9.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PASS=0
FAIL=0

run_test() {
    local name="$1"
    local stdin_file="$2"
    local log_file="${name}.log"

    echo "--------------------------------------------------------------"
    echo "Running testcase: $name"
    echo "  stdin:   $stdin_file"
    echo "  log:     $log_file"
    echo "--------------------------------------------------------------"

    # Remove stale log
    rm -f "$log_file"

    python3 cada0001_alpha -config config.yaml < "$stdin_file"

    if [[ -f "$log_file" ]]; then
        echo "[PASS] $name — log file created: $log_file"
        PASS=$((PASS + 1))
    else
        echo "[FAIL] $name — log file NOT found: $log_file"
        FAIL=$((FAIL + 1))
    fi
    echo ""
}

# ---------------------------------------------------------------------------
# Test 8
# ---------------------------------------------------------------------------
run_test "test8" "examples/test8_stdin.txt"

# ---------------------------------------------------------------------------
# Test 35
# ---------------------------------------------------------------------------
run_test "test35" "examples/test35_stdin.txt"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "=============================="
echo "Results: PASS=$PASS  FAIL=$FAIL"
echo "=============================="

if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
exit 0
