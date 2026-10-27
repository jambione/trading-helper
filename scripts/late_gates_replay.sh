#!/bin/zsh
# docs/studies/late_gates_replay_prereg.json runner. Run ON THE MINI after the close, never while scripts/nightly.sh runs.
# usage: scripts/late_gates_replay.sh fidelity|primary|info [--jobs 3] [--stop-at 08:45]
cd ~/repo/trading-helper || exit 1
exec .venv/bin/python tools/studies/late_gates_run.py "$@"
