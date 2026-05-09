#!/usr/bin/env bash
# stop_trading_server.sh
# Cleanly shuts down the trading server.
# Called by launchd at 11:00 AM on weekdays.

LOG="/Users/jonathanbrasfield/repo/trading-helper/trading-helper/server.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stopping trading server (scheduled 11 AM shutdown)" >> "$LOG"

# Kill the processes by name — order matters: kill the parent first
# so it doesn't restart children, then clean up children.
pkill -SIGTERM -f "start_all.py"
sleep 3

# Force-kill anything still running
pkill -SIGKILL -f "start_all.py"
pkill -SIGKILL -f "dashboard.py"
pkill -SIGKILL -f "caffeinate.*start_all"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Trading server stopped" >> "$LOG"
