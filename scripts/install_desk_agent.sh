#!/usr/bin/env bash
# install_desk_agent.sh — let a deploy restart the desk without logging the
# research CLIs out.
#
# An SSH session cannot read the mini's login Keychain, so `./trading restart`
# over SSH brings the stack back with claude and agy logged out even though
# the credential never moved. Bootstrapping the stack restart as a LaunchAgent
# in the gui/<uid> domain runs it inside the console user's session instead,
# where that credential is readable.
#
# Verified 2026-08-29 on the mini, same second, only the session differing:
#     plain ssh        claude ok=False  agy ok=False
#     gui/501 agent    claude ok=True   agy ok=True
#
# Idempotent, and safe to run over ssh — `bootstrap` and `kickstart` against
# gui/<uid> are both permitted from a non-GUI session, unlike `asuser`.
#
#   scripts/install_desk_agent.sh          install / re-install
#   scripts/install_desk_agent.sh --status is it loaded?
#   scripts/install_desk_agent.sh --remove unload and delete
set -uo pipefail

LABEL="com.jambi.trading-desk"
REPO="${REPO:-/Users/jambimac/repo/trading-helper}"
SRC="$REPO/scripts/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"

if [ "$(id -un 2>/dev/null || true)" != "jambimac" ]; then
  echo "❌ Mini-only (user jambimac). This machine is $(id -un)."
  exit 1
fi

case "${1:-install}" in
  --status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      echo "✓ $LABEL is loaded in $DOMAIN"
      exit 0
    fi
    echo "✗ $LABEL is NOT loaded in $DOMAIN"
    exit 1
    ;;
  --remove)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
    rm -f "$DEST"
    echo "✓ removed $LABEL"
    exit 0
    ;;
esac

[ -f "$SRC" ] || { echo "❌ missing $SRC"; exit 1; }

# The GUI domain has to be reachable, which it is not when nobody is logged in
# at the console. Say so plainly rather than installing something inert.
if ! launchctl print "$DOMAIN" >/dev/null 2>&1; then
  echo "❌ $DOMAIN is not reachable — is anyone logged in at the mini's screen?"
  echo "   Without a console session there is no Keychain to inherit, and the"
  echo "   agent would be no better than the ssh restart it replaces."
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs"
cp "$SRC" "$DEST"

# bootout first so a re-install picks up an edited plist; ignore "not loaded".
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
if launchctl bootstrap "$DOMAIN" "$DEST" 2>&1; then
  echo "✓ $LABEL bootstrapped into $DOMAIN"
else
  echo "❌ bootstrap failed"
  exit 1
fi

echo ""
echo "Deploys will now restart the desk through this agent, which keeps the"
echo "Claude and Antigravity logins alive. Verify after the next restart:"
echo "    grep -E 'claude_auth|agy_auth' logs/ai_trader.log | tail -2"
