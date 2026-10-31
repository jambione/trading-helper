#!/bin/zsh
# After-hours queue for 2026-10-30: square_relax under amended_harness_switch (READY 10/29, operator 'Accept READY').
# Run ON THE MINI after the close, the snapshot and the stack restart:
#   nohup scripts/night_2026-10-30.sh > logs/night_2026-10-30.log 2>&1 &
# Waits for scripts/nightly.sh (one heavy Alpaca job at a time). Runners stop launching at 08:45 ET.
# stale_age is NOT run: it still logs held names (needs its own amendment first).
cd ~/repo/trading-helper || exit 1
d=2026-10-30
SHA=$(git rev-parse HEAD)
STOP=08:45
OUT=~/replay_runs/square_relax
say() { echo "[night] $(TZ=America/New_York date +%H:%M:%S) $*"; }
say "queue start at $SHA"

until [ -f /tmp/nightly/done-$d ] || { [ "$(TZ=America/New_York date +%H%M)" -ge 2000 ] && ! pgrep -f "scripts/nightly.sh" > /dev/null; }; do
  sleep 60
done
while pgrep -f "tools/nightly.py" > /dev/null; do sleep 60; done
say "nightly done"

# R2_sha_pin: archive the bc8e1cfc outputs, then re-pin to this queue's sha.
if [ "$(cat $OUT/sha.txt 2>/dev/null)" = "bc8e1cfc7a4ac2ba3467c0fcff8e650e4ba773b6" ]; then  # pragma: allowlist secret (a git commit id)
  mkdir -p $OUT/archive_harness_switch_2026-10-29
  mv $OUT/sha.txt $OUT/fidelity.json $OUT/2026-10-27-FIDELITY.json $OUT/archive_harness_switch_2026-10-29/ 2>/dev/null
  say "archived bc8e1cfc outputs"
fi

# marketable_limit_hist (READY, operator 'I accept'): local logs + Alpaca FILL activities + 1m bars for blocked trades.
.venv/bin/python tools/studies/marketable_limit_hist.py
say "marketable_limit done"

# F6: a fail here is final (NOT TESTABLE on this replay); the later phases refuse without a pass.
.venv/bin/python tools/studies/square_relax_run.py fidelity --sha $SHA
say "fidelity: $(.venv/bin/python -c "import json;print(json.load(open('$OUT/fidelity.json')).get('pass'))" 2>/dev/null)"
# iex_order_spread_calibration stage 0 (READY b1818e60): ~5,500 delayed-SIP reads, ~30 min, before the long replays.
.venv/bin/python tools/studies/iex_order_spread_calibration.py stage0
say "iex stage0 done"
.venv/bin/python tools/studies/square_relax_run.py primary  --sha $SHA --jobs 3 --stop-at $STOP
.venv/bin/python tools/studies/square_relax_run.py controls --sha $SHA --jobs 3 --stop-at $STOP
.venv/bin/python tools/studies/square_relax_run.py info     --sha $SHA --jobs 3 --stop-at $STOP
say "queue done"

# ORB moved to scripts/orb_fetch_chain.sh (operator 10/30 chose buystop_flow first): run it after the buystop_flow
# fetch and count finish (one heavy Alpaca job at a time).
say "done"
