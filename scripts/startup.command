#!/usr/bin/env bash
# startup.command — double-click from the Desktop to start the full trading session.
#
#   1. ./trading start  → dashboard + signal engine + Cloudflare tunnel
#   2. Open Discord → Stock Scanners & Alerts, click Join Voice + Watch Stream,
#      then start the transcriber  (the Discord/transcriber steps from morning_start.sh)
#   3. Launch the macOS TradingView/Webull agent (mac_agent.sh).
#      This stays in the foreground and keeps THIS window open — leave it running.
#
# Source of truth lives in the repo at scripts/startup.command; a copy sits on the
# Desktop for double-click convenience. Edit the repo copy and re-copy if you change it.

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
BACKEND="http://localhost:8888"

cd "$REPO" || { echo "❌ Could not cd to $REPO"; echo "Press any key to close."; read -r -n1; exit 1; }

echo ""
echo "============================================"
echo "  Brasfield Trading — Startup"
echo "============================================"
echo ""

# ── 1. Start the local stack (dashboard + engine + tunnel) ────────────────────
echo "[1/3] ./trading start"
./trading start

# Make sure the backend is actually answering before we drive Discord / transcriber.
echo "      Waiting for backend at $BACKEND ..."
for i in $(seq 1 60); do
    curl -s "$BACKEND/api/meta" > /dev/null 2>&1 && { echo "      ✓ Backend up (${i}s)."; break; }
    sleep 1
    [ "$i" -eq 60 ] && echo "      ⚠ Backend not responding after 60s — continuing anyway."
done

# ── 2. Discord (join voice + watch stream) and transcriber ────────────────────
echo ""
echo "[2/3] Opening Discord → Stock Scanners & Alerts (join voice + watch stream)..."
python3 "$REPO/click_join_voice.py" \
    && echo "      ✓ Joined voice channel + Watch Stream." \
    || echo "      ⚠ Could not auto-click — please join manually."

echo "      Starting transcription..."
sleep 2
RESPONSE=$(curl -s -X POST "$BACKEND/api/transcriber/start" 2>/dev/null)
if echo "$RESPONSE" | grep -qE '"running":\s*true'; then
    echo "      ✓ Transcription started."
elif echo "$RESPONSE" | grep -qE '"running":\s*false'; then
    echo "      ⚠ Transcription did not start. Response: $RESPONSE"
else
    echo "      ✗ Could not reach backend. Response: ${RESPONSE:-(empty)}"
fi

# ── 3. macOS TradingView/Webull agent (foreground — keeps this window open) ────
echo ""
echo "[3/3] Launching macOS TradingView/Webull agent (mac_agent.sh)..."
echo "      Keep this window open while trading. Close it (or Ctrl+C) to stop the agent."
echo ""
cd "$REPO"
bash mac_agent.sh
