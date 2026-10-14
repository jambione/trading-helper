#!/bin/bash
# hist_sim_prereg.json fidelity gates (amended_6), mini only. Runs after the catch-up ratchet sweep (operator order 10/14).
#   1. hist_universe: daily cache to 10/14, then the u3 rebuild of the UNTOUCHED days 10/12-10/14 (quote_mode file;
#      admissions recall written as information) -> calib_DAY.json for part B
#   2. synth_day_fidelity: export, then 3 lanes: part A (rec + syn_first on the 12 calibration days) and part B
#      (rec + syn_hist on 10/12-10/14); score A; gate -> ai_reports/hist_sim/FIDELITY.json
# Resume-safe (finished replays are skipped). No launches 03:30-16:15 ET; running replays are stopped at 03:25.
#   nohup bash scripts/hist_sim_fidelity.sh > /tmp/fid_run.log 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."
OUT=/tmp/fid_u3
CAL="2026-09-24 2026-09-25 2026-09-28 2026-09-29 2026-09-30 2026-10-01 2026-10-02 2026-10-05 2026-10-06 2026-10-07 2026-10-08 2026-10-09"
NEW="2026-10-12 2026-10-13 2026-10-14"
SHA=$(git rev-parse --short HEAD)
PY=.venv/bin/python
SF=tools/studies/synth_day_fidelity.py
ok_window() { HM=$(TZ=America/New_York date +%H%M); [ "$HM" -ge 1615 ] || [ "$HM" -lt 0330 ]; }
until ok_window; do sleep 120; done
while pgrep -f "knobs/one.sh|ratchet_sweep.sh" > /dev/null; do sleep 60; done
# Deadline guard: never run into the next premarket (shared 200/min Alpaca budget).
( while :; do HM=$(TZ=America/New_York date +%H%M); if [ "$HM" -ge 0325 ] && [ "$HM" -lt 1615 ]; then
    pkill -f "synth_day_fidelity.py run"; pkill -f "replay_session.py --day .* --out $OUT"; exit 0; fi; sleep 60; done ) &
GUARD=$!
echo "sha $SHA out $OUT $(date)"
if [ ! -f ai_reports/hist_universe/check_u3_untouched_file_INFO.json ]; then
  $PY tools/studies/hist_universe.py daily 2026-10-10 2026-10-14 || exit 1
  $PY - <<'PY' || exit 1
import sys, json
sys.path.insert(0, "tools/studies")
import hist_universe as HU
assert HU.RULE_VERSION.startswith("hist_universe/u3"), HU.RULE_VERSION
r = HU.cmd_check("2026-10-12..2026-10-14", knobs={"quote_mode": "file"}, out_name="check_u3_untouched_file_INFO")
print("u3 untouched (information under amended_6):", json.dumps({k: r[k] for k in ("pooled_pct", "void", "git_sha")}))
PY
fi
$PY $SF export --out $OUT $CAL $NEW || exit 1
for k in 0 1 2; do
  ( ok_window && $PY $SF run --out $OUT --lane $k/3 --leg rec --sha $SHA $CAL $NEW
    ok_window && $PY $SF run --out $OUT --lane $k/3 --leg syn --universe-kind first --sha $SHA $CAL
    ok_window && $PY $SF run --out $OUT --lane $k/3 --leg syn --universe-kind hist --sha $SHA $NEW ) &
done
wait $(jobs -p | grep -v "^$GUARD$") 2>/dev/null
ok_window || { echo "deadline: resume next evening (re-run this script)"; exit 0; }
$PY $SF score --out $OUT $CAL && $PY $SF gate --out $OUT --dest ai_reports/hist_sim/FIDELITY.json $NEW
kill $GUARD 2>/dev/null
