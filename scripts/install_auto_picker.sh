#!/bin/sh
# Installs (or removes) the weekly automatic picker on the mini. It records paper picks only and places no orders.
#   scripts/install_auto_picker.sh          install and schedule (Mondays 08:30)
#   scripts/install_auto_picker.sh now      install, schedule, and run once now
#   scripts/install_auto_picker.sh remove   unschedule (ledgers in ~/pick_ledgers are kept)
L=com.jambi.auto-picker; D="$HOME/Library/LaunchAgents/$L.plist"; U="gui/$(id -u)"
cd "$(dirname "$0")/.." || exit 1
launchctl bootout "$U/$L" 2>/dev/null
if [ "$1" = "remove" ]; then rm -f "$D"; echo "removed $L"; exit 0; fi
cp "scripts/$L.plist" "$D" && launchctl bootstrap "$U" "$D" && echo "installed $L (Mondays 08:30)"
[ "$1" = "now" ] && launchctl kickstart "$U/$L" && echo "started one run; see logs/auto_picker.log"
exit 0
