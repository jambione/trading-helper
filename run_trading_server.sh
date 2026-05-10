#!/usr/bin/env bash
# run_trading_server.sh
# Keeps the Mac awake (AC power), runs the trading server, and opens
# a Cloudflare Quick Tunnel so the dashboard is publicly reachable.
# Started by launchd at 4:00 AM on weekdays.

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
LOG="$REPO/server.log"
TUNNEL_LOG="$REPO/tunnel.log"
CF="$(which cloudflared)"
VENV="$REPO/venv"

# ── Find Python 3.12 ──────────────────────────────────────────────────────────
PY312=""
for candidate in /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
    if [ -x "$candidate" ]; then PY312="$candidate"; break; fi
done

# ── Ensure venv exists and has all packages ───────────────────────────────────
# Always use venv/bin/python3.12 (not the generic 'python' symlink) to avoid
# version confusion when the venv directory already exists from a prior run.

if [ -n "$PY312" ]; then
    # Check if venv is healthy: correct Python version + required packages
    VENV_OK=false
    if [ -x "$VENV/bin/python3.12" ] && "$VENV/bin/python3.12" -c "import uvicorn, pandas, fastapi" 2>/dev/null; then
        VENV_OK=true
    fi

    if [ "$VENV_OK" = false ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Creating/repairing venv with $PY312 ..." >> "$LOG"
        # --clear guarantees a clean slate even if the directory already exists
        "$PY312" -m venv --clear "$VENV" >> "$LOG" 2>&1
        "$VENV/bin/pip" install --quiet --upgrade pip >> "$LOG" 2>&1
        "$VENV/bin/pip" install --quiet -r "$REPO/requirements.txt" >> "$LOG" 2>&1
        "$VENV/bin/pip" install --quiet mlx-whisper >> "$LOG" 2>&1

        # Verify it worked
        if ! "$VENV/bin/python3.12" -c "import uvicorn, pandas" 2>/dev/null; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: venv setup failed — check requirements." >> "$LOG"
        fi
    fi

    PYTHON="$VENV/bin/python3.12"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: Python 3.12 not found. Install with: brew install python@3.12" >> "$LOG"
    PYTHON="$(which python3)"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Using Python: $PYTHON ($("$PYTHON" --version 2>&1))" >> "$LOG"

echo "" >> "$LOG"
echo "======================================" >> "$LOG"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting trading server" >> "$LOG"
echo "======================================" >> "$LOG"

# ── Start Cloudflare named tunnel in background ───────────────────────────────
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
