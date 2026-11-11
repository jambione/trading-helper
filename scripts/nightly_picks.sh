#!/bin/zsh
# Nightly job after the day desk was wound down (operator 2026-11-11: "calling it quits" on day trading).
# Run by com.jambi.nightly-studies at 16:40 ET on weekdays; safe to run by hand: scripts/nightly_picks.sh [DAY]
# Keeps only what the longer-hold pick test needs:
#   1. append today's bars to the study warehouses (daily data for longer-horizon tests)
#   2. score the automatic pickers' ledgers -> ~/pick_ledgers/REPORT.txt
#   3. score the operator's own ledger (docs/pick_ledger.jsonl) if it has picks
# The old desk studies are in scripts/nightly.sh (no longer scheduled).
cd ~/repo/trading-helper || exit 1
d=${1:-$(TZ=America/New_York date +%F)}
mkdir -p /tmp/nightly
until [ "$(TZ=America/New_York date +%H%M)" -ge 1635 ] || [ "$d" != "$(TZ=America/New_York date +%F)" ]; do sleep 60; done
nice -n 19 .venv/bin/python tools/studies/warehouse_nightly.py > /tmp/nightly/warehouse-$d.log 2>&1
.venv/bin/python tools/auto_picker.py report > /tmp/nightly/auto_picker-$d.log 2>&1
if [ -s docs/pick_ledger.jsonl ]; then
  .venv/bin/python tools/pick_ledger.py score > /tmp/nightly/pick_ledger-$d.log 2>&1
fi
echo "$(date '+%F %T') nightly_picks done for $d" >> /tmp/nightly/nightly_picks.log
