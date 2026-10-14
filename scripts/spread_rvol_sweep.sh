#!/bin/bash
# Spread cap x arm RVOL floor sweep (docs/studies/spread_rvol_sweep_prereg.json), mini only.
# Pins the WHOLE live config of $CFG_SHA via --set-json, varies only the spread cap (admit/arm and the tight
# screen together) and the arm RVOL floor. Waits for 16:15 ET AND for the evening batch / per-knob replays,
# keeps <= 3 replays of its own (<= 6 on the box), never launches 03:30-16:15 ET (unlaunched arms roll to
# the next evening on a re-run: finished outputs are skipped), then scores.
#   nohup bash scripts/spread_rvol_sweep.sh [OUT] > /tmp/srs_run.log 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."
CFG_SHA=94f2576
SHA=$(git rev-parse --short HEAD)
OUT=${1:-/tmp/srs_$(date +%Y%m%d)}
mkdir -p "$OUT"
MAXP=3
git show "$CFG_SHA:config/bot_config.json" > "$OUT/base_config.json"
N=$(.venv/bin/python -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$OUT/base_config.json")
if [ "$N" -lt 300 ]; then echo "base config has only $N keys - refusing to run unpinned"; exit 1; fi
git diff --quiet HEAD -- || { echo "tracked files differ from HEAD - commit first"; exit 1; }
echo "sha $SHA cfg $CFG_SHA keys $N out $OUT"

while [ "$(TZ=America/New_York date +%H%M)" -lt 1615 ] && [ "$(TZ=America/New_York date +%H%M)" -ge 0400 ]; do sleep 120; done
while pgrep -f "no_green_scratch_replays.sh|knobs/one.sh|ratchet_sweep.sh|hist_sim_fidelity.sh" > /dev/null; do sleep 60; done

arm_sets() {   # sNN = spread cap %, rNN = arm RVOL floor (as recorded live, see prereg)
  case $1 in
    s05r125) echo "--set ai_watch_max_sip_spread_pct=0.05 --set ai_tight_max_spread_pct=0.05 --set ai_watch_arm_min_rvol=1.25" ;;
    s15r125) echo "--set ai_watch_max_sip_spread_pct=0.15 --set ai_tight_max_spread_pct=0.15 --set ai_watch_arm_min_rvol=1.25" ;;
    s25r125) echo "--set ai_watch_max_sip_spread_pct=0.25 --set ai_tight_max_spread_pct=0.25 --set ai_watch_arm_min_rvol=1.25" ;;
    s05r06)  echo "--set ai_watch_max_sip_spread_pct=0.05 --set ai_tight_max_spread_pct=0.05 --set ai_watch_arm_min_rvol=0.6" ;;
    s15r06)  echo "--set ai_watch_max_sip_spread_pct=0.15 --set ai_tight_max_spread_pct=0.15 --set ai_watch_arm_min_rvol=0.6" ;;
    s25r06)  echo "--set ai_watch_max_sip_spread_pct=0.25 --set ai_tight_max_spread_pct=0.25 --set ai_watch_arm_min_rvol=0.6" ;;
    s15r095) echo "--set ai_watch_max_sip_spread_pct=0.15 --set ai_tight_max_spread_pct=0.15 --set ai_watch_arm_min_rvol=0.95" ;;
    *) echo "unknown arm $1" >&2; exit 1 ;;
  esac
}

for d in 2026-10-06 2026-10-07 2026-10-08 2026-10-09 2026-10-12 2026-10-13; do
  for a in s05r125 s15r125 s25r125 s05r06 s15r06 s25r06 s15r095; do
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
.venv/bin/python tools/studies/spread_rvol_score.py --dir "$OUT" | tee "$OUT/result.md"
