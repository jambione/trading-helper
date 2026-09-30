#!/bin/bash
# Install or reinstall the overnight book scheduler as a LaunchAgent in the
# console user's GUI session. Idempotent and safe over ssh.
#   scripts/install_overnight_agent.sh          install / restart with the repo's plist
#   scripts/install_overnight_agent.sh remove   stop and uninstall
set -euo pipefail
LABEL=com.jambi.overnight-book
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/scripts/launchd/$LABEL.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "remove" ]; then
  rm -f "$DST"
  echo "removed $LABEL"
  exit 0
fi
mkdir -p "$HERE/logs"
plutil -lint "$SRC" >/dev/null
cp "$SRC" "$DST"
launchctl bootstrap "$DOMAIN" "$DST"
sleep 2
launchctl print "$DOMAIN/$LABEL" | grep -E "state =|pid =|last exit code" || true
