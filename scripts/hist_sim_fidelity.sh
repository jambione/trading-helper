#!/bin/bash
# hist_sim_prereg.json fidelity gates (amended_6 + amended_7), mini only. Runs after the catch-up ratchet sweep.
#   1. hist_universe: daily cache to 10/14, u3 rebuild of the UNTOUCHED days 10/12-10/14 (quote_mode file, the same
#      knob the held-out batch builds with); admissions recall written as information; void rules enforced at the gate
#   2. synth_day_fidelity: export; 3 lanes: part A (rec + syn_first on 9/24-10/02, amended_4(e)) and part B
#      (rec + syn_hist on the untouched days); score A; gate -> ai_reports/hist_sim/FIDELITY.json
# Resume-safe (finished replays skipped; score/gate refuse outputs from another sha). Launch window 16:15-03:25 ET;
# a guard stops running replays at 03:25 and stays alive until this script exits.
#   nohup bash scripts/hist_sim_fidelity.sh [extra untouched days...] > /tmp/fid_run.log 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."
OUT="$PWD/ai_reports/hist_sim/fid_u3"     # absolute: replay_session absolutizes --out for its child (guard match)
COST="$OUT/costing_cache.json"
CAL="2026-09-24 2026-09-25 2026-09-28 2026-09-29 2026-09-30 2026-10-01 2026-10-02"
NEW="2026-10-12 2026-10-13 2026-10-14 $*"
PY=.venv/bin/python
SF=tools/studies/synth_day_fidelity.py
mkdir -p "$OUT"
ok_window() { HM=$(TZ=America/New_York date +%H%M); [ "$HM" -ge 1615 ] || [ "$HM" -lt 0325 ]; }
until ok_window; do sleep 120; done
while pgrep -f "knobs/one.sh|ratchet_sweep.sh" > /dev/null; do sleep 60; done
SHA=$(git rev-parse --short HEAD)
( while :; do ok_window || { pkill -f "synth_day_fidelity.py run"; pkill -f "replay_session.py --day .* --out $OUT"; }; sleep 60; done ) &
GUARD=$!
trap 'kill $GUARD 2>/dev/null' EXIT
echo "sha $SHA out $OUT days A [$CAL] B [$NEW] $(date)"
INFO=ai_reports/hist_universe/check_u3_untouched_file_INFO.json
WANT_SPAN="$(echo $NEW | tr ' ' '\n' | sort | head -1)..$(echo $NEW | tr ' ' '\n' | sort | tail -1)"
HAVE=$($PY -c "import json,sys; j=json.load(open(sys.argv[1])); print(j.get('span',''), j.get('git_sha',''))" "$INFO" 2>/dev/null || true)
if [ "$HAVE" != "$WANT_SPAN $SHA" ]; then     # rebuild when the graded days OR the code sha changed
  # cmd_daily MERGES into the cache (never replaces the January-October history the builds read).
  # hi = the last graded day: the free plan refuses SIP bars from the last 15 minutes (a future end date fails).
  LASTDAY=$(echo $NEW | tr ' ' '\n' | sort | tail -1)
  $PY tools/studies/hist_universe.py daily 2026-10-10 "$LASTDAY" || exit 1
  $PY - "$NEW" <<'PY' || exit 1
import sys, pickle
sys.path.insert(0, "tools/studies")
import hist_universe as HU
d = pickle.load(open(HU.daily_path("sip"), "rb"))["data"].get("SPY") or []
last = sorted(sys.argv[1].split())[-1]
assert d and d[0][0] <= "2026-01-05" and d[-1][0] >= last, f"daily cache SPY span {d[:1]}..{d[-1:]} does not cover Jan..{last}"
print("daily cache SPY", d[0][0], "..", d[-1][0])
PY
  $PY - "$NEW" <<'PY' || exit 1
import sys, json
sys.path.insert(0, "tools/studies")
import hist_universe as HU
assert HU.RULE_VERSION.startswith("hist_universe/u3"), HU.RULE_VERSION
days = sorted(sys.argv[1].split())
r = HU.cmd_check(f"{days[0]}..{days[-1]}", knobs={"quote_mode": "file"}, out_name="check_u3_untouched_file_INFO")
print("u3 untouched (information under amended_6):", json.dumps({k: r[k] for k in ("pooled_pct", "void", "git_sha")}))
PY
fi
$PY $SF export --out $OUT $CAL $NEW || exit 1
pids=()
for k in 0 1 2; do
  ( ok_window && $PY $SF run --out $OUT --lane $k/3 --leg rec --sha $SHA $CAL $NEW
    ok_window && $PY $SF run --out $OUT --lane $k/3 --leg syn --universe-kind first --sha $SHA $CAL
    ok_window && $PY $SF run --out $OUT --lane $k/3 --leg syn --universe-kind hist --sha $SHA $NEW ) &
  pids+=($!)
done
wait "${pids[@]}"
ok_window || { echo "deadline: resume next evening (re-run this script)"; exit 0; }
$PY $SF score --out $OUT --cost-cache $COST --sha $SHA $CAL && \
  $PY $SF gate --out $OUT --cost-cache $COST --sha $SHA --dest ai_reports/hist_sim/FIDELITY.json $NEW
