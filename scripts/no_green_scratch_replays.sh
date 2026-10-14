#!/bin/bash
# Evening replay batch for three preregs (mini only):
#   docs/studies/no_green_scratch_prereg.json   np180 (control) / np90 / np60
#   docs/studies/no_momentum_seats_prereg.json  nomom
#   docs/studies/keep_falling_seats_prereg.json keepfall vs np180rt (both --engine rt)
# Pins the WHOLE live config of $CFG_SHA via --set-json (not only the keys that
# changed), waits for 16:15 ET, keeps <= 3 replay_session processes running in
# total (the shared 200 req/min Alpaca budget), then scores.
#   nohup bash scripts/no_green_scratch_replays.sh > /tmp/ngs_run.log 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$PWD
CFG_SHA=73217e4
SHA=$(git rev-parse --short HEAD)              # must contain replay --set-json
OUT=/tmp/ngs_$(date +%Y%m%d_%H%M)               # fresh dir: no stale outputs reused
mkdir -p "$OUT"
MAXP=3

git show "$CFG_SHA:config/bot_config.json" > "$OUT/base_config.json"
N=$(.venv/bin/python -c "import json,sys; d=json.load(open(sys.argv[1])); print(len(d))" "$OUT/base_config.json")
if [ "$N" -lt 300 ]; then echo "base config has only $N keys - refusing to run unpinned"; exit 1; fi
# The replay runs `git archive HEAD`, so check HEAD itself, not the working tree.
git diff --quiet HEAD -- || { echo "tracked files differ from HEAD - commit first"; exit 1; }
git show HEAD:tools/replay_session.py | grep -c "def work_counts" > /dev/null || { echo "HEAD replay lacks --set-json/work_counts"; exit 1; }
git show HEAD:ai_entry_watch.py | grep -c "ai_watch_dead_evict_exh_falling" > /dev/null || { echo "HEAD lacks the keepfall switch"; exit 1; }
echo "sha $SHA cfg $CFG_SHA keys $N out $OUT"

while [ "$(TZ=America/New_York date +%H%M)" -lt 1615 ]; do sleep 120; done

arm_sets() {
  case $1 in
    np180)    echo "--set ai_no_progress_sec=180" ;;
    np90)     echo "--set ai_no_progress_sec=90" ;;
    np60)     echo "--set ai_no_progress_sec=60" ;;
    nomom)    echo "--set ai_no_progress_sec=180 --set ai_watch_seed_momentum=false --set ai_watch_seed_momentum_open=false --set ai_watch_soft_seed_momentum=false" ;;
    np180rt)  echo "--engine rt --set ai_no_progress_sec=180" ;;
    keepfall) echo "--engine rt --set ai_no_progress_sec=180 --set ai_watch_dead_evict_exh_falling=false" ;;
    *) echo "unknown arm $1" >&2; exit 1 ;;
  esac
}

for d in 2026-09-30 2026-10-01 2026-10-02 2026-10-05 2026-10-06 2026-10-07 2026-10-08 2026-10-09 2026-10-12 2026-10-13; do
  for a in np180 np90 np60 nomom np180rt keepfall; do
    # Deadline: never launch into the next premarket (shared Alpaca budget from 04:00 ET).
    HM=$(TZ=America/New_York date +%H%M)
    if [ "$HM" -ge 0330 ] && [ "$HM" -lt 1615 ]; then
      echo "deadline: not launched $d $a" >> "$OUT/failures.txt"; continue
    fi
    # Count every replay on the box, not only ours (nightly runs share the budget).
    while [ "$(pgrep -f 'tools/replay_session.py --day' | wc -l)" -ge $((MAXP * 2)) ]; do sleep 20; done
    HM=$(TZ=America/New_York date +%H%M)
    if [ "$HM" -ge 0330 ] && [ "$HM" -lt 1615 ]; then
      echo "deadline: not launched $d $a" >> "$OUT/failures.txt"; continue
    fi
    # shellcheck disable=SC2046
    ( .venv/bin/python tools/replay_session.py --day "$d" --sha "$SHA" --set-json "$OUT/base_config.json" \
        $(arm_sets "$a") --out "$OUT/${d}_${a}.json" > "$OUT/${d}_${a}.log" 2>&1 \
      || echo "FAILED $d $a" >> "$OUT/failures.txt" ) &
    sleep 5
  done
done
wait
.venv/bin/python tools/studies/no_green_scratch_score.py --dir "$OUT" --sha "$SHA" | tee "$OUT/result.md"
