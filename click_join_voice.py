#!/usr/bin/env python3
"""
click_join_voice.py — navigate to a Discord channel, click Join Voice, click Watch Stream.

By default it then pops the stream out into Discord's floating Picture-in-Picture
window. The PiP keeps rendering (and playing audio into ScreenCaptureKit) even
when the main Discord window is minimized or buried behind your other apps — so
you can run everything minimized and transcription keeps working. Pass
--no-pop-out to skip this and leave the stream in the main window.

Usage:
    python3 click_join_voice.py                         # default morning channel
    python3 click_join_voice.py --channel 1132389452510142465   # switch channel
    python3 click_join_voice.py --no-pop-out            # don't pop out the stream
"""
import argparse, subprocess, sys, time
import pyautogui

pyautogui.FAILSAFE = False

SERVER_ID          = "822849028395892788"
MORNING_CHANNEL_ID = "822849029393612804"   # Stock Scanners & Alerts (4 AM)
DAY_CHANNEL_ID     = "1132389452510142465"  # 7:15 AM channel switch

# Stream pop-out: the "Pop Out" (Picture-in-Picture) button lives in the stream's
# hover control bar at the bottom-right. These are fractions of the content area
# (right of the sidebar) — tune them like the Join Voice / Watch Stream coords
# above if the click misses on your Discord layout.
POPOUT_X_FRAC = 0.88   # across the content area (0 = just right of sidebar, 1 = right edge)
POPOUT_Y_FRAC = 0.92   # down the window (bottom control bar)


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


def click_join_voice(channel_id: str = MORNING_CHANNEL_ID, pop_out: bool = True):
    DISCORD_URL = f"discord://discord.com/channels/{SERVER_ID}/{channel_id}"
    print(f"  Navigating to channel {channel_id}...")
    subprocess.run(['open', DISCORD_URL])
    time.sleep(4)  # wait for Discord to navigate to the channel

    print("  Activating Discord...")
    subprocess.run(['osascript', '-e', 'tell application "Discord" to activate'],
                   capture_output=True)
    time.sleep(1)

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
    btn_y = int(win['y'] + win['h'] * btn_y_frac) + 20  # +20px correction

    print(f"  Targeting Join Voice at ({btn_x}, {btn_y}) ...")
    pyautogui.moveTo(btn_x, btn_y, duration=0.4)
    time.sleep(0.2)
    pyautogui.click()
    print("  ✓ Clicked Join Voice.")

    # After joining, click Watch Stream
    click_watch_stream(win)

    # Pop the stream out into a floating PiP so it keeps playing when Discord is
    # minimized / buried — letting you run everything minimized.
    if pop_out:
        pop_out_stream()
    return True


def click_watch_stream(win=None):
    """Click the Watch Stream button using the same Quartz approach as Join Voice."""
    print("  Waiting for Watch Stream button to appear...")
    time.sleep(3)  # Discord takes a moment to show the stream panel

    if not win:
        win = get_discord_window()
    if not win:
        print("  ✗ Could not find Discord window via Quartz.")
        return False

    print(f"  Found: '{win['name']}'  {win['w']}x{win['h']} @ ({win['x']},{win['y']})")

    # "Watch Stream" appears as an overlay on the first video tile (top-left).
    # The tile grid starts just right of the sidebar; the first tile centre is
    # at sidebar + 1/6 of the remaining content width, ~26% down the window.
    sidebar_frac = 0.27
    content_w    = win['w'] * (1 - sidebar_frac)
    btn_x = int(win['x'] + win['w'] * sidebar_frac + content_w / 6)
    btn_y = int(win['y'] + win['h'] * 0.26)

    print(f"  Targeting Watch Stream at ({btn_x}, {btn_y}) ...")
    pyautogui.moveTo(btn_x, btn_y, duration=0.4)
    time.sleep(0.2)
    pyautogui.click()
    print("  ✓ Clicked Watch Stream.")
    return True


def pop_out_stream(win=None):
    """
    Detach the live stream into Discord's floating Picture-in-Picture window,
    then park it in the bottom-right corner.

    The PiP keeps rendering — and keeps playing audio into ScreenCaptureKit —
    regardless of the main Discord window being minimized or occluded, so
    transcription survives running Discord minimized. Best-effort: logs and
    returns False if it can't find the window.
    """
    print("  Popping out the stream...")
    time.sleep(2)  # let the stream finish loading after Watch Stream
    if not win:
        win = get_discord_window()
    if not win:
        print("  ✗ Could not find Discord window via Quartz.")
        return False

    sidebar_frac = 0.27
    content_w    = win['w'] * (1 - sidebar_frac)
    content_left = win['x'] + win['w'] * sidebar_frac

    # Hover over the stream centre to reveal the control bar, then click Pop Out.
    stream_cx = int(content_left + content_w / 2)
    stream_cy = int(win['y'] + win['h'] * 0.45)
    pyautogui.moveTo(stream_cx, stream_cy, duration=0.3)
    time.sleep(0.6)

    btn_x = int(content_left + content_w * POPOUT_X_FRAC) + 5   # +5px correction
    btn_y = int(win['y'] + win['h'] * POPOUT_Y_FRAC) + 15      # +15px correction
    print(f"  Targeting Pop Out at ({btn_x}, {btn_y}) ...")
    pyautogui.moveTo(btn_x, btn_y, duration=0.3)
    time.sleep(0.2)
    pyautogui.click()
    time.sleep(1.5)
    print("  ✓ Clicked Pop Out.")

    park_pip_corner()
    return True


def park_pip_corner():
    """
    Move the popped-out PiP into the bottom-right corner, out of the way.

    Guarded: only moves the frontmost Discord window if it's small (the PiP),
    so we never accidentally shove the main Discord window into a corner.
    Requires Accessibility permission (already needed for pyautogui).
    """
    try:
        chk = subprocess.run(
            ['osascript', '-e',
             'tell application "System Events" to tell process "Discord" '
             'to return size of window 1'],
            capture_output=True, text=True)
        dims = [int(s) for s in chk.stdout.strip().replace(' ', '').split(',') if s]
        if len(dims) != 2:
            print(f"  [park PiP skipped] couldn't read window size: {chk.stdout.strip()!r}")
            return
        w, h = dims
        if w >= 900 or h >= 700:
            print(f"  [park PiP skipped] frontmost window is {w}x{h} (looks like main, not PiP).")
            return
        sw, sh = pyautogui.size()
        x = max(0, sw - w - 20)
        y = max(0, sh - h - 60)
        subprocess.run(
            ['osascript', '-e',
             f'tell application "System Events" to tell process "Discord" '
             f'to set position of window 1 to {{{x}, {y}}}'],
            capture_output=True)
        print(f"  ✓ Parked PiP ({w}x{h}) at ({x}, {y}).")
    except Exception as e:
        print(f"  [park PiP skipped] {e}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel', default=MORNING_CHANNEL_ID,
                        help='Discord channel ID to join (default: morning channel)')
    parser.add_argument('--pop-out', dest='pop_out', action='store_true', default=True,
                        help='Pop the stream into a floating PiP (default: on)')
    parser.add_argument('--no-pop-out', dest='pop_out', action='store_false',
                        help='Leave the stream in the main Discord window')
    args = parser.parse_args()
    click_join_voice(channel_id=args.channel, pop_out=args.pop_out)
