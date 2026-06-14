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

# ── 1. Start the local stack (dashboard + signal engine + tunnel) ─────────────
echo "[1/5] ./trading start  (dashboard + signal engine + Discord OCR + tunnel)"
./trading start

# Wait for the dashboard to answer, then confirm the signal engine is up.
echo "      Waiting for backend at $BACKEND ..."
for i in $(seq 1 60); do
    curl -s "$BACKEND/api/meta" > /dev/null 2>&1 && { echo "      ✓ Backend up (${i}s)."; break; }
    sleep 1
    [ "$i" -eq 60 ] && echo "      ⚠ Backend not responding after 60s — continuing anyway."
done

echo "      Verifying signal engine (paper trading)..."
sleep 2
ENG_RESP=$(curl -s "$BACKEND/api/state" 2>/dev/null)
ENG_MODE=$(echo "$ENG_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('version',{}).get('engine_strategy','') or '')" 2>/dev/null || true)
ENG_VER=$(echo "$ENG_RESP"  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('version',{}).get('engine','') or '')" 2>/dev/null || true)
if [ -n "$ENG_VER" ]; then
    echo "      ✓ Signal engine running  (build $ENG_VER · strategy $ENG_MODE)"
else
    echo "      ⚠ Signal engine not reporting yet — check logs/engine.log"
fi

# ── 2. Discord — open -daytrading-alerts and dock the window right for OCR ────
echo ""
echo "[2/5] Opening Discord → -daytrading-alerts (docked right)..."
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

# ── 3. Native toast notifier (BrasfieldNotifier.app) ─────────────────────────
# Posts macOS banners for bursts/BUYs and runs add-to-TV+WB on click. Build it
# once with scripts/build_notifier.sh; launch here if present.
echo ""
echo "[3/5] Launching toast notifier (BrasfieldNotifier.app)..."
if [ -d "$REPO/BrasfieldNotifier.app" ]; then
    open "$REPO/BrasfieldNotifier.app" \
        && echo "      ✓ Notifier launched (listening on :8890)." \
        || echo "      ⚠ Could not launch notifier."
else
    echo "      ⚠ BrasfieldNotifier.app not found — build it: scripts/build_notifier.sh"
fi

# ── 4. Arrange windows (background — fires after mac_agent starts) ───────────
echo ""
echo "[4/5] Scheduling window arrangement in 6 s..."
bash "$REPO/scripts/arrange_windows.sh" 6 &

# ── 5. macOS TradingView/Webull agent (foreground — keeps this window open) ────
echo ""
echo "[5/5] Launching macOS TradingView/Webull agent (mac_agent.sh)..."
echo "      Keep this window open while trading. Close it (or Ctrl+C) to stop the agent."
echo ""
cd "$REPO"
bash mac_agent.sh
