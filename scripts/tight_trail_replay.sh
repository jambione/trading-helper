#!/bin/zsh
# Replays for docs/studies/tight_trail_replay_prereg.json. Run ON THE MINI after the close.
# usage: scripts/tight_trail_replay.sh DAY   (default: today ET)
# From 2026-10-08 the live config has np180 + lob ON (operator), so every variant sets BOTH switches
# explicitly; 'off' is the pre-10/8 live exit and the reference for the original variants.
cd ~/repo/trading-helper || exit 1
d=${1:-$(TZ=America/New_York date +%F)}
mkdir -p /tmp/tt_run
R() { local name=$1; shift; .venv/bin/python tools/replay_session.py --day $d "$@" --out /tmp/tt_run/$d-$name.json > /tmp/tt_run/$d-$name.log 2>&1; }
NP_OFF=(--set ai_no_progress_flatten_enabled=false)
NP_ON=(--set ai_no_progress_flatten_enabled=true --set ai_no_progress_sec=180 --set ai_no_progress_mfe_r=0.001)
LOB_OFF=(--set ai_exit_left_overbought=false)
LOB_ON=(--set ai_exit_left_overbought=true)
R base
R off       $NP_OFF $LOB_OFF
R arm_pct   $NP_OFF $LOB_OFF --set ai_local_trail_arm_pct=0.10
R stop_1pct $NP_OFF $LOB_OFF --set ai_watch_synth_stop_pct=1.0 --set ai_watch_min_stop_pct=0.5
R lob       $NP_OFF $LOB_ON
R np180     $NP_ON  $LOB_OFF
R np_lob    $NP_ON  $LOB_ON
echo DONE $d > /tmp/tt_run/done-$d
