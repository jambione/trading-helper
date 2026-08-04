#!/bin/bash
# Double-click this in Finder (or run from Ghostty) to grant Screen Recording
# for Discord OCR. Must run in a normal GUI terminal so macOS can show the
# Allow dialog — agent/background shells often cannot.
set -euo pipefail
cd "$(dirname "$0")/.."
clear
echo "═══════════════════════════════════════════════════════"
echo "  Discord OCR — Screen Recording setup"
echo "═══════════════════════════════════════════════════════"
echo
echo "macOS will prompt to allow Screen Recording."
echo "Click  Allow  when asked."
echo
echo "Also enable in System Settings if listed:"
echo "  Privacy & Security → Screen & System Audio Recording"
echo "  • Ghostty (or Terminal / iTerm)"
echo "  • Python  (the one that runs the stack)"
echo

open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture" 2>/dev/null || true

PY="${PWD}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then PY="$(command -v python3)"; fi
echo "Using Python: $PY"
echo

# Request access from Python (shows TCC dialog when run interactively).
"$PY" - <<'PY'
import sys, time
from Quartz import (
    CGRequestScreenCaptureAccess,
    CGPreflightScreenCaptureAccess,
    CGDisplayCreateImage,
    CGMainDisplayID,
    CGImageGetWidth,
)

print("preflight:", CGPreflightScreenCaptureAccess())
if not CGPreflightScreenCaptureAccess():
    print("Requesting Screen Recording… click Allow…")
    ok = CGRequestScreenCaptureAccess()
    print("request returned:", ok)
    time.sleep(1)
print("preflight now:", CGPreflightScreenCaptureAccess())

img = CGDisplayCreateImage(CGMainDisplayID())
if img is None:
    print()
    print("✗ Capture still blocked.")
    print("  1. System Settings → Screen & System Audio Recording")
    print("  2. Enable Ghostty/Terminal and Python")
    print("  3. Quit & reopen Ghostty, then re-run this script")
    sys.exit(1)

print(f"✓ Capture OK ({CGImageGetWidth(img)} px wide)")
print()
print("Running OCR self-check…")
sys.exit(0)
PY

"$PY" discord_source.py --check || true
echo
echo "If VERDICT is ✓, start/restart the stack:"
echo "  ./trading restart"
echo
read -r -p "Press Return to close…"
