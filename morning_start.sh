#!/usr/bin/env bash
# morning_start.sh
# Automates the morning trading setup:
#   1. Switch Mac audio output to Multi-Output Device
#   2. Open Discord → Bullish Bob's Trading Hub → Stock Scanners & Alerts
#   3. Start transcription via the dashboard API

BACKEND="http://localhost:8888"
DISCORD_URL="discord://discord.com/channels/822849028395892788/822849029393612804"

echo ""
echo "========================================"
echo "  Brasfield Trading — Morning Setup"
echo "========================================"
echo ""

# ── 1. Switch audio output to Multi-Output Device ─────────────────────────────
echo "[1/3] Switching audio to Multi-Output Device..."
if ! command -v SwitchAudioSource &>/dev/null; then
    echo "      Installing SwitchAudioSource (one-time)..."
    brew install switchaudio-osx
fi
SwitchAudioSource -s "Multi-Output Device" 2>/dev/null \
    && echo "      ✓ Audio output set to Multi-Output Device." \
    || echo "      ⚠ Could not switch audio — check Audio MIDI Setup."

# ── 2. Open Discord in the right channel ──────────────────────────────────────
echo "[2/3] Opening Discord → Stock Scanners & Alerts..."
open "$DISCORD_URL"
echo "      Waiting 12s for Discord to load..."
sleep 12

# ── 3. Start transcription ────────────────────────────────────────────────────
echo "[3/3] Starting transcription..."
RESPONSE=$(curl -s -X POST "$BACKEND/api/transcriber/start" 2>/dev/null)
if echo "$RESPONSE" | grep -q '"running":true'; then
    echo "      ✓ Transcription started."
elif echo "$RESPONSE" | grep -q '"running"'; then
    echo "      ⚠ Transcription response: $RESPONSE"
else
    echo "      ✗ Could not reach backend at $BACKEND — is the server running?"
fi

echo ""
echo "========================================"
echo "  Setup complete!"
echo "  Dashboard: $BACKEND"
echo "  Note: Click 'Join Voice' in Discord if not auto-joined."
echo "========================================"
echo ""
