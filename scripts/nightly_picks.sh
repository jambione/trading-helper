#!/bin/zsh
# Nightly job. 11/11 wound-down version kept only the pick steps; 11/17 the day desk was restored, so the desk nightly (scripts/nightly.sh) runs first, then the pick steps.
# Run by com.jambi.nightly-studies at 16:40 ET on weekdays; safe to run by hand: scripts/nightly_picks.sh [DAY]
# Keeps only what the longer-hold pick test needs:
#   1. append today's bars to the study warehouses (daily data for longer-horizon tests)
#   2. record the week's picks if Monday's run was missed, and score the pickers' ledgers
#      -> ~/pick_ledgers/REPORT.txt (headline picker) and ~/pick_ledgers/FACTS_REPORT.txt (fact-sheet picker)
#   3. score the operator's own ledger (docs/pick_ledger.jsonl) if it has picks
# The old desk studies are in scripts/nightly.sh (no longer scheduled).
cd ~/repo/trading-helper || exit 1
d=${1:-$(TZ=America/New_York date +%F)}
mkdir -p /tmp/nightly
until [ "$(TZ=America/New_York date +%H%M)" -ge 1635 ] || [ "$d" != "$(TZ=America/New_York date +%F)" ]; do sleep 60; done
# Day desk restored 11/17 (operator): the desk's own nightly (forward studies, day review, scoreboard, broker truth report AND the warehouse append) runs first.
scripts/nightly.sh "$d" > /tmp/nightly/nightly-desk-$d.log 2>&1
# Catch-up: if Monday's run was missed (machine off, agent did not answer), record this week's picks tonight.
# Both commands do nothing when the week is already recorded; the entry is then the next open. Each ends by writing its report.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
.venv/bin/python -u tools/auto_picker.py run > /tmp/nightly/auto_picker-$d.log 2>&1
.venv/bin/python -u tools/fact_picker.py run > /tmp/nightly/fact_picker-$d.log 2>&1
.venv/bin/python tools/pick_positions.py status > /tmp/nightly/pick_positions-$d.log 2>&1   # paper holdings vs the S&P -> ~/pick_ledgers/POSITIONS.txt
.venv/bin/python tools/odd_lot_watch.py > /tmp/nightly/odd_lots-$d.log 2>&1   # listed odd-lot tender offers -> ~/pick_ledgers/ODD_LOTS.txt (11/15)
.venv/bin/python tools/pick_email.py > /tmp/nightly/pick_email-$d.log 2>&1   # the daily email of holdings and picks to the operator (11/14)
if [ -s docs/pick_ledger.jsonl ]; then
  .venv/bin/python tools/pick_ledger.py score > /tmp/nightly/pick_ledger-$d.log 2>&1
fi
echo "$(date '+%F %T') nightly_picks done for $d" >> /tmp/nightly/nightly_picks.log
