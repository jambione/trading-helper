#!/usr/bin/env python3
"""
click_join_voice.py — click Discord "Join Voice" using Quartz window detection.
"""
import subprocess, sys, time
import pyautogui

pyautogui.FAILSAFE = False

def get_discord_window():
    """Use Quartz CGWindowList to find Discord's main window bounds."""
    try:
        from Quartz import (CGWindowListCopyWindowInfo,
                            kCGWindowListOptionOnScreenOnly,
                            kCGNullWindowID)
        wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                         kCGNullWindowID)
        best = None
        for w in wins:
            owner = str(w.get('kCGWindowOwnerName', ''))
            if 'Discord' not in owner:
                continue
            bounds = w.get('kCGWindowBounds', {})
            width  = bounds.get('Width', 0)
            height = bounds.get('Height', 0)
            # Grab the largest Discord window (main app, not helpers)
            if best is None or (width * height) > (best['w'] * best['h']):
                best = {
                    'x': bounds.get('X', 0),
                    'y': bounds.get('Y', 0),
                    'w': width,
                    'h': height,
                    'name': owner,
                    'title': w.get('kCGWindowName', ''),
                }
        return best
    except Exception as e:
        print(f"  [Quartz error] {e}")
        return None

def click_join_voice():
    print("  Activating Discord...")
    subprocess.run(['osascript', '-e', 'tell application "Discord" to activate'],
                   capture_output=True)
    time.sleep(2)

    win = get_discord_window()
    if not win:
        print("  ✗ Could not find Discord window via Quartz.")
        sys.exit(1)

    print(f"  Found: '{win['name']}' / '{win['title']}'  {win['w']}x{win['h']} @ ({win['x']},{win['y']})")

    # Discord layout (dark mode, standard):
    #   Left sidebar (server list + channel list): ~27% of total width
    #   "Join Voice" button: centred in content area, ~72% down
    sidebar_frac = 0.27
    btn_y_frac   = 0.72

    content_start = win['x'] + win['w'] * sidebar_frac
    content_cx    = content_start + (win['w'] * (1 - sidebar_frac)) / 2
    btn_x = int(content_cx)
    btn_y = int(win['y'] + win['h'] * btn_y_frac)

    print(f"  Targeting Join Voice at ({btn_x}, {btn_y}) ...")
    pyautogui.moveTo(btn_x, btn_y, duration=0.4)
    time.sleep(0.2)
    pyautogui.click()
    print("  ✓ Clicked.")
    return True

if __name__ == '__main__':
    click_join_voice()
