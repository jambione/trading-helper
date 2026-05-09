#!/usr/bin/env bash
# morning_start.sh
# Automates the morning trading setup:
#   1. Switch Mac audio output to Multi-Output Device
#   2. Open Discord → Stock Scanners & Alerts and click Join Voice
#   3. Start transcription via the dashboard API

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
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

# ── 2. Open Discord and click Join Voice ──────────────────────────────────────
echo "[2/3] Opening Discord → Stock Scanners & Alerts..."
open "$DISCORD_URL"
echo "      Waiting 10s for Discord to load..."
sleep 10

echo "      Clicking 'Join Voice'..."
osascript << 'APPLESCRIPT'
tell application "Discord" to activate
delay 2
tell application "System Events"
    tell process "Discord"
        -- look for the Join Voice button and click it
        set btns to every button of window 1 whose name contains "Join Voice"
        if (count of btns) > 0 then
            click item 1 of btns
        else
            -- fallback: search deeper in UI tree
            set allBtns to every UI element of window 1 whose description contains "Join Voice"
            if (count of allBtns) > 0 then
                click item 1 of allBtns
            end if
        end if
    end tell
end tell
APPLESCRIPT

echo "      ✓ Join Voice clicked (or already in channel)."

# ── 3. Start transcription ────────────────────────────────────────────────────
echo "[3/3] Starting transcription..."
sleep 3
RESPONSE=$(curl -s -X POST "$BACKEND/api/transcriber/start" 2>/dev/null)
if echo "$RESPONSE" | grep -q '"running":true'; then
    echo "      ✓ Transcription started."
elif echo "$RESPONSE" | grep -q '"running"'; then
    echo "      ⚠ Response: $RESPONSE"
else
    echo "      ✗ Could not reach backend at $BACKEND — is the server running?"
fi

echo ""
echo "========================================"
echo "  Setup complete! Dashboard: $BACKEND"
echo "========================================"
echo ""
