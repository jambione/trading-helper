#!/bin/bash
# After 16:15 ET: add prior-session IEX bars to the early_entry_honest cache (connors_green_prereg.json warm-up).
cd "$(dirname "$0")/.." || exit 1
while [ "$(TZ=America/New_York date +%H%M)" -lt 1615 ] && [ "$(TZ=America/New_York date +%H%M)" -ge 0400 ]; do sleep 60; done
.venv/bin/python tools/studies/early_entry_honest.py --fetch-prior
.venv/bin/python tools/studies/early_entry_honest.py --study connors | tail -5
