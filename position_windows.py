"""Save/restore the monitor console windows' size and position.

    python position_windows.py save     capture the current layout
    python position_windows.py          restore saved layout (or defaults)

Workflow: launch the monitors, drag/resize them how you like, then run
save (or double-click save_layout.bat). Every launch after that puts
them back exactly there.
"""
import ctypes
import json
import sys
from pathlib import Path

TITLES = ["Trading Monitors",          # combined single window
          "Alpaca Flow Monitor", "Trading Monitors", "TradingView Monitor"]
LAYOUT = Path(__file__).parent / "monitor_layout.json"
DEFAULTS = {"Trading Monitors": {"x": 20, "y": 20},
            "Alpaca Flow Monitor": {"x": 20, "y": 20},
            "TradingView Monitor": {"x": 20, "y": 560}}

if sys.platform != "win32":
    sys.exit(0)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from ctypes import wintypes  # noqa: E402

user32 = ctypes.windll.user32
SWP_NOSIZE, SWP_NOZORDER = 0x0001, 0x0004


def find_windows() -> dict:
    found = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        for key in TITLES:
            if key in buf.value and key not in found:
                found[key] = hwnd
        return True

    user32.EnumWindows(cb, 0)
    return found


def rect_of(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return {"x": r.left, "y": r.top,
            "w": r.right - r.left, "h": r.bottom - r.top}


def save():
    found = find_windows()
    if not found:
        print("no monitor windows found - launch them first, then save")
        return
    layout = {k: rect_of(h) for k, h in found.items()}
    LAYOUT.write_text(json.dumps(layout, indent=2))
    for k, r in layout.items():
        print(f"saved: {k} -> {r}")
    print(f"layout written to {LAYOUT.name}; every launch now restores it")


def restore():
    layout = (json.loads(LAYOUT.read_text()) if LAYOUT.exists()
              else DEFAULTS)
    found = find_windows()
    for key, hwnd in found.items():
        r = layout.get(key)
        if not r:
            continue
        if "w" in r and "h" in r:
            user32.SetWindowPos(hwnd, 0, r["x"], r["y"], r["w"], r["h"],
                                SWP_NOZORDER)
        else:
            user32.SetWindowPos(hwnd, 0, r["x"], r["y"], 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER)
        print(f"positioned: {key} -> {r}")
    if not found:
        print("no monitor windows found to position")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower() == "save":
        save()
    else:
        restore()
