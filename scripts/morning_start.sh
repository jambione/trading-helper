#!/usr/bin/env bash
# morning_start.sh
# Automates the morning trading setup:
#   1. Start backend server if not already running
#   2. Open Discord → Stock Scanners & Alerts, click Join Voice + Watch Stream
#   3. Start transcription via the dashboard API
#
# Audio: transcription uses ScreenCaptureKit (no BlackHole / Multi-Output Device needed).

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

# ── 2. Open Discord → right channel, click Join Voice + Watch Stream ──────────
echo "[2/3] Opening Discord → Stock Scanners & Alerts..."
python3 "$REPO/click_join_voice.py" \
    && echo "      ✓ Joined voice channel + Watch Stream." \
    || echo "      ⚠ Could not auto-click — please join manually."

# ── 3. Start transcription ────────────────────────────────────────────────────
echo "[3/3] Starting transcription..."
sleep 2
RESPONSE=$(curl -s -X POST "$BACKEND/api/transcriber/start" 2>/dev/null)

# Python JSON can include a space after the colon ("running": true) or not
if echo "$RESPONSE" | grep -qE '"running":\s*true'; then
    echo "      ✓ Transcription started."
elif echo "$RESPONSE" | grep -qE '"running":\s*false'; then
    echo "      ⚠ Transcription did not start. Response: $RESPONSE"
else
    echo "      ✗ Could not reach backend at $BACKEND"
    echo "      Response was: ${RESPONSE:-(empty)}"
fi

echo ""
echo "========================================"
echo "  Setup complete!  Dashboard → $BACKEND"
echo "========================================"
echo ""
