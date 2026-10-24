#!/bin/bash
# Batch 2 on the mini, unattended (operator 10/24: "let the mini do the work").
# Order per harness: planted-edge canary -> outcome-blind count -> score (each script refuses to score without a
# passing canary, the floors and the reviewer's READY string). Then the T1 lag audit (diagnostic).
# Usage: batch2_runner.sh "<READY string>" <prereg commit>
set -u
READY="$1"; PREREG="$2"
cd ~ || exit 1
export EEH_ROOT=$HOME/repo/trading-helper
P=repo/trading-helper/.venv/bin/python
S=studies_bin/fib
L=repo/trading-helper/ai_reports/batch2_logs
mkdir -p "$L"
stamp() { date "+%Y-%m-%d %H:%M:%S"; }
run() { echo "== $(stamp) $*" >> "$L/runner.log"; nice -n 10 "$@" >> "$L/runner.log" 2>&1; echo "rc $? $(stamp)" >> "$L/runner.log"; }

run $P $S/batch2.py --canary --workers 4
run $P $S/batch2.py --workers 4 --prereg-commit "$PREREG"
run $P $S/batch2.py --score --skeptic-ready "$READY" --workers 4
run $P $S/batch2_etf.py --canary
run $P $S/batch2_etf.py --prereg-commit "$PREREG"
run $P $S/batch2_etf.py --score --skeptic-ready "$READY"
run $P $S/lag_audit.py
echo "done $(stamp)" > "$L/runner.done"
