#!/bin/zsh
# After-hours queue for 2026-10-28 (operator 10/28: bring the slope-now exploration forward; stale_age's controls last).
# Run ON THE MINI after the close, the 16:05 snapshot and the stack restart:
#   nohup scripts/night_2026-10-28.sh > logs/night_2026-10-28.log 2>&1 &
# One heavy Alpaca job at a time: the replays wait for scripts/nightly.sh (its tight-trail costing ran 17:09-01:05
# on 10/27 while late_gates replays ran beside it). Every runner stops launching at 08:45 ET.
cd ~/repo/trading-helper || exit 1
d=2026-10-28
SHA=$(git rev-parse HEAD)
STOP=08:45
say() { echo "[night] $(TZ=America/New_York date +%H:%M:%S) $*"; }
say "queue start at $SHA"

# 1. Historical slope/sideways exploration: reads the local bar warehouse only (no Alpaca), so it starts at once.
.venv/bin/python tools/studies/slope_now_hist.py --workers 2 > logs/slope_now_hist-$d.log 2>&1 &
say "slope_now_hist started (pid $!)"

# 2. Wait for the nightly job to finish (done marker), or for 21:00 with no nightly.sh running.
until [ -f /tmp/nightly/done-$d ] || { [ "$(TZ=America/New_York date +%H%M)" -ge 2100 ] && ! pgrep -f "scripts/nightly.sh" > /dev/null; }; do
  sleep 60
done
while pgrep -f "tools/nightly.py" > /dev/null; do sleep 60; done
say "nightly done; replays start"

# 3. late_gates info phase (its own pinned sha), finishing the 5m-veto / wr_trend study.
scripts/late_gates_replay.sh info --sha 21e6f763 --jobs 3 --stop-at $STOP
say "late_gates info done"

# 4. stale_age: fidelity, then the stage-1 primary runs (the runner refuses without a fidelity pass).
.venv/bin/python tools/studies/stale_age_run.py fidelity --sha $SHA
.venv/bin/python tools/studies/stale_age_run.py primary --sha $SHA --jobs 3 --stop-at $STOP
say "stale_age fidelity + primary done"

# 5. slope_now: fidelity, then REF with arm rows (the replay half of the exploration), then the exploration table.
.venv/bin/python tools/studies/slope_now_run.py fidelity --sha $SHA
.venv/bin/python tools/studies/slope_now_run.py ref --sha $SHA --jobs 3 --stop-at $STOP
.venv/bin/python tools/studies/slope_now_explore.py > logs/slope_now_explore-$d.log 2>&1
say "slope_now fidelity + ref + explore done"

# 6. stale_age random controls (the slowest part) for the rest of the night.
.venv/bin/python tools/studies/stale_age_run.py controls --sha $SHA --jobs 3 --stop-at $STOP
say "queue done"
