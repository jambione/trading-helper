#!/usr/bin/env bash
# channel_switch.sh
# Runs at 7:15 AM — re-asserts the Discord alert channel (-daytrading-alerts) and
# re-docks the window right, in case Discord drifted. The OCR source reads whatever
# channel is on screen, so keeping the alert channel up is all we need.

# Resolve the repo from this script's own location. The hardcoded home
# directory that used to sit here pointed at a path that does not exist on the
# trading host, so the 7:15 AM channel re-assert never actually ran.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo ""
echo "========================================"
echo "  Channel Switch — $(date '+%H:%M:%S')"
echo "========================================"
echo ""

echo "[1/1] Opening -daytrading-alerts and docking Discord right..."
"$REPO/.venv/bin/python" "$REPO/open_discord_alerts.py" \
    && echo "      ✓ Alert channel docked right." \
    || echo "      ⚠ Could not auto-open — open -daytrading-alerts and dock it right manually."

echo ""
echo "========================================"
echo "  Done."
echo "========================================"
echo ""
