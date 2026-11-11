#!/bin/sh
# Installs (or removes) the weekly automatic picker on the mini. It records paper picks only and places no orders.
#   scripts/install_pick_positions.sh          install and schedule (weekdays 09:32)
#   scripts/install_pick_positions.sh now      install, schedule, and run once now
#   scripts/install_pick_positions.sh remove   unschedule (ledgers in ~/pick_ledgers are kept)
L=com.jambi.pick-positions; D="$HOME/Library/LaunchAgents/$L.plist"; U="gui/$(id -u)"
cd "$(dirname "$0")/.." || exit 1
launchctl bootout "$U/$L" 2>/dev/null
if [ "$1" = "remove" ]; then rm -f "$D"; echo "removed $L"; exit 0; fi
cp "scripts/$L.plist" "$D" && launchctl bootstrap "$U" "$D" && echo "installed $L (weekdays 09:32)"
[ "$1" = "now" ] && launchctl kickstart "$U/$L" && echo "started one run; see logs/pick_positions.log"
exit 0
