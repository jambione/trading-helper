#!/usr/bin/env bash
# run_trading_server.sh
# Keeps the Mac awake (AC power), runs the trading server, and opens
# a Cloudflare Quick Tunnel so the dashboard is publicly reachable.
# Started by launchd at 4:00 AM on weekdays.

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
LOG="$REPO/server.log"
TUNNEL_LOG="$REPO/tunnel.log"
CF="$(which cloudflared)"

# ── Find a Python that has all required packages installed ────────────────────
# Homebrew updates can silently change which python3 is on PATH, losing packages.
# Try known-good locations first (Python 3.9 had everything), then fall back.
PYTHON=""
for candidate in \
    /usr/local/bin/python3.9 \
    /opt/homebrew/bin/python3.9 \
    /usr/local/bin/python3 \
    /opt/homebrew/bin/python3 \
    "$(which python3 2>/dev/null)"; do
    if [ -x "$candidate" ] && "$candidate" -c "import uvicorn, pandas, fastapi" 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    # Nothing has all packages — pick the best available Python and install everything
    for candidate in \
        /usr/local/bin/python3.9 \
        /opt/homebrew/bin/python3.9 \
        /usr/local/bin/python3 \
        /opt/homebrew/bin/python3 \
        "$(which python3 2>/dev/null)"; do
        if [ -x "$candidate" ]; then
            PYTHON="$candidate"
            break
        fi
    done
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Installing requirements into $PYTHON ..." >> "$LOG"
    "$PYTHON" -m pip install --quiet -r "$REPO/requirements.txt" --break-system-packages >> "$LOG" 2>&1 \
        || "$PYTHON" -m pip install --quiet -r "$REPO/requirements.txt" --user >> "$LOG" 2>&1
fi
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Using Python: $PYTHON ($("$PYTHON" --version 2>&1))" >> "$LOG"

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
