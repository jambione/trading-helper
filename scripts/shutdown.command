#!/usr/bin/env bash
# shutdown.command — double-click from the Desktop to tear down the trading session.
#
#   1. Quit Discord (closes the alert window the OCR source reads)
#   2. ./trading stop  → dashboard + signal engine + Discord OCR source + tunnel
#   3. Kill the macOS TradingView/Webull agent (mac_agent.sh / mac_agent.py)
#   4. (optional) Clean up TradingView + Webull watchlists — see commented section
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

# ── 1. Quit Discord ───────────────────────────────────────────────────────────
echo "[1/3] Quitting Discord..."
osascript -e 'tell application "Discord" to quit' 2>/dev/null || true
sleep 2
pkill -x Discord 2>/dev/null || true
echo "      ✓ Discord closed."

# ── 2. Stop the local stack (dashboard + engine + Discord OCR source + tunnel) ─
echo "[2/3] ./trading stop"
./trading stop

# ── 3. Kill the macOS TradingView/Webull agent ────────────────────────────────
echo "[3/3] Stopping mac_agent..."
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
