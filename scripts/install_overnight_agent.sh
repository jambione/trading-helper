#!/bin/bash
# Install or reinstall the overnight book scheduler as a LaunchAgent in the
# console user's GUI session. Idempotent and safe over ssh.
#   scripts/install_overnight_agent.sh               install / restart the paper book
#   scripts/install_overnight_agent.sh remove        stop and uninstall the paper book
#   scripts/install_overnight_agent.sh live          install the REAL-MONEY test (the switch)
#   scripts/install_overnight_agent.sh remove live   stop and uninstall the real-money test
#   scripts/install_overnight_agent.sh monthend      install / restart the month-end pilot (remove monthend: uninstall)
set -euo pipefail
ACTION=install
[ "${1:-}" = "remove" ] && { ACTION=remove; shift; }
LABEL=com.jambi.overnight-book
[ "${1:-}" = "live" ] && LABEL=com.jambi.overnight-live
[ "${1:-}" = "monthend" ] && LABEL=com.jambi.month-end-book
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/scripts/$LABEL.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
# bootout returns before the job is gone; bootstrapping too soon fails with
# "Bootstrap failed: 5: Input/output error". Wait for launchd to let go.
for _ in $(seq 1 30); do
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
  sleep 1
done
if [ "$ACTION" = "remove" ]; then
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
