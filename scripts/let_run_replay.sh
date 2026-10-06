#!/bin/zsh
# Replays for docs/studies/let_run_replay_prereg.json. Run ON THE MINI after the close.
cd ~/repo/trading-helper || exit 1
mkdir -p /tmp/rp_run
for d in 2026-09-29 2026-09-30 2026-10-01 2026-10-02 2026-10-05 2026-10-06; do
  .venv/bin/python tools/replay_session.py --day $d --out /tmp/rp_run/$d-base.json > /tmp/rp_run/$d-base.log 2>&1
  .venv/bin/python tools/replay_session.py --day $d --set ai_local_trail_time_decay_enabled=false --out /tmp/rp_run/$d-decay_off.json > /tmp/rp_run/$d-decay_off.log 2>&1
  .venv/bin/python tools/replay_session.py --day $d --set ai_local_trail_decay_idle_sec=30 --out /tmp/rp_run/$d-decay_slow.json > /tmp/rp_run/$d-decay_slow.log 2>&1
  .venv/bin/python tools/replay_session.py --day $d --set ai_local_trail_give_r=0.25 --set ai_local_trail_peak_give_pct=0.7 --out /tmp/rp_run/$d-give_wide.json > /tmp/rp_run/$d-give_wide.log 2>&1
done
.venv/bin/python tools/studies/replay_costing.py /tmp/rp_run/*.json > /tmp/rp_run/costing.txt 2>&1
echo DONE > /tmp/rp_run/done
