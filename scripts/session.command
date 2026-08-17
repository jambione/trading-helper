#!/usr/bin/env bash
# session.command — double-click from the Desktop for a full desk reset.
#
# Same as:  ./trading desk
#   shutdown.command  +  stack  +  Discord (jump to latest)  +  caffeinate
#   + window arrange  +  mac_agent (background)
#
# Source of truth: scripts/session.command
# Desktop copy:    ~/Desktop/session.command  (re-copy after edits)
#
#   chmod +x scripts/session.command
#   cp scripts/session.command ~/Desktop/session.command

set -uo pipefail

REPO="${REPO:-/Users/jambimac/repo/trading-helper}"

cd "$REPO" || {
  echo "❌ Could not cd to $REPO"
  echo "Press any key to close."
  read -r -n1
  exit 1
}

echo ""
echo "============================================"
echo "  Brasfield Trading — Desk session"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================"
echo ""

./trading desk
status=$?

echo ""
echo "This window can stay open (stack is in the background)."
echo "Press any key to close the Terminal only."
read -r -n1
exit "$status"
