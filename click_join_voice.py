#!/usr/bin/env python3
"""
click_join_voice.py
Finds and clicks the Discord "Join Voice" button using macOS Vision OCR.
Reads actual text rendered on screen — no coordinates or templates needed.
"""
import subprocess, sys, time, os, tempfile
import pyautogui

pyautogui.FAILSAFE = False

def ocr_find_text(search_text):
    """Use macOS Vision framework (built-in) to find text on screen and return (x, y)."""
    script = f'''
import Vision, Quartz, objc, sys
from Foundation import NSURL
from PIL import ImageGrab
import tempfile, os

# Take screenshot
img = ImageGrab.grab()
tmp = tempfile.mktemp(suffix=".png")
img.save(tmp)
W, H = img.size

url   = NSURL.fileURLWithPath_(tmp)
src   = Quartz.CGImageSourceCreateWithURL(url, None)
cgimg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)

hits = []

def handler(req, err):
    for obs in req.results():
        for cand in obs.topCandidates_(1):
            if "{search_text}".lower() in cand.string().lower():
                bb = obs.boundingBox()  # normalized, origin bottom-left
                cx = int((bb.origin.x + bb.size.width  / 2) * W)
                cy = int((1 - bb.origin.y - bb.size.height / 2) * H)
                hits.append((cx, cy))

req = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(handler)
req.setRecognitionLevel_(1)
h   = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimg, {{}})
h.performRequests_error_([req], None)
os.unlink(tmp)

if hits:
    print(hits[0][0], hits[0][1])
else:
    print("NOT_FOUND")
'''
    result = subprocess.run(
        ['python3', '-c', script],
        capture_output=True, text=True
    )
    output = result.stdout.strip()
    if output and output != 'NOT_FOUND':
        parts = output.split()
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    return None

def click_join_voice():
    print("  Activating Discord...")
    subprocess.run(['osascript', '-e', 'tell application "Discord" to activate'],
                   capture_output=True)
    time.sleep(2)

    # ── Strategy 1: macOS Vision OCR ──────────────────────────────────────────
    print("  Searching screen for 'Join Voice' text (Vision OCR)...")
    hit = ocr_find_text("Join Voice")
    if hit:
        x, y = hit
        print(f"  Found at ({x}, {y}) — clicking...")
        pyautogui.moveTo(x, y, duration=0.3)
        pyautogui.click()
        print("  ✓ Clicked.")
        return True

    # ── Strategy 2: window-relative coordinates ────────────────────────────────
    print("  OCR failed — trying window-relative coordinates...")
    bounds_script = '''
    tell application "System Events"
        tell process "Discord"
            set b to bounds of front window
            return ((item 1 of b) as string) & "," & ((item 2 of b) as string) & "," & ((item 3 of b) as string) & "," & ((item 4 of b) as string)
        end tell
    end tell
    '''
    res = subprocess.run(['osascript', '-e', bounds_script],
                         capture_output=True, text=True)
    if res.returncode == 0:
        parts = [int(v.strip()) for v in res.stdout.strip().split(',')]
        left, top, right, bottom = parts
        w = right - left
        h = bottom - top
        sidebar = w * 0.27
        cx = int(left + sidebar + (w - sidebar) / 2)
        cy = int(top  + h * 0.72)
        print(f"  Window {w}x{h} → clicking ({cx},{cy})...")
        pyautogui.moveTo(cx, cy, duration=0.3)
        pyautogui.click()
        print("  ✓ Clicked.")
        return True

    print("  ✗ Could not locate Join Voice button.")
    return False

if __name__ == '__main__':
    ok = click_join_voice()
    sys.exit(0 if ok else 1)
