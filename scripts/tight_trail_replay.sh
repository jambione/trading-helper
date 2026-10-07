#!/bin/zsh
# Replays for docs/studies/tight_trail_replay_prereg.json. Run ON THE MINI after the close.
# usage: scripts/tight_trail_replay.sh DAY   (default: today ET)
cd ~/repo/trading-helper || exit 1
d=${1:-$(TZ=America/New_York date +%F)}
mkdir -p /tmp/tt_run
.venv/bin/python tools/replay_session.py --day $d --out /tmp/tt_run/$d-base.json > /tmp/tt_run/$d-base.log 2>&1
.venv/bin/python tools/replay_session.py --day $d --set ai_local_trail_arm_pct=0.10 --out /tmp/tt_run/$d-arm_pct.json > /tmp/tt_run/$d-arm_pct.log 2>&1
.venv/bin/python tools/replay_session.py --day $d --set ai_watch_synth_stop_pct=1.0 --out /tmp/tt_run/$d-stop_1pct.json > /tmp/tt_run/$d-stop_1pct.log 2>&1
echo DONE $d > /tmp/tt_run/done-$d
