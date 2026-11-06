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
.venv/bin/python tools/studies/early_entry_honest.py --fetch --days $d > /tmp/nightly/eeh_fetch-$d.log 2>&1   # bar cache for wait_held_prereg.json (forward from 10/15)
.venv/bin/python tools/studies/wait_held_study.py --score >> /tmp/nightly/eeh_fetch-$d.log 2>&1   # prints NOT DUE until 10 forward sessions
.venv/bin/python tools/studies/day_hold.py forward > /tmp/nightly/day_hold-$d.log 2>&1   # day_hold_prereg.json forward run (read at 40 sessions)
# RETIRED 2026-10-29 (operator 10/28 'Yes, retire them'): the tight-trail replay block (~24 variants, ~6 h + ~1 h costing a
# night) produced no decision. tight_trail_replay_prereg was frozen 10/9; the wr_trend forward read is void (superseded by
# late_gates); the SuperTrend / leash forward reads lost every session before 10/23 to a /tmp wipe and test the np180 + lob
# exits that are no longer live. Restart an exit test, if wanted, against the live exits with outputs outside /tmp.
# scripts/tight_trail_replay.sh $d > /tmp/nightly/tt_replay-$d.log 2>&1
# .venv/bin/python tools/studies/replay_costing.py /tmp/tt_run/$d-*.json > /tmp/nightly/tt_costing-$d.txt 2>&1
.venv/bin/python tools/studies/day_review.py $d > /tmp/nightly/day_review-$d.log 2>&1
scripts/regression_check.sh > /tmp/nightly/regression-$d.log 2>&1   # new failing tests vs tests/known_failures.txt (operator 11/05)
.venv/bin/python tools/hold_scorecard.py > /tmp/nightly/hold_scorecard-$d.log 2>&1   # did holding an at-entry trail sale pay? (operator 11/05)
.venv/bin/python tools/entry_cost_daily.py > /tmp/nightly/entry_cost-$d.log 2>&1   # fill vs SIP mid per fill, never-green share (operator 11/06)
.venv/bin/python tools/studies/range_position_forward.py > /tmp/nightly/range_fwd-$d.log 2>&1   # counts only until 150 entries (prereg)
.venv/bin/python tools/studies/wr_slope_forward.py > /tmp/nightly/slope_fwd-$d.log 2>&1   # counts only until due (prereg)
.venv/bin/python tools/studies/run_before_entry_forward.py > /tmp/nightly/run_fwd-$d.log 2>&1   # operator's apex hypothesis, counts only until 150 (prereg)
.venv/bin/python tools/studies/run_efficiency_forward.py > /tmp/nightly/eff_fwd-$d.log 2>&1   # run-efficiency hypothesis, counts only until 150 (prereg)
.venv/bin/python tools/premarket_report.py > /tmp/nightly/premarket-$d.log 2>&1   # docs/PREMARKET.md, funnel + frozen Hybrid C verdict (operator 11/06)
.venv/bin/python tools/daily_scoreboard.py > /tmp/nightly/scoreboard-$d.log 2>&1   # docs/SCOREBOARD.md, rolling 20 sessions (operator 11/05)
echo DONE > /tmp/nightly/done-$d
