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
DECAY30=(--set ai_local_trail_decay_idle_sec=30)
STEP02=(--set ai_local_trail_decay_step_r=0.02)
R np_lob_slow30 $NP_ON $LOB_ON $DECAY30    # triangle (engine %R) + leash steps after 30 s idle
R np_lob_step02 $NP_ON $LOB_ON $STEP02     # triangle (engine %R) + smaller leash steps (0.02R)
R dump30 $NP_ON $LOB_OFF --set ai_exit_rsi_dump_enabled=true --set ai_exit_rsi_dump_points=30 --set ai_exit_rsi_dump_sec=60 --set ai_exit_rsi_dump_confirm_ticks=2
R brk_replace --set ai_watch_breakout_arm=replace   # entry: clean breakout INSTEAD of the square
R brk_either  --set ai_watch_breakout_arm=either    # entry: square OR clean breakout
R start0945 $NP_ON $LOB_OFF --set ai_entry_earliest_time=09:45   # the open, kept in the replay after the live 10:30 start
R presq_only --set ai_watch_presquare_only=true    # entry: about-to-square arm only (no buying already-overbought)
R ob_broad --set ai_watch_ob_resist_skip=true    # entry rule: no arm in or within 0.3% under resistance
R ob_narrow --set ai_watch_ob_resist_ob_skip=true --set ai_watch_ob_resist_ob_room_pct=0.10   # entry rule, on top of the live config
R pr_engine --set ai_watch_exhaustion_live=false   # %R fix test: engine minute-grid %R only, no live clock-window overwrite (raw clock_range bug, 10/8)
R presq_engine --set ai_watch_presquare_only=true --set ai_watch_exhaustion_live=false   # presquare-only on the engine %R line
# Range entry (docs/studies/range_arm_replay_prereg.json): range rule in, range exit out. Replayed twice for the
# prereg's determinism check; range_arm_control.py check writes the per-input counts (never P&L).
R range_arm --set ai_watch_range_arm=true --set ai_exit_range=true
R range_arm_rerun --set ai_watch_range_arm=true --set ai_exit_range=true
.venv/bin/python tools/studies/range_arm_control.py check $d --dir /tmp/tt_run > /tmp/tt_run/$d-range_arm_check.log 2>&1
echo DONE $d > /tmp/tt_run/done-$d
