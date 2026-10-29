#!/bin/zsh
# 2026-10-29 rerun after the replay print-age fix (88422acf; amended_harness_fix in both preregs, operator 'Yes, rerun tonight').
# Leg-1 first: square_relax, then stale_age. Each runner refuses without a fidelity pass. Stops launching at 08:45 ET.
cd ~/repo/trading-helper || exit 1
SHA=$(git rev-parse HEAD)
STOP=08:45
say() { echo "[rerun] $(TZ=America/New_York date +%H:%M:%S) $*"; }
say "start at $SHA"
.venv/bin/python tools/studies/square_relax_run.py fidelity --sha $SHA
.venv/bin/python tools/studies/square_relax_run.py primary  --sha $SHA --jobs 3 --stop-at $STOP
say "square_relax fidelity + primary done"
.venv/bin/python tools/studies/stale_age_run.py fidelity --sha $SHA
.venv/bin/python tools/studies/stale_age_run.py primary  --sha $SHA --jobs 3 --stop-at $STOP
say "stale_age fidelity + primary done"
.venv/bin/python tools/studies/square_relax_run.py controls --sha $SHA --jobs 3 --stop-at $STOP
.venv/bin/python tools/studies/stale_age_run.py controls   --sha $SHA --jobs 3 --stop-at $STOP
.venv/bin/python tools/studies/square_relax_run.py info     --sha $SHA --jobs 3 --stop-at $STOP
say "done"
