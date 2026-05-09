#!/usr/bin/env bash
# morning_start.sh
# Automates the morning trading setup:
#   1. Start backend server if not already running
#   2. Switch Mac audio output to Multi-Output Device
#   3. Open Discord → Stock Scanners & Alerts and click Join Voice
#   4. Start transcription via the dashboard API

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
BACKEND="http://localhost:8888"

echo ""
echo "========================================"
echo "  Brasfield Trading — Morning Setup"
echo "========================================"
echo ""

# ── 1. Start backend if not running ───────────────────────────────────────────
echo "[1/4] Checking backend server..."
if curl -s "$BACKEND/api/meta" > /dev/null 2>&1; then
    echo "      ✓ Server already running."
else
    echo "      Server not running — starting it now..."
    osascript -e "tell application \"Terminal\" to do script \"bash $REPO/run_trading_server.sh\""
    echo "      Waiting up to 30s for server to come up..."
    for i in $(seq 1 30); do
        sleep 1
        if curl -s "$BACKEND/api/meta" > /dev/null 2>&1; then
            echo "      ✓ Server is up (${i}s)."
            break
        fi
        if [ "$i" -eq 30 ]; then
            echo "      ✗ Server did not start in time. Check Terminal for errors."
            exit 1
        fi
    done
fi

# ── 2. Switch audio output to Multi-Output Device ─────────────────────────────
echo "[2/4] Switching audio to Multi-Output Device..."
if ! command -v SwitchAudioSource &>/dev/null; then
    echo "      Installing SwitchAudioSource (one-time)..."
    brew install switchaudio-osx
fi
SwitchAudioSource -s "Multi-Output Device" 2>/dev/null \
    && echo "      ✓ Audio output set to Multi-Output Device." \
    || echo "      ⚠ Could not switch audio — check Audio MIDI Setup."

# ── 3. Open Discord → right channel and click Join Voice ──────────────────────
echo "[3/4] Opening Discord → Stock Scanners & Alerts and clicking Join Voice..."
python3 "$REPO/click_join_voice.py" \
    && echo "      ✓ Joined voice channel." \
    || echo "      ⚠ Could not auto-click — please click Join Voice manually."

# ── 4. Start transcription ────────────────────────────────────────────────────
echo "[4/4] Starting transcription..."
sleep 2
RESPONSE=$(curl -s -X POST "$BACKEND/api/transcriber/start" 2>/dev/null)
if echo "$RESPONSE" | grep -q '"running":true'; then
    echo "      ✓ Transcription started."
elif echo "$RESPONSE" | grep -q '"running"'; then
    echo "      ⚠ Response: $RESPONSE"
else
    echo "      ✗ Could not reach backend at $BACKEND"
fi

echo ""
echo "========================================"
echo "  Setup complete!  Dashboard → $BACKEND"
echo "========================================"
echo ""
