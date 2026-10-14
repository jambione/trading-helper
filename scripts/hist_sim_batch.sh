#!/bin/bash
# Held-out synthetic batch for docs/studies/hist_sim_prereg.json (the "scripts/hist_sim_batch.sh (to be
# written)" it names). Mini only. REFUSES to run until both fidelity gates have passed:
#   B: ai_reports/hist_universe/check_u2_untouched_file_GRADED.json  pass_pooled all true, void empty
#   A: ai_reports/hist_sim/FIDELITY.json  (synth_day_fidelity.py gate --dest ai_reports/hist_sim/FIDELITY.json)
#      {"verdict": "PASS"} - that document requires both its part A (engine) and part B (end to end)
# Then, newest held-out day first (2026-09-23 back to 2026-03-02), per day:
#   1. the point-in-time universe (hist_universe build_day with the FROZEN config)
#   2. variants base, wr15, wrrsi, st, nodecay, wrrsi_st  (one pass)
#   3. rand_wrrsi (PRIMARY's trade-less control) and rand_wr15 (information, amended_5) at the share
#      of base trades the matching filter removed that day (two-pass)
# Discipline: after 16:15 ET, no launches 03:30-16:15, <= 3 replay_session runs on the box.
#   nohup bash scripts/hist_sim_batch.sh > /tmp/hist_sim_batch.log 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=ai_reports/hist_sim
UNI=ai_reports/hist_universe
mkdir -p "$OUT"   # layout tools/studies/hist_sim_read.py (dfb0646) reads: DIR/DAY-VARIANT.json, DIR/DAY-dayinfo.json, DIR/FIDELITY.json
FROZEN="$OUT/frozen_config.json"
MAXP=3

EQUITY=2253.32   # amended_5: pinned (the recorded paper equity, WS_R_STATUS); never today's live state file
while [ "$(TZ=America/New_York date +%H%M)" -lt 1615 ] && [ "$(TZ=America/New_York date +%H%M)" -ge 0330 ]; do sleep 120; done

# ── gates ──
.venv/bin/python - <<'PY'
import json, os, sys
a = "ai_reports/hist_sim/FIDELITY.json"
# amended_6: gate B (traded-name recall + trade-result conditions on untouched 10/12-10/14) lives in FIDELITY.json
# beside gate A; the u2 admissions-recall file is information. Run scripts/hist_sim_fidelity.sh first.
if not os.path.exists(a) or json.load(open(a)).get("verdict") != "PASS":
    sys.exit("fidelity gates A+B not passed or not run - held-out batch refused")
print("gates A and B passed")
PY

# ── frozen config (once): the live config in force at the 2026-10-09 close, sha logged ──
if [ ! -f "$FROZEN" ]; then
  .venv/bin/python - <<'PY'
import sys, json, hashlib
sys.path.insert(0, "tools/studies")
import hist_universe as HU
cfg_at, note, _ = HU.recorded_config_fn("2026-10-09")
c = cfg_at(HU.N_MIN - 1)
json.dump(c, open("ai_reports/hist_sim/frozen_config.json", "w"), indent=1, sort_keys=True)
sha = hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()
open("ai_reports/hist_sim/frozen_config.sha256", "w").write(f"{sha}  {note}\n")
print("frozen config", sha[:16], note)
PY
fi

wait_window() { while :; do HM=$(TZ=America/New_York date +%H%M); { [ "$HM" -ge 1615 ] || [ "$HM" -lt 0330 ]; } && return 0; return 1; done; }
throttle() { while [ "$(pgrep -f 'tools/replay_session.py --day' | wc -l)" -ge $((MAXP * 2)) ]; do sleep 20; done; }

run() {   # run DAY NAME [--set ...]
  local d=$1 name=$2; shift 2
  local f="$OUT/${d}-${name}.json"
  [ -f "$f" ] && return 0
  wait_window || { echo "deadline: $d $name not launched" >> "$OUT/failures.txt"; return 0; }
  throttle
  ( .venv/bin/python tools/replay_session.py --day "$d" --universe "$UNI/${d}.json" --config-file "$FROZEN" \
      --engine recompute --engine-bars iex --allow-held-out --equity "$EQUITY" "$@" --out "$f" > "$OUT/${d}-${name}.log" 2>&1 \
    || echo "FAILED $d $name" >> "$OUT/failures.txt" ) &
  sleep 5
}

share() {  # share DAY FILTERED: 1 - filtered trades / base trades
  .venv/bin/python -c "import json,sys; b=len(json.load(open('$OUT/$1-base.json'))['closed']); w=len(json.load(open('$OUT/$1-$2.json'))['closed']); print(max(0.0, round(1-w/b, 4)) if b else 0.0)"
}

DAYS=$(.venv/bin/python -c "
import sys; sys.path.insert(0,'tools/studies'); import hist_universe as HU
print(' '.join(reversed(HU.trading_days('2026-03-02','2026-09-23'))))")

for d in $DAYS; do
  wait_window || break
  if [ ! -f "$UNI/${d}.json" ]; then
    .venv/bin/python -c "
import sys; sys.path.insert(0,'tools/studies'); import hist_universe as HU
HU.cmd_build_range('$d','$d', config_path='$FROZEN')" >> "$OUT/universe_build.log" 2>&1 || { echo "universe failed $d" >> "$OUT/failures.txt"; continue; }
  fi
  [ -f "$OUT/${d}-dayinfo.json" ] || .venv/bin/python -c "
import json; u=json.load(open('$UNI/${d}.json'))
json.dump({'spy_missing_share': u.get('spy_missing_share'), 'universe': u.get('universe_size'), 'fetch_failed': u.get('failed_fetch_names')}, open('$OUT/${d}-dayinfo.json','w'))"
  run "$d" base
  run "$d" wr15     --set ai_watch_wr_trend_min_rise=15
  run "$d" wrrsi    --set ai_watch_wr_trend_min_rise=15 --set ai_watch_wr_rsi_min_rise=10
  run "$d" st       --set ai_local_trail_time_decay_enabled=false --set ai_exit_supertrend=true
  run "$d" nodecay  --set ai_local_trail_time_decay_enabled=false
  run "$d" wrrsi_st --set ai_watch_wr_trend_min_rise=15 --set ai_watch_wr_rsi_min_rise=10 --set ai_local_trail_time_decay_enabled=false --set ai_exit_supertrend=true
  wait
  [ -f "$OUT/${d}-base.json" ] || continue
  [ -f "$OUT/${d}-wrrsi.json" ] && run "$d" rand_wrrsi --set ai_watch_wr_rand_refuse_share="$(share "$d" wrrsi)"
  [ -f "$OUT/${d}-wr15.json" ]  && run "$d" rand_wr15  --set ai_watch_wr_rand_refuse_share="$(share "$d" wr15)"
done
wait
echo "batch finished $(date); score with: .venv/bin/python tools/studies/hist_sim_read.py --dir $OUT"
