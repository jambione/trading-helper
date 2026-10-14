#!/bin/bash
# Catch-up ratchet (time-decay leash) sweep (docs/studies/ratchet_sweep_prereg.json), mini only.
# Pins the WHOLE live config of $CFG_SHA via --set-json, varies only the spread cap (admit/arm and the tight
# screen together) and the arm RVOL floor. Waits for 16:15 ET AND for the evening batch / per-knob replays,
# keeps <= 3 replays of its own (<= 6 on the box), never launches 03:30-16:15 ET (unlaunched arms roll to
# the next evening on a re-run: finished outputs are skipped), then scores.
#   nohup bash scripts/ratchet_sweep.sh [OUT] > /tmp/rat_run.log 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."
CFG_SHA=94f2576
SHA=$(git rev-parse --short HEAD)
OUT=${1:-/tmp/rat_$(date +%Y%m%d)}
mkdir -p "$OUT"
MAXP=3
git show "$CFG_SHA:config/bot_config.json" > "$OUT/base_config.json"
N=$(.venv/bin/python -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$OUT/base_config.json")
if [ "$N" -lt 300 ]; then echo "base config has only $N keys - refusing to run unpinned"; exit 1; fi
git diff --quiet HEAD -- || { echo "tracked files differ from HEAD - commit first"; exit 1; }
echo "sha $SHA cfg $CFG_SHA keys $N out $OUT"

while [ "$(TZ=America/New_York date +%H%M)" -lt 1615 ] && [ "$(TZ=America/New_York date +%H%M)" -ge 0400 ]; do sleep 120; done
while pgrep -f "knobs/one.sh" > /dev/null; do sleep 60; done   # operator 10/14: the ratchet runs first, before the evening batch

arm_sets() {   # the leash only; everything else is the pinned live config (idle 4 s, step 0.05R, overtake on, cushion $0.01)
  case $1 in
    base)      echo "" ;;
    ot_off)    echo "--set ai_local_trail_decay_overtake=false" ;;
    idle15)    echo "--set ai_local_trail_decay_idle_sec=15" ;;
    idle30)    echo "--set ai_local_trail_decay_idle_sec=30" ;;
    idle60)    echo "--set ai_local_trail_decay_idle_sec=60" ;;
    cush10)    echo "--set ai_local_trail_decay_overtake=false --set ai_local_trail_decay_cushion_pct=0.10" ;;
    cush25)    echo "--set ai_local_trail_decay_overtake=false --set ai_local_trail_decay_cushion_pct=0.25" ;;
    cush25i30) echo "--set ai_local_trail_decay_overtake=false --set ai_local_trail_decay_cushion_pct=0.25 --set ai_local_trail_decay_idle_sec=30" ;;
    nodecay)   echo "--set ai_local_trail_time_decay_enabled=false" ;;
    *) echo "unknown arm $1" >&2; exit 1 ;;
  esac
}

for d in 2026-10-06 2026-10-07 2026-10-08 2026-10-09 2026-10-12 2026-10-13; do
  for a in base ot_off idle15 idle30 idle60 cush10 cush25 cush25i30 nodecay; do
    [ -s "$OUT/${d}_${a}.json" ] && continue
    HM=$(TZ=America/New_York date +%H%M)
    if [ "$HM" -ge 0330 ] && [ "$HM" -lt 1615 ]; then echo "deadline: not launched $d $a" >> "$OUT/failures.txt"; continue; fi
    while [ "$(pgrep -f 'tools/replay_session.py --day' | wc -l)" -ge $((MAXP * 2)) ]; do sleep 20; done
    # shellcheck disable=SC2046
    ( .venv/bin/python tools/replay_session.py --day "$d" --sha "$SHA" --set-json "$OUT/base_config.json" \
        $(arm_sets "$a") --out "$OUT/${d}_${a}.json" > "$OUT/${d}_${a}.log" 2>&1 \
      || echo "FAILED $d $a" >> "$OUT/failures.txt" ) &
    sleep 5
  done
done
wait
HM=$(TZ=America/New_York date +%H%M)
if [ "$HM" -ge 0400 ] && [ "$HM" -lt 1615 ]; then echo "scoring deferred (quote fetches need the evening)"; exit 0; fi
.venv/bin/python tools/studies/spread_rvol_score.py --dir "$OUT" --arms base,ot_off,idle15,idle30,idle60,cush10,cush25,cush25i30,nodecay --refs base | tee "$OUT/result.md"
