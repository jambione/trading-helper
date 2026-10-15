#!/bin/bash
# After 16:15 ET: SIP-bar markouts for every recorded momentum alert (information only).
cd "$(dirname "$0")/.." || exit 1
while [ "$(TZ=America/New_York date +%H%M)" -lt 1615 ] && [ "$(TZ=America/New_York date +%H%M)" -ge 0400 ]; do sleep 60; done
.venv/bin/python tools/studies/momentum_alert_markouts.py > /tmp/momentum_alerts.log 2>&1
# Then the 30-180 s tick scalps on SIP trades + quotes (entry at the ask 5 s after the alert, exit at the bid).
.venv/bin/python tools/studies/momentum_alert_ticks.py > /tmp/momentum_alert_ticks.log 2>&1
# Then the clock-window entries (docs/studies/clock_windows_prereg.json), on the same cached SIP bars.
.venv/bin/python tools/studies/clock_windows.py > /tmp/clock_windows.log 2>&1
