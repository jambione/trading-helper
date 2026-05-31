#!/usr/bin/env bash
# channel_switch.sh
# Runs at 7:15 AM — switches Discord to the day channel and rejoins the stream.

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"

echo ""
echo "========================================"
echo "  Channel Switch — $(date '+%H:%M:%S')"
echo "========================================"
echo ""

echo "[1/1] Switching Discord to day channel and rejoining stream..."
python3 "$REPO/click_join_voice.py" --channel 1132389452510142465 \
    && echo "      ✓ Switched channel and joined stream." \
    || echo "      ⚠ Could not auto-click — please switch manually."

echo ""
echo "========================================"
echo "  Done."
echo "========================================"
echo ""
