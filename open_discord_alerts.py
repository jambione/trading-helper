#!/usr/bin/env python3
"""
open_discord_alerts.py — bring Discord to the alert channel and dock it on the
right of the desktop, so the screen-OCR ticker source (discord_source.py) has a
clean, visible window to read.

Replaces the old click_join_voice.py: the OCR source reads the channel's TEXT,
so there is no voice/stream/PiP step anymore. We just:
  1. Launch / activate Discord.
  2. Open the target channel by its discord:// deep link (exact, no guessing).
  3. Dock the Discord window to the right half of the desktop.

Usage:
    python3 open_discord_alerts.py                                  # -daytrading-alerts
    python3 open_discord_alerts.py --channel-id 1132389452510142465 # a different channel
    python3 open_discord_alerts.py --no-dock                        # open, don't move window
"""
import argparse
import subprocess
import sys
import time

import pyautogui

pyautogui.FAILSAFE = False

# alerts + general messages channel (alerts + sentiment source).
SERVER_ID          = "822849028395892788"
DEFAULT_CHANNEL_ID = "839203064014831646"


def get_discord_window():
    """Use Quartz CGWindowList to find Discord's main (largest) window bounds."""
    try:
        from Quartz import (CGWindowListCopyWindowInfo,
                            kCGWindowListOptionOnScreenOnly,
                            kCGNullWindowID)
        wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                         kCGNullWindowID)
        best = None
        for w in wins:
            if 'Discord' not in str(w.get('kCGWindowOwnerName', '')):
                continue
            b = w.get('kCGWindowBounds', {})
            wsz, hsz = b.get('Width', 0), b.get('Height', 0)
            if best is None or (wsz * hsz) > (best['w'] * best['h']):
                best = {'x': b.get('X', 0), 'y': b.get('Y', 0), 'w': wsz, 'h': hsz}
        return best
    except Exception as e:
        print(f"  [Quartz error] {e}")
        return None


def activate_discord():
    """Launch Discord if needed and bring it to the front."""
    print("  Launching / activating Discord...")
    subprocess.run(['open', '-a', 'Discord'], capture_output=True)
    time.sleep(2)
    subprocess.run(['osascript', '-e', 'tell application "Discord" to activate'],
                   capture_output=True)
    time.sleep(1.5)


def open_channel(channel_id: str):
    """Open a channel by its discord:// deep link (exact navigation, no guessing)."""
    url = f"discord://discord.com/channels/{SERVER_ID}/{channel_id}"
    print(f"  Navigating to channel {channel_id}...")
    subprocess.run(['open', url], capture_output=True)
    time.sleep(4)              # let Discord navigate
    print("  ✓ Channel opened.")


def dock_right():
    """Dock the Discord window to the right half of the main desktop."""
    print("  Docking Discord to the right half of the desktop...")
    osa = (
        'tell application "Finder" to set b to bounds of window of desktop\n'
        'set sw to item 3 of b\n'
        'set sh to item 4 of b\n'
        'set rightX to (sw / 2) as integer\n'
        'tell application "Discord" to activate\n'
        'delay 0.3\n'
        'tell application "System Events" to tell process "Discord"\n'
        '    set position of window 1 to {rightX, 0}\n'
        '    set size of window 1 to {sw - rightX, sh}\n'
        'end tell\n'
    )
    r = subprocess.run(['osascript', '-e', osa], capture_output=True, text=True)
    if r.returncode == 0:
        win = get_discord_window()
        if win:
            print(f"  ✓ Discord docked right: {int(win['w'])}x{int(win['h'])} @ "
                  f"({int(win['x'])},{int(win['y'])}).")
        else:
            print("  ✓ Dock command sent.")
    else:
        print(f"  ⚠ Could not dock window (Accessibility permission?): {r.stderr.strip()}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel-id', default=DEFAULT_CHANNEL_ID,
                        help=f'Discord channel id to open (default: {DEFAULT_CHANNEL_ID})')
    parser.add_argument('--no-dock', dest='dock', action='store_false', default=True,
                        help="Open the channel but don't move/resize the window")
    args = parser.parse_args()

    activate_discord()
    open_channel(args.channel_id)
    if args.dock:
        dock_right()
    print("  ✓ Discord ready for OCR. Keep this window visible (not minimized).")
    return 0


if __name__ == '__main__':
    sys.exit(main())
