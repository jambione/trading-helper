#!/usr/bin/env bash
# Bootstrap the momentum monitor on a fresh macOS (or Linux) machine.
#
#   bash scripts/setup_monitor.sh
#
# Creates a venv, installs only the monitor's dependencies, scaffolds the
# optional env file, and runs preflight checks. Safe to re-run.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv-monitor"
PY_MIN_MAJOR=3
PY_MIN_MINOR=10

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
hdr()  { printf '\n\033[1m%s\033[0m\n%s\n' "$1" "$(printf '─%.0s' {1..66})"; }

hdr "1. Python"

# Find an interpreter meeting the floor. alpaca_trader.py uses `bool | None` in
# a def signature WITHOUT `from __future__ import annotations`, which is a
# runtime TypeError before 3.10 — so 3.10 is a hard floor, not a preference.
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c "import sys; sys.exit(0 if sys.version_info >= ($PY_MIN_MAJOR,$PY_MIN_MINOR) else 1)" 2>/dev/null; then
      PY="$(command -v "$cand")"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  bad "No Python >= ${PY_MIN_MAJOR}.${PY_MIN_MINOR} found."
  echo "     macOS:  brew install python@3.12"
  echo "     Linux:  sudo apt install python3.12 python3.12-venv"
  exit 1
fi
ok "$("$PY" -V) at $PY"

hdr "2. Virtual environment"
if [ -d "$VENV" ]; then
  ok "reusing $VENV"
else
  "$PY" -m venv "$VENV" || { bad "venv creation failed"; exit 1; }
  ok "created $VENV"
fi
VPY="$VENV/bin/python"

hdr "3. Dependencies"
"$VPY" -m pip install --upgrade pip --quiet 2>/dev/null
REQ="$REPO/momentum-monitor/requirements-monitor.txt"
if [ ! -f "$REQ" ]; then
  bad "missing $REQ"; exit 1
fi
# Deliberately NOT the repo-root requirements.txt: it pulls in the whole
# project and its unguarded pywin32 pin fails to resolve on macOS.
if "$VPY" -m pip install -r "$REQ" --quiet; then
  ok "installed from requirements-monitor.txt"
else
  warn "some packages failed — rerun without --quiet to see details"
fi

hdr "4. Configuration"
if [ -f "$REPO/signal_engine.env" ]; then
  ok "signal_engine.env present"
else
  if [ -f "$REPO/signal_engine.env.example" ]; then
    cp "$REPO/signal_engine.env.example" "$REPO/signal_engine.env"
    ok "created signal_engine.env from example (credentials blank)"
    warn "monitor runs read-only until ALPACA_API_KEY/SECRET are filled in"
  else
    warn "no signal_engine.env.example — monitor will run read-only"
  fi
fi
[ -f "$REPO/momentum-monitor/momentum_config.json" ] \
  && ok "momentum_config.json present" \
  || warn "momentum_config.json missing — built-in defaults will be used"

hdr "5. Preflight"
"$VPY" - <<'PYEOF'
import importlib, sys, os

def check(mod, label, required=False):
    try:
        importlib.import_module(mod)
        print(f"  \033[32m✓\033[0m {label}")
        return True
    except Exception:
        mark = "\033[31m✗\033[0m" if required else "\033[33m!\033[0m"
        print(f"  {mark} {label} — not installed")
        return False

rich_ok = check("rich", "rich (required — UI)", required=True)
pag_ok  = check("pyautogui", "pyautogui (TradingView hotkeys)")
check("alpaca", "alpaca-py (B/S keys + P&L)")
check("plyer", "plyer (desktop toasts)")

if sys.platform == "darwin" and pag_ok:
    import ctypes
    try:
        aps = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        aps.AXIsProcessTrusted.restype = ctypes.c_bool
        if aps.AXIsProcessTrusted():
            print("  \033[32m✓\033[0m Accessibility granted to this process")
        else:
            print("  \033[31m✗\033[0m Accessibility NOT granted — hotkeys will be")
            print("      silently discarded. Grant it to the app that LAUNCHES")
            print("      the monitor (Terminal.app), not to the python binary:")
            print("      System Settings > Privacy & Security > Accessibility")
    except Exception:
        print("  \033[33m!\033[0m could not query Accessibility")

if not rich_ok:
    print("\n  rich is required. Install it before launching.")
    sys.exit(1)
PYEOF

hdr "Next"
cat <<EOF
  Launch:   $VENV/bin/python momentum-monitor/momentum_signal.py
  Run it from a real terminal window — single-key hotkeys need a TTY.

  Before the 1-9 keys will work:
    • Brave or Chrome running
    • a TradingView chart pinned at tab BRAVE_TV_TAB (default 1)
    • macOS only: Accessibility granted to your terminal app

  See momentum-monitor/DEPLOYMENT.md for the full checklist.
EOF
