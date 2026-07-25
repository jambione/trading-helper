#!/usr/bin/env bash
# mac_agent.sh — Launch the macOS TradingView/TradingView agent
#
# TradingView setup (do this once before running):
#   1. Open Brave or Google Chrome.
#   2. Go to https://www.tradingview.com/chart/ and load any chart.
#   3. Right-click the TradingView tab → "Pin tab".
#   4. Drag it so it is the FIRST tab (Cmd+1 switches to it).
#      If TradingView lives on a different tab, set BRAVE_TV_TAB in .env.
#
# TradingView setup (optional):
#   Install "TradingView Desktop" from the Mac App Store.
#   The agent skips TradingView adds if the app is not found.
#
# Accessibility (required for keyboard automation):
#   System Settings → Privacy & Security → Accessibility → add Terminal ✓

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

# ── Locate Python ─────────────────────────────────────────────────────────────
VENV_PYTHON="$ROOT/venv/bin/python"
if [ -f "$VENV_PYTHON" ]; then
    PYEXE="$VENV_PYTHON"
elif command -v python3 &>/dev/null; then
    PYEXE="$(command -v python3)"
else
    echo "❌ Python 3 was not found."
    echo "   Install it from https://www.python.org/downloads/ or via Homebrew:"
    echo "       brew install python"
    exit 1
fi

echo "Using Python: $PYEXE"

# ── Check / install required packages ────────────────────────────────────────
if ! "$PYEXE" -c "import pyautogui" &>/dev/null; then
    echo "Installing pyautogui..."
    "$PYEXE" -m pip install --quiet pyautogui
    if ! "$PYEXE" -c "import pyautogui" &>/dev/null; then
        echo "❌ Failed to install pyautogui. Try manually:"
        echo "       $PYEXE -m pip install pyautogui"
        exit 1
    fi
    echo "✅ pyautogui installed."
fi

echo
echo "Listening on http://localhost:8889"
echo "Keep this window open while using the dashboard."
echo

"$PYEXE" "$ROOT/mac_agent.py"

echo
echo "Agent stopped."
