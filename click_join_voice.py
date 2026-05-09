#!/usr/bin/env python3
"""
click_join_voice.py
Finds and clicks the Discord "Join Voice" button using screen image matching.
Run after Discord has navigated to the Stock Scanners & Alerts channel.
"""
import subprocess, sys, time
import pyautogui
from PIL import ImageGrab
import numpy as np

def find_and_click_join_voice(max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        print(f"  Attempt {attempt}/{max_attempts}...")

        # Grab full screen
        screen = ImageGrab.grab()
        w, h = screen.size
        px = np.array(screen)

        # The "Join Voice" button in Discord dark theme is a distinctive
        # light-gray rounded rectangle on a dark navy/charcoal background.
        # Search the center region of the screen where it always appears.
        x1, x2 = int(w * 0.25), int(w * 0.80)
        y1, y2 = int(h * 0.45), int(h * 0.85)
        region = px[y1:y2, x1:x2].astype(int)

        r, g, b = region[:,:,0], region[:,:,1], region[:,:,2]

        # Button is light-gray: all channels 160-240, channels close to each other
        mask = (
            (r > 160) & (r < 240) &
            (g > 160) & (g < 240) &
            (b > 160) & (b < 240) &
            (np.abs(r - g) < 20) &
            (np.abs(g - b) < 20)
        )

        # Find contiguous bright regions — the biggest one is the button
        from scipy import ndimage
        labeled, num_features = ndimage.label(mask)
        if num_features == 0:
            print("  No button-like region found, retrying...")
            time.sleep(2)
            continue

        # Pick the largest labeled region
        sizes = ndimage.sum(mask, labeled, range(1, num_features + 1))
        largest = int(np.argmax(sizes)) + 1
        ys, xs = np.where(labeled == largest)

        click_x = x1 + int(np.median(xs))
        click_y = y1 + int(np.median(ys))

        print(f"  Found button at screen position ({click_x}, {click_y})")
        pyautogui.click(click_x, click_y)
        time.sleep(1)
        print("  Clicked!")
        return True

    print("  Could not find Join Voice button after all attempts.")
    return False

if __name__ == '__main__':
    # Give Discord focus first
    subprocess.run(['osascript', '-e', 'tell application "Discord" to activate'],
                   capture_output=True)
    time.sleep(1.5)

    success = find_and_click_join_voice()
    sys.exit(0 if success else 1)
