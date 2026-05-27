#!/usr/bin/env bash
# stop_now.sh — immediately kill all trading processes (no time-window guard)

LOG="/Users/jonathanbrasfield/repo/trading-helper/trading-helper/server.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Manual stop triggered" >> "$LOG"

# Graceful shutdown first
pkill -SIGTERM -f "start_all.py"      2>/dev/null
pkill -SIGTERM -f "signal_engine.py"  2>/dev/null
sleep 2

# Force-kill anything still running
pkill -SIGKILL -f "start_all.py"      2>/dev/null
pkill -SIGKILL -f "dashboard.py"      2>/dev/null
pkill -SIGKILL -f "signal_engine.py"  2>/dev/null
pkill -SIGKILL -f "transcribe_action.py" 2>/dev/null
pkill -SIGKILL -f "caffeinate.*start_all" 2>/dev/null
pkill -f "cloudflared"                2>/dev/null

echo "[$(date '+%Y-%m-%d %H:%M:%S')] All trading processes stopped" >> "$LOG"
echo "✅ All trading processes stopped."
