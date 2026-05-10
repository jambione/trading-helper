#!/usr/bin/env bash
# run_trading_server.sh
# Keeps the Mac awake (AC power), runs the trading server, and opens
# a Cloudflare Quick Tunnel so the dashboard is publicly reachable.
# Started by launchd at 4:00 AM on weekdays.

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
LOG="$REPO/server.log"
TUNNEL_LOG="$REPO/tunnel.log"
CF="$(which cloudflared)"

# ── Find a Python that has uvicorn installed ──────────────────────────────────
# Homebrew updates can silently change which python3 is on PATH, losing packages.
# Try common locations in order; fall back to whichever python3 is on PATH.
PYTHON=""
for candidate in \
    /usr/local/bin/python3 \
    /opt/homebrew/bin/python3 \
    "$(which python3 2>/dev/null)"; do
    if [ -x "$candidate" ] && "$candidate" -c "import uvicorn" 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    # No Python with uvicorn found — install it into whichever python3 is on PATH
    PYTHON="$(which python3)"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] uvicorn missing — installing now..." >> "$LOG"
    "$PYTHON" -m pip install --quiet uvicorn fastapi --break-system-packages >> "$LOG" 2>&1 \
        || "$PYTHON" -m pip install --quiet uvicorn fastapi --user >> "$LOG" 2>&1
fi
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Using Python: $PYTHON" >> "$LOG"

echo "" >> "$LOG"
echo "======================================" >> "$LOG"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting trading server" >> "$LOG"
echo "======================================" >> "$LOG"

# ── Start Cloudflare named tunnel in background ───────────────────────────────
# Named tunnel gives a permanent URL: https://trading.jbrasfield.com
echo "" > "$TUNNEL_LOG"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Cloudflare tunnel (trading.jbrasfield.com)..." >> "$TUNNEL_LOG"
"$CF" tunnel --config ~/.cloudflared/config.yml run trading-helper >> "$TUNNEL_LOG" 2>&1 &
CF_PID=$!

sleep 5
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Public URL: https://trading.jbrasfield.com" >> "$LOG"

# ── Start server (caffeinate keeps Mac awake while on AC power) ───────────────
caffeinate -si "$PYTHON" "$REPO/start_all.py" >> "$LOG" 2>&1

# ── Server exited — clean up tunnel ──────────────────────────────────────────
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Trading server exited — stopping tunnel" >> "$LOG"
kill "$CF_PID" 2>/dev/null
wait "$CF_PID" 2>/dev/null
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done." >> "$LOG"
