#!/usr/bin/env bash
# morning_start.sh
# Automates the morning trading setup:
#   1. Start backend server if not already running (also launches the Discord OCR source)
#   2. Open Discord → Stock Scanners & Alerts so the alert channel is on screen
#   3. Verify the Discord OCR source is live via the dashboard API
#
# Source: the ticker source is screen OCR of the Discord alert channel (discord_source.py,
# auto-started by start_all.py). The Discord window must stay visible (not minimized).

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
BACKEND="http://localhost:8888"

echo ""
echo "========================================"
echo "  Brasfield Trading — Morning Setup"
echo "========================================"
echo ""

# ── 1. Start backend if not running ───────────────────────────────────────────
echo "[1/3] Checking backend server..."
if curl -s "$BACKEND/api/meta" > /dev/null 2>&1; then
    echo "      ✓ Server already running at $BACKEND"
else
    echo "      Server not running — starting it now..."
    nohup bash "$REPO/scripts/run_trading_server.sh" >> "$REPO/server.log" 2>&1 &
    echo "      Waiting up to 60s for server to come up..."
    for i in $(seq 1 60); do
        sleep 1
        if curl -s "$BACKEND/api/meta" > /dev/null 2>&1; then
            echo "      ✓ Server is up (${i}s)."
            break
        fi
        if [ "$i" -eq 60 ]; then
            echo "      ✗ Server did not start in 60s. Check $REPO/server.log for errors."
            exit 1
        fi
    done
fi

# ── 2. Open Discord → -daytrading-alerts and dock right for OCR ───────────────
echo "[2/3] Opening Discord → -daytrading-alerts (docked right)..."
"$REPO/venv/bin/python" "$REPO/open_discord_alerts.py" \
    && echo "      ✓ Discord alert channel docked right." \
    || echo "      ⚠ Could not auto-open — open -daytrading-alerts and dock it right manually."

# ── 3. Verify the Discord OCR source is live ──────────────────────────────────
echo "[3/3] Verifying Discord OCR source..."
sleep 2
RESPONSE=$(curl -s "$BACKEND/api/discord/status" 2>/dev/null)

# "running": true means discord_source.py is alive and sending heartbeats.
if echo "$RESPONSE" | grep -qE '"running":\s*true'; then
    echo "      ✓ Discord OCR source is live."
elif [ -n "$RESPONSE" ]; then
    echo "      ⚠ OCR source not live yet (it starts with the server). Response: $RESPONSE"
else
    echo "      ✗ Could not reach backend at $BACKEND"
fi

echo ""
echo "========================================"
echo "  Setup complete!  Dashboard → $BACKEND"
echo "========================================"
echo ""
