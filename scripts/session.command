#!/usr/bin/env bash
# session.command — double-click from the Desktop for a full desk reset.
#
# Same as:  ./trading desk
#   1. shutdown.command
#   2. close Terminal / iTerm windows
#   3. caffeinate -si
#   4. startup.command  (new Terminal; Discord jumps to latest)
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
# desk quits Terminal; if we are still here (SSH), do not wait on a key.
exit 0
