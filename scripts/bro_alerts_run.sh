#!/bin/zsh
# Trader Bro + alerts historical runs (preregs bro_sr_wr_prereg.json / alerts_sr_wr_prereg.json). After hours only.
cd ~/repo/trading-helper || exit 1
until [ "$(TZ=America/New_York date +%H%M)" -ge 1635 ]; do sleep 120; done
.venv/bin/python tools/studies/bro_sr_wr.py all > /tmp/bro_run.log 2>&1
.venv/bin/python tools/studies/bro_sr_wr.py all --source alerts > /tmp/alerts_run.log 2>&1
echo DONE > /tmp/bro_alerts_done
