#!/usr/bin/env bash
# run_trading_server.sh
# Keeps the Mac awake (AC power) and runs the trading server.
# Started by launchd at 4:00 AM on weekdays.

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
LOG="$REPO/server.log"
PYTHON="$(which python3)"

echo "" >> "$LOG"
echo "======================================" >> "$LOG"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting trading server" >> "$LOG"
echo "======================================" >> "$LOG"

# caffeinate flags:
#   -s  prevent sleep while on AC (mains) power
#   -i  prevent idle sleep
# The server runs as a child of caffeinate — when the server stops,
# caffeinate exits too (and vice versa).
caffeinate -si "$PYTHON" "$REPO/start_all.py" >> "$LOG" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Trading server exited" >> "$LOG"
