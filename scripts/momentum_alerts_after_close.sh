#!/bin/bash
# After 16:15 ET: SIP-bar markouts for every recorded momentum alert (information only).
cd "$(dirname "$0")/.." || exit 1
while [ "$(TZ=America/New_York date +%H%M)" -lt 1615 ] && [ "$(TZ=America/New_York date +%H%M)" -ge 0400 ]; do sleep 60; done
.venv/bin/python tools/studies/momentum_alert_markouts.py > /tmp/momentum_alerts.log 2>&1
