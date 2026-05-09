#!/usr/bin/env bash
# install_schedule.sh
# One-time setup: installs the 4 AM start / 11 AM stop schedule.
# Run once from Terminal:  bash install_schedule.sh

set -e
REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
AGENTS="$HOME/Library/LaunchAgents"

echo ""
echo "=== Trading Helper Schedule Installer ==="
echo ""

# ── 1. Make scripts executable ────────────────────────────────────────────────
echo "[1/4] Setting permissions on scripts..."
chmod +x "$REPO/run_trading_server.sh"
chmod +x "$REPO/stop_trading_server.sh"
chmod +x "$REPO/morning_start.sh"
echo "      Done."

# ── 2. Copy plists into LaunchAgents ─────────────────────────────────────────
echo "[2/4] Installing launchd agents..."
cp "$REPO/com.trading.helper.start.plist"   "$AGENTS/"
cp "$REPO/com.trading.helper.stop.plist"    "$AGENTS/"
cp "$REPO/com.trading.helper.morning.plist" "$AGENTS/"
echo "      Copied to $AGENTS"

# ── 3. Load the agents ────────────────────────────────────────────────────────
echo "[3/4] Loading agents into launchd..."
launchctl unload "$AGENTS/com.trading.helper.start.plist"   2>/dev/null || true
launchctl unload "$AGENTS/com.trading.helper.stop.plist"    2>/dev/null || true
launchctl unload "$AGENTS/com.trading.helper.morning.plist" 2>/dev/null || true

launchctl load "$AGENTS/com.trading.helper.start.plist"
launchctl load "$AGENTS/com.trading.helper.stop.plist"
launchctl load "$AGENTS/com.trading.helper.morning.plist"
echo "      Loaded."

# ── 4. Wake schedule via pmset ────────────────────────────────────────────────
echo "[4/4] Setting Mac wake schedule (3:55 AM Mon-Fri, 5 min before server starts)..."
echo "      This requires your password (uses sudo)."
sudo pmset repeat wake MTWRF 03:55:00
echo "      Wake schedule set."

echo ""
echo "=== Installation complete! ==="
echo ""
echo "  Mac will WAKE         at 3:55 AM Mon-Fri (via pmset)"
echo "  Server will START     at 4:00 AM Mon-Fri"
echo "  Discord + Transcribe  at 4:00 AM Mon-Fri"
echo "  Server will STOP      at 11:00 AM Mon-Fri"
echo ""
echo "To test RIGHT NOW without waiting for 4 AM, run:"
echo "  bash morning_start.sh"
echo ""
echo "To check logs at any time:"
echo "  tail -f $REPO/server.log"
echo ""
echo "To uninstall:"
echo "  bash $REPO/uninstall_schedule.sh"
