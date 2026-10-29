#!/bin/zsh
# 2026-10-29 03:xx: follow-up after the 10/28 queue (stale_age and slope_now fidelity failed; late_gates UNDERPOWERED).
# (1) late_gates primary on the new forward session 10/28 (adds one session toward the 24 it needs);
# (2) slope_now REF in explore-only mode + the exploration table (range / range-per-spread; pitch splits flagged).
cd ~/repo/trading-helper || exit 1
STOP=08:45
say() { echo "[early] $(TZ=America/New_York date +%H:%M:%S) $*"; }
say "start"
scripts/late_gates_replay.sh primary --sha 21e6f763 --jobs 3 --stop-at $STOP
say "late_gates primary (10/28) done"
.venv/bin/python tools/studies/slope_now_run.py ref --explore-only --sha 5d3ff37002b25277ea183211cfd90fa797c3c200 --jobs 3 --stop-at $STOP
.venv/bin/python tools/studies/slope_now_explore.py > logs/slope_now_explore-2026-10-28.log 2>&1
say "slope_now explore done"
