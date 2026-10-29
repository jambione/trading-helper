#!/bin/zsh
# After-hours queue for 2026-10-29 (operator: Leg-1 delay first; the movement-to-cost hold-out alongside).
# Run ON THE MINI after the close, the snapshot and the stack restart:
#   nohup scripts/night_2026-10-29.sh > logs/night_2026-10-29.log 2>&1 &
# One heavy Alpaca job at a time: waits for scripts/nightly.sh (tight-trail block retired 10/28). Runners stop launching
# at 08:45 ET.
cd ~/repo/trading-helper || exit 1
d=2026-10-29
SHA=$(git rev-parse HEAD)
STOP=08:45
say() { echo "[night] $(TZ=America/New_York date +%H:%M:%S) $*"; }
say "queue start at $SHA"

until [ -f /tmp/nightly/done-$d ] || { [ "$(TZ=America/New_York date +%H%M)" -ge 2000 ] && ! pgrep -f "scripts/nightly.sh" > /dev/null; }; do
  sleep 60
done
while pgrep -f "tools/nightly.py" > /dev/null; do sleep 60; done
say "nightly done"

# 1. Movement-to-cost hold-out (READY a13f7acb): count -> recheck -> score (score refuses unless powered and recheck passed).
.venv/bin/python tools/studies/move_to_cost_holdout.py count   > logs/mtc_count-$d.log 2>&1
.venv/bin/python tools/studies/move_to_cost_holdout.py recheck > logs/mtc_recheck-$d.log 2>&1
.venv/bin/python tools/studies/move_to_cost_holdout.py score   > logs/mtc_score-$d.log 2>&1
say "move_to_cost done"

# 2. square_relax (READY 50f61f3b, screen-first): fidelity -> primary -> controls -> info (each refuses without fidelity).
.venv/bin/python tools/studies/square_relax_run.py fidelity --sha $SHA
.venv/bin/python tools/studies/square_relax_run.py primary  --sha $SHA --jobs 3 --stop-at $STOP
say "square_relax fidelity + primary done"
.venv/bin/python tools/studies/square_relax_run.py controls --sha $SHA --jobs 3 --stop-at $STOP
.venv/bin/python tools/studies/square_relax_run.py info     --sha $SHA --jobs 3 --stop-at $STOP
say "queue done"
