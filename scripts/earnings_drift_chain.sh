#!/bin/zsh
# Earnings drift (docs/studies/earnings_drift_prereg.json) on the mini, after hours only.
# Waits for the let-it-run replay to finish (shared Alpaca budget), then: symbols -> news (resumable, stops at 09:00 ET)
# -> events -> score. Rerun this script on later evenings until /tmp/ed_chain/done exists; finished phases are skipped.
cd ~/repo/trading-helper || exit 1
mkdir -p /tmp/ed_chain
until [ -f /tmp/rp_run/done ] || ! pgrep -f let_run_replay.sh >/dev/null; do sleep 60; done
W=ai_reports/earnings_drift
[ -f $W/symbols.json ] || .venv/bin/python tools/studies/earnings_drift.py symbols >> /tmp/ed_chain/symbols.log 2>&1 || exit 1
.venv/bin/python tools/studies/earnings_drift.py news >> /tmp/ed_chain/news.log 2>&1 || exit 0     # guard stop at 09:00 -> resume next evening
.venv/bin/python tools/studies/earnings_drift.py events >> /tmp/ed_chain/events.log 2>&1 || exit 1
.venv/bin/python tools/studies/earnings_drift.py score >> /tmp/ed_chain/score.log 2>&1 || exit 0
[ -f $W/report.md ] && echo DONE > /tmp/ed_chain/done
