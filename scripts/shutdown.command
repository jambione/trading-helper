#!/usr/bin/env bash
# shutdown.command — double-click from the Desktop to tear down the trading session.
#
#   1. Stop the transcriber (while the backend is still alive)
#   2. Quit Discord (leaves voice + closes the app)
#   3. ./trading stop  → dashboard + signal engine + Cloudflare tunnel
#   4. Kill the macOS TradingView/Webull agent (mac_agent.sh / mac_agent.py)
#   5. (optional) Clean up TradingView + Webull watchlists — see commented section
#
# Source of truth lives in the repo at scripts/shutdown.command; a copy sits on the
# Desktop for double-click convenience.

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
BACKEND="http://localhost:8888"

cd "$REPO" || { echo "❌ Could not cd to $REPO"; echo "Press any key to close."; read -r -n1; exit 1; }

echo ""
echo "============================================"
echo "  Brasfield Trading — Shutdown"
echo "============================================"
echo ""

# ── 1. Stop the transcriber (needs the backend, so do it first) ───────────────
echo "[1/4] Stopping transcription..."
RESPONSE=$(curl -s -X POST "$BACKEND/api/transcriber/stop" 2>/dev/null)
if echo "$RESPONSE" | grep -qE '"running":\s*false'; then
    echo "      ✓ Transcription stopped."
elif curl -s "$BACKEND/api/meta" > /dev/null 2>&1; then
    echo "      ✓ Stop signal sent."
else
    echo "      ⚠ Backend not reachable — skipping."
fi

# ── 2. Quit Discord ───────────────────────────────────────────────────────────
echo "[2/4] Quitting Discord..."
osascript -e 'tell application "Discord" to quit' 2>/dev/null || true
sleep 2
pkill -x Discord 2>/dev/null || true
echo "      ✓ Discord closed."

# ── 3. Stop the local stack (dashboard + engine + tunnel) ─────────────────────
echo "[3/4] ./trading stop"
./trading stop

# ── 4. Kill the macOS TradingView/Webull agent ────────────────────────────────
echo "[4/4] Stopping mac_agent..."
pkill -f "mac_agent.py" 2>/dev/null && echo "      ✓ mac_agent.py stopped." || echo "      • mac_agent.py was not running."
pkill -f "mac_agent.sh" 2>/dev/null || true

# ── 5. OPTIONAL: clean up TradingView + Webull watchlists ─────────────────────
# There is no API for this yet — it would be UI keystroke automation against the
# live apps, which is fragile. Fill in the exact keystrokes for your layout and
# uncomment to enable. TEST CAREFULLY: select-all + delete in the wrong window is
# destructive.
#
# echo "[5] Cleaning up watchlists..."
#
# # --- TradingView (Brave/Chrome, pinned tab Cmd+1) ---
# osascript <<'APPLESCRIPT'
# tell application "Brave Browser" to activate
# delay 0.5
# tell application "System Events"
#     key code 18 using {command down}   -- Cmd+1 → pinned TradingView tab
#     delay 0.6
#     -- TODO: click the watchlist pane, then your "remove all" / per-symbol delete
#     -- e.g. right-click watchlist → "Remove all symbols", or loop Delete on each row.
# end tell
# APPLESCRIPT
#
# # --- Webull Desktop ---
# osascript <<'APPLESCRIPT'
# tell application "Webull Desktop" to activate
# delay 0.5
# tell application "System Events"
#     -- TODO: navigate to the watchlist and remove symbols (Webull has no bulk clear;
#     -- you typically right-click each symbol → Remove, or use the edit/manage view).
# end tell
# APPLESCRIPT
#
# echo "      ✓ Watchlist cleanup done."

echo ""
echo "============================================"
echo "  Session ended."
echo "============================================"
echo ""
echo "This window can be closed."
