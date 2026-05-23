#!/usr/bin/env bash
# uninstall_schedule.sh
# Removes the trading server schedule entirely.

AGENTS="$HOME/Library/LaunchAgents"

echo "=== Uninstalling Trading Helper Schedule ==="

launchctl unload "$AGENTS/com.trading.helper.start.plist" 2>/dev/null && echo "  Unloaded start agent" || echo "  Start agent was not loaded"
launchctl unload "$AGENTS/com.trading.helper.stop.plist"  2>/dev/null && echo "  Unloaded stop agent"  || echo "  Stop agent was not loaded"

rm -f "$AGENTS/com.trading.helper.start.plist"
rm -f "$AGENTS/com.trading.helper.stop.plist"
echo "  Removed plist files"

echo ""
echo "  To also remove the Mac wake schedule, run:"
echo "    sudo pmset repeat cancel"
echo ""
echo "Done."
