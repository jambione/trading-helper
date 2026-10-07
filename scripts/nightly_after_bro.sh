#!/bin/zsh
# Nightly study chain (10/7): after the Bro + alerts run finishes, run the
# daily records and replays for DAY. After hours only. Logs in /tmp/nightly/.
cd ~/repo/trading-helper || exit 1
d=${1:-$(TZ=America/New_York date +%F)}
mkdir -p /tmp/nightly
until [ -f /tmp/bro_alerts_done ]; do sleep 60; done
.venv/bin/python tools/studies/tight_shadow.py $d > /tmp/nightly/tight_shadow-$d.log 2>&1
.venv/bin/python tools/studies/ob_fills_daily.py $d > /tmp/nightly/ob_fills-$d.log 2>&1
scripts/tight_trail_replay.sh $d > /tmp/nightly/tt_replay-$d.log 2>&1
.venv/bin/python tools/studies/replay_costing.py /tmp/tt_run/$d-*.json > /tmp/nightly/tt_costing-$d.txt 2>&1
echo DONE > /tmp/nightly/done-$d
