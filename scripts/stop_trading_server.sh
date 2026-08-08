#!/usr/bin/env bash
# stop_trading_server.sh
# Cleanly shuts down the trading server.
# Called by launchd at 11:00 AM on weekdays.

# Resolve the repo from this script's own location — a hardcoded home directory
# silently rotted here once already (the log went to a path that didn't exist).
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$REPO/server.log"

# Guard: only run if it's actually between 10:30–11:30 AM.
# launchd replays "missed" jobs when the Mac wakes up, which would kill
# the server seconds after startup. This check prevents that.
HOUR=$(date +%H)
MIN=$(date +%M)
TOTAL_MIN=$((HOUR * 60 + MIN))
if [ "$TOTAL_MIN" -lt 630 ] || [ "$TOTAL_MIN" -gt 690 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stop job fired outside 10:30-11:30 AM window (${HOUR}:${MIN}) — skipping." >> "$LOG"
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stopping trading server (scheduled 11 AM shutdown)" >> "$LOG"

# Kill the processes by name — order matters: kill the parent first
# so it doesn't restart children, then clean up children.
pkill -SIGTERM -f "start_all.py"
sleep 3

# Force-kill anything still running
pkill -SIGKILL -f "start_all.py"
pkill -SIGKILL -f "dashboard.py"
pkill -SIGKILL -f "signal_engine.py"
pkill -SIGKILL -f "discord_source.py"
pkill -SIGKILL -f "caffeinate.*start_all"
pkill -SIGKILL -f "cloudflared"

# Kill Discord
osascript -e 'tell application "Discord" to quit' 2>/dev/null
sleep 2
pkill -x Discord 2>/dev/null || true

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Trading server stopped" >> "$LOG"
