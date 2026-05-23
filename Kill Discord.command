#!/usr/bin/env bash
echo "Closing Discord..."
osascript -e 'tell application "Discord" to quit' 2>/dev/null
sleep 2
pkill -x Discord 2>/dev/null && echo "✓ Discord closed." || echo "✓ Discord was not running."
