#!/bin/bash
# Exit-cut sweep (docs/studies/exit_cut_sweep_prereg.json), mini only: no-progress / double check / gated variants.
# Pins the WHOLE live config of HEAD via --set-json, varies only the exit-cut knobs. Waits for 16:15 ET AND for the evening batch / per-knob replays,
# keeps <= 3 replays of its own (<= 6 on the box), never launches 03:30-16:15 ET (unlaunched arms roll to
# the next evening on a re-run: finished outputs are skipped), then scores.
#   nohup bash scripts/exit_cut_sweep.sh [OUT] > /tmp/ecs_run.log 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."
SHA=${SWEEP_SHA:-$(git rev-parse --short HEAD)}   # SWEEP_SHA pins a relaunch to the first run's code
CFG_SHA=$SHA
OUT=${1:-/tmp/ecs_$(date +%Y%m%d)}
mkdir -p "$OUT"
MAXP=${SWEEP_MAXP:-7}
git show "$CFG_SHA:config/bot_config.json" > "$OUT/base_config.json"
N=$(.venv/bin/python -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$OUT/base_config.json")
if [ "$N" -lt 300 ]; then echo "base config has only $N keys - refusing to run unpinned"; exit 1; fi
[ -n "${SWEEP_SHA:-}" ] || git diff --quiet HEAD -- || { echo "tracked files differ from HEAD - commit first"; exit 1; }
echo "sha $SHA cfg $CFG_SHA keys $N out $OUT"

# SWEEP_ANYTIME=1 (operator 10/16 05:00): launch now, but never start a replay 09:15-16:15 (the desk needs the budget).
if [ -z "${SWEEP_ANYTIME:-}" ]; then
  while [ "$(TZ=America/New_York date +%H%M)" -lt 1615 ] && [ "$(TZ=America/New_York date +%H%M)" -ge 0400 ]; do sleep 120; done
fi
while pgrep -f "knobs/one.sh" > /dev/null; do sleep 60; done

arm_sets() {   # base = HEAD config (no-progress off, double check off)
  local OFF="--set ai_no_progress_flatten_enabled=false --set ai_double_check_sell_enabled=false --set ai_no_progress_require_double_check=false --set ai_trend_reentry_idle_sec=0 --set ai_trail_exit_trend_hold=false --set ai_watch_profile_path="
  case $1 in
    off)      echo "$OFF" ;;
    np180)    echo "$OFF --set ai_no_progress_flatten_enabled=true --set ai_no_progress_sec=180" ;;
    dc180)    echo "$OFF --set ai_double_check_sell_enabled=true --set ai_double_check_min_sec=180" ;;
    np_dc)    echo "$OFF --set ai_no_progress_flatten_enabled=true --set ai_no_progress_sec=180 --set ai_no_progress_require_double_check=true" ;;
    np120_dc) echo "$OFF --set ai_no_progress_flatten_enabled=true --set ai_no_progress_sec=120 --set ai_no_progress_require_double_check=true" ;;
    np300_dc) echo "$OFF --set ai_no_progress_flatten_enabled=true --set ai_no_progress_sec=300 --set ai_no_progress_require_double_check=true" ;;
    trend60)  echo "$OFF --set ai_trend_reentry_idle_sec=60 --set ai_trend_reentry_min_pct=2.0" ;;
    trend120) echo "$OFF --set ai_trend_reentry_idle_sec=120 --set ai_trend_reentry_min_pct=2.0" ;;
    trail_dc) echo "$OFF --set ai_trail_exit_trend_hold=true --set ai_trail_exit_trend_hold_band_pct=0.05" ;;
    trail_dc_np_dc) echo "$OFF --set ai_trail_exit_trend_hold=true --set ai_trail_exit_trend_hold_band_pct=0.05 --set ai_no_progress_flatten_enabled=true --set ai_no_progress_sec=180 --set ai_no_progress_require_double_check=true" ;;
    # amended_3 (operator 10/16): judged vs trail_dc, the live exit since 09:32 10/16.
    trail_dc_lob) echo "$OFF --set ai_trail_exit_trend_hold=true --set ai_trail_exit_trend_hold_band_pct=0.05 --set ai_exit_left_overbought=true --set ai_exit_triangle_engine_only=true" ;;
    trail_dc_seat150) echo "$OFF --set ai_trail_exit_trend_hold=true --set ai_trail_exit_trend_hold_band_pct=0.05 --set ai_watch_dead_seat_evict_sec=150 --set ai_watch_unarmable_evict_sec=150" ;;
    *) echo "unknown arm $1" >&2; exit 1 ;;
  esac
}

# Deciding arms across every day first (operator 10/15 evening: ~35 min per replay, so a day-major order left all arms
# on only 4-5 days by 03:30); the rest after.
for a in ${SWEEP_ARMS:-off np180 np_dc trail_dc trail_dc_np_dc dc180 np120_dc np300_dc trend60 trend120}; do
  for d in 2026-10-06 2026-10-07 2026-10-08 2026-10-09 2026-10-12 2026-10-13 2026-10-14 2026-10-15; do
    [ -s "$OUT/${d}_${a}.json" ] && continue
    pgrep -f -- "--out $OUT/${d}_${a}.json" > /dev/null && continue   # already running (relaunch after a reorder)
    HM=$(TZ=America/New_York date +%H%M)
    if [ -n "${SWEEP_ANYTIME:-}" ]; then
      if [ "$HM" -ge 0915 ] && [ "$HM" -lt 1615 ]; then echo "deadline: not launched $d $a" >> "$OUT/failures.txt"; continue; fi
    elif [ "$HM" -ge 0330 ] && [ "$HM" -lt 1615 ]; then echo "deadline: not launched $d $a" >> "$OUT/failures.txt"; continue; fi
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
if [ -n "${SWEEP_ANYTIME:-}" ] && [ "$HM" -lt 0915 ]; then :   # operator 10/16: score before the open
elif [ "$HM" -ge 0400 ] && [ "$HM" -lt 1615 ]; then echo "scoring deferred (quote fetches need the evening)"; exit 0; fi
.venv/bin/python tools/studies/spread_rvol_score.py --dir "$OUT" --arms ${SWEEP_SCORE_ARMS:-off,np180,dc180,np_dc,np120_dc,np300_dc,trend60,trend120,trail_dc,trail_dc_np_dc,trail_dc_lob,trail_dc_seat150} --refs ${SWEEP_REFS:-off,trail_dc} | tee "$OUT/result.md"
