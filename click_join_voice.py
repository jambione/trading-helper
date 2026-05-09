#!/usr/bin/env python3
"""
click_join_voice.py
Clicks the Discord "Join Voice" button using window-relative coordinates.
Works for Electron apps where accessibility APIs don't expose button names.
"""
import subprocess, sys, time
import pyautogui

pyautogui.FAILSAFE = False

def get_discord_window_bounds():
    """Get Discord window bounds via AppleScript."""
    script = '''
    tell application "System Events"
        tell process "Discord"
            set b to bounds of front window
            return (item 1 of b) & "," & (item 2 of b) & "," & (item 3 of b) & "," & (item 4 of b)
        end tell
    end tell
    '''
    result = subprocess.run(['osascript', '-e', script],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    parts = [int(x.strip()) for x in result.stdout.strip().split(',')]
    return {'left': parts[0], 'top': parts[1], 'right': parts[2], 'bottom': parts[3]}

def click_join_voice():
    # Bring Discord to front
    subprocess.run(['osascript', '-e', 'tell application "Discord" to activate'],
                   capture_output=True)
    time.sleep(1.5)

    bounds = get_discord_window_bounds()
    if not bounds:
        print("  Could not get Discord window bounds.")
        return False

    left   = bounds['left']
    top    = bounds['top']
    width  = bounds['right']  - bounds['left']
    height = bounds['bottom'] - bounds['top']

    print(f"  Discord window: {width}x{height} at ({left},{top})")

    # Discord sidebar is ~27% of window width.
    # "Join Voice" button is centered in the content area, ~72% down.
    sidebar_w = width * 0.27
    content_center_x = left + sidebar_w + (width - sidebar_w) / 2
    button_y         = top  + height * 0.72

    click_x = int(content_center_x)
    click_y = int(button_y)

    print(f"  Clicking Join Voice at ({click_x}, {click_y})...")
    pyautogui.moveTo(click_x, click_y, duration=0.3)
    pyautogui.click()
    time.sleep(0.5)
    print("  Done.")
    return True

if __name__ == '__main__':
    success = click_join_voice()
    sys.exit(0 if success else 1)
