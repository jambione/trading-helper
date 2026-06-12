#!/usr/bin/env bash
# startup.command — double-click from the Desktop to start the full trading session.
#
#   1. ./trading start  → dashboard + signal engine + Discord OCR source + Cloudflare tunnel
#   2. Open Discord → Stock Scanners & Alerts so the alert channel is on screen for OCR
#   3. Arrange windows  → Brave left, Webull right, Terminal minimized (runs in background)
#   4. Launch the macOS TradingView/Webull agent (mac_agent.sh).
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
echo "[1/4] ./trading start"
./trading start

# Make sure the backend is actually answering before we drive Discord.
echo "      Waiting for backend at $BACKEND ..."
for i in $(seq 1 60); do
    curl -s "$BACKEND/api/meta" > /dev/null 2>&1 && { echo "      ✓ Backend up (${i}s)."; break; }
    sleep 1
    [ "$i" -eq 60 ] && echo "      ⚠ Backend not responding after 60s — continuing anyway."
done

# ── 2. Discord — open -daytrading-alerts and dock the window right for OCR ────
echo ""
echo "[2/4] Opening Discord → -daytrading-alerts (docked right)..."
"$REPO/venv/bin/python" "$REPO/open_discord_alerts.py" \
    && echo "      ✓ Discord alert channel docked right." \
    || echo "      ⚠ Could not auto-open — open -daytrading-alerts and dock it right manually."

echo "      Verifying Discord OCR source..."
sleep 2
RESPONSE=$(curl -s "$BACKEND/api/discord/status" 2>/dev/null)
if echo "$RESPONSE" | grep -qE '"running":\s*true'; then
    echo "      ✓ Discord OCR source is live."
elif [ -n "$RESPONSE" ]; then
    echo "      ⚠ OCR source not live yet (it starts with the server). Response: $RESPONSE"
else
    echo "      ✗ Could not reach backend. Response: ${RESPONSE:-(empty)}"
fi

# ── 3. Arrange windows (background — fires after mac_agent starts) ───────────
echo ""
echo "[3/4] Scheduling window arrangement in 6 s..."
bash "$REPO/scripts/arrange_windows.sh" 6 &

# ── 4. macOS TradingView/Webull agent (foreground — keeps this window open) ────
echo ""
echo "[4/4] Launching macOS TradingView/Webull agent (mac_agent.sh)..."
echo "      Keep this window open while trading. Close it (or Ctrl+C) to stop the agent."
echo ""
cd "$REPO"
bash mac_agent.sh
