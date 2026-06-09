#!/usr/bin/env bash
# evening_stop.sh
# Clean shutdown of the trading session:
#   1. Quit Discord (closes the alert window the OCR source reads)
#   2. Kill the trading server (dashboard + engine + Discord OCR source + tunnel)

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
BACKEND="http://localhost:8888"

echo ""
echo "========================================"
echo "  Brasfield Trading — Stopping Session"
echo "========================================"
echo ""

# ── 1. Quit Discord ───────────────────────────────────────────────────────────
echo "[1/2] Quitting Discord..."
osascript -e 'tell application "Discord" to quit' 2>/dev/null || true
sleep 2
pkill -x Discord 2>/dev/null || true
echo "      ✓ Discord closed."

# ── 2. Kill the trading server ────────────────────────────────────────────────
echo "[2/2] Shutting down trading server..."
pkill -SIGTERM -f "start_all.py" 2>/dev/null; sleep 2
pkill -SIGKILL -f "start_all.py"       2>/dev/null
pkill -SIGKILL -f "discord_source.py"  2>/dev/null
pkill -SIGKILL -f "signal_engine.py"   2>/dev/null
pkill -SIGKILL -f "dashboard.py"       2>/dev/null
pkill -SIGKILL -f "caffeinate.*start_all" 2>/dev/null
pkill -SIGKILL -f "cloudflared" 2>/dev/null
echo "      ✓ Server stopped."

echo ""
echo "========================================"
echo "  Session ended. Ready for next morning."
echo "========================================"
echo ""
