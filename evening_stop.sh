#!/usr/bin/env bash
# evening_stop.sh
# Clean shutdown of the trading session:
#   1. Stop transcription
#   2. Disconnect from Discord voice + quit Discord
#   3. Switch audio back to MacBook Neo Speakers
#   4. Kill the trading server

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
BACKEND="http://localhost:8888"

echo ""
echo "========================================"
echo "  Brasfield Trading — Stopping Session"
echo "========================================"
echo ""

# ── 1. Stop transcription ─────────────────────────────────────────────────────
echo "[1/4] Stopping transcription..."
RESPONSE=$(curl -s -X POST "$BACKEND/api/transcriber/stop" 2>/dev/null)
if echo "$RESPONSE" | grep -q '"running":false'; then
    echo "      ✓ Transcription stopped."
elif curl -s "$BACKEND/api/meta" > /dev/null 2>&1; then
    echo "      ✓ Stop signal sent."
else
    echo "      ⚠ Backend not reachable — skipping."
fi

# ── 2. Disconnect from Discord voice + quit Discord ───────────────────────────
echo "[2/4] Disconnecting from Discord voice and quitting..."
osascript << 'APPLESCRIPT'
-- Disconnect from voice channel via keyboard shortcut (Ctrl+Shift+D in older Discord)
-- Newest Discord: just quit the app entirely
tell application "Discord"
    quit
end tell
APPLESCRIPT
sleep 2
# Force-kill if still running
pkill -x Discord 2>/dev/null || true
echo "      ✓ Discord closed."

# ── 3. Switch audio back to MacBook Neo Speakers ──────────────────────────────
echo "[3/4] Restoring audio output to MacBook Neo Speakers..."
if command -v SwitchAudioSource &>/dev/null; then
    SwitchAudioSource -s "MacBook Neo Speakers" 2>/dev/null \
        && echo "      ✓ Audio restored." \
        || echo "      ⚠ Could not switch — restore manually if needed."
else
    echo "      ⚠ SwitchAudioSource not installed — restore audio manually."
fi

# ── 4. Kill the trading server ────────────────────────────────────────────────
echo "[4/4] Shutting down trading server..."
bash "$REPO/stop_trading_server.sh"
echo "      ✓ Server stopped."

echo ""
echo "========================================"
echo "  Session ended. Ready for next morning."
echo "========================================"
echo ""
