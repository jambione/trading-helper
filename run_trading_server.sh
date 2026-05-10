#!/usr/bin/env bash
# run_trading_server.sh
# Keeps the Mac awake (AC power), runs the trading server, and opens
# a Cloudflare Quick Tunnel so the dashboard is publicly reachable.
# Started by launchd at 4:00 AM on weekdays.

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
LOG="$REPO/server.log"
TUNNEL_LOG="$REPO/tunnel.log"
CF="$(which cloudflared)"

# ── Find Python — prefer project venv (Python 3.12 + mlx_whisper) ─────────────
VENV="$REPO/venv"

if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import uvicorn, pandas" 2>/dev/null; then
    # Project venv exists and has the required packages — use it
    PYTHON="$VENV/bin/python"
else
    # Venv missing or incomplete — try to create/repair it with Python 3.12
    PY312=""
    for candidate in /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
        if [ -x "$candidate" ]; then PY312="$candidate"; break; fi
    done

    if [ -n "$PY312" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Building venv with $PY312 ..." >> "$LOG"
        "$PY312" -m venv "$VENV" >> "$LOG" 2>&1
        "$VENV/bin/pip" install --quiet -r "$REPO/requirements.txt" >> "$LOG" 2>&1
        "$VENV/bin/pip" install --quiet mlx-whisper >> "$LOG" 2>&1
        PYTHON="$VENV/bin/python"
    else
        # No Python 3.12 — fall back to whatever is on PATH
        PYTHON="$(which python3)"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: Python 3.12 not found. MLX Whisper unavailable." >> "$LOG"
    fi
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
