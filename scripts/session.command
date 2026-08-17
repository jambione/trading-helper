#!/usr/bin/env bash
# session.command — double-click from the Desktop for a full desk reset.
#
# Mini only (same as ./trading desk). Do not run on the MacBook.
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

if [ "$(id -un 2>/dev/null || true)" != "jambimac" ] \
   || [ "${HOME:-}" != "/Users/jambimac" ]; then
  echo "❌ session.command is Mini-only (user jambimac)."
  echo "   This machine is $(id -un)@$(hostname -s 2>/dev/null || hostname)."
  echo "Press any key to close."
  read -r -n1
  exit 1
fi

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
