#!/bin/bash
# trading.command — double-click to open the momentum monitor (desk UI).
#
# Polls the *server* dashboard feed (DASHBOARD_URL, default
# https://trading.jbrasfield.com). Does NOT start the local trading stack.
#
# Source of truth: scripts/trading.command
# Desktop copy:    ~/Desktop/trading.command  (re-copy after edits)
#
#   chmod +x scripts/trading.command
#   cp scripts/trading.command ~/Desktop/trading.command

set -uo pipefail

# Cannot self-locate off BASH_SOURCE like the scripts/ helpers do: this file is
# meant to be copied to ~/Desktop, where dirname would resolve to the Desktop.
# Absolute default, overridable by env for a differently-placed checkout.
REPO="${REPO:-/Users/jambimac/repo/trading-helper}"
VENV_PY="$REPO/.venv-monitor/bin/python"
MONITOR="$REPO/momentum-monitor/momentum_signal.py"

cd "$REPO" || {
  echo "❌ Could not cd to $REPO"
  echo "Press any key to close."
  read -r -n1
  exit 1
}

if [ ! -x "$VENV_PY" ]; then
  echo "❌ Monitor venv missing at $VENV_PY"
  echo "   Run once:  bash scripts/setup_monitor.sh"
  echo "Press any key to close."
  read -r -n1
  exit 1
fi

if [ ! -f "$MONITOR" ]; then
  echo "❌ Monitor not found: $MONITOR"
  echo "Press any key to close."
  read -r -n1
  exit 1
fi

# Prefer server feed if env is unset (local stack should stay off on this Mac).
export DASHBOARD_URL="${DASHBOARD_URL:-https://trading.jbrasfield.com}"

echo ""
echo "============================================"
echo "  Momentum Monitor  $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Feed: $DASHBOARD_URL"
echo "  Ctrl+C to stop"
echo "============================================"
echo ""

"$VENV_PY" "$MONITOR"
status=$?

echo ""
if [ "$status" -ne 0 ]; then
  echo "Momentum desk exited with code $status."
else
  echo "Momentum desk exited."
fi
read -r -n1 -s -p "Press any key to close…"
echo ""
exit "$status"
