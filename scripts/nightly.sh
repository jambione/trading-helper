#!/bin/zsh
# Nightly studies + the end-of-day review (operator 10/7). Run by
# com.jambi.nightly-studies at 16:40 ET on weekdays; safe to run by hand:
#   scripts/nightly.sh [DAY]
# After hours only (the 09:00-16:30 fetch guard): it waits until 16:35 ET.
# Logs: /tmp/nightly/*-DAY.log; review: ai_reports/day_review/DAY.md
cd ~/repo/trading-helper || exit 1
d=${1:-$(TZ=America/New_York date +%F)}
mkdir -p /tmp/nightly
until [ "$(TZ=America/New_York date +%H%M)" -ge 1635 ] || [ "$d" != "$(TZ=America/New_York date +%F)" ]; do sleep 60; done
# tools/nightly.py (started at 16:05 by session_snapshot) runs one replay at a time; do not overlap it.
while pgrep -f "tools/nightly.py" > /dev/null; do sleep 60; done
# ... nor a one-off study run (e.g. scripts/bro_alerts_run.sh): one heavy Alpaca job at a time.
while pgrep -f "scripts/bro_alerts_run.sh" > /dev/null; do sleep 60; done
if [[ "$d" > "2026-10-07" ]]; then
  .venv/bin/python tools/studies/bro_sr_wr.py forward $d > /tmp/nightly/bro_fwd-$d.log 2>&1
  .venv/bin/python tools/studies/bro_sr_wr.py forward $d --source alerts > /tmp/nightly/alerts_fwd-$d.log 2>&1
fi
if [[ "$d" > "2026-10-07" ]]; then
  .venv/bin/python tools/studies/sr_breakout_book.py score $d > /tmp/nightly/sr_breakout_book-$d.log 2>&1
  .venv/bin/python tools/studies/sr_breakout_book.py power >> /tmp/nightly/sr_breakout_book-$d.log 2>&1
  .venv/bin/python tools/studies/sr_breakout_book.py read-due >> /tmp/nightly/sr_breakout_book-$d.log 2>&1
fi
.venv/bin/python tools/studies/tight_shadow.py $d > /tmp/nightly/tight_shadow-$d.log 2>&1
.venv/bin/python tools/studies/ob_fills_daily.py $d > /tmp/nightly/ob_fills-$d.log 2>&1
scripts/tight_trail_replay.sh $d > /tmp/nightly/tt_replay-$d.log 2>&1
.venv/bin/python tools/studies/replay_costing.py /tmp/tt_run/$d-*.json > /tmp/nightly/tt_costing-$d.txt 2>&1
.venv/bin/python tools/studies/day_review.py $d > /tmp/nightly/day_review-$d.log 2>&1
echo DONE > /tmp/nightly/done-$d
