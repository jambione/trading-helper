from __future__ import annotations
"""
workflows.py — Trading workflow actions for transcribe_action.py.
Imported by both the main script and test_workflows.py.

LIVE_MODE = False  → log only, no automation
LIVE_MODE = True   → real GUI automation
"""

import ctypes
import json
import os
import subprocess
import sys
import time as _time_mod

_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS   = sys.platform == "darwin"

# ========================= WATCHLIST TRACKER =========================
# Tickers already added to Webull are stored in a JSON file so the list
# survives restarts. Checked before every ADD_WB call to skip duplicates.

_WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "wb_watchlist.json")


def _load_watchlist() -> set:
    try:
        with open(_WATCHLIST_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_watchlist(watchlist: set):
    try:
        with open(_WATCHLIST_FILE, "w") as f:
            json.dump(sorted(watchlist), f, indent=2)
    except Exception as e:
        print(f"⚠️  Could not save watchlist: {e}")


_wb_watchlist: set = _load_watchlist()


def wb_watchlist_contains(ticker: str) -> bool:
    return ticker.upper() in _wb_watchlist


def wb_watchlist_add(ticker: str):
    _wb_watchlist.add(ticker.upper())
    _save_watchlist(_wb_watchlist)


def wb_watchlist_remove(ticker: str):
    _wb_watchlist.discard(ticker.upper())
    _save_watchlist(_wb_watchlist)


def wb_watchlist_clear():
    _wb_watchlist.clear()
    _save_watchlist(_wb_watchlist)


def wb_watchlist_show():
    if _wb_watchlist:
        print(f"📋 Webull watchlist ({len(_wb_watchlist)}): {', '.join(sorted(_wb_watchlist))}")
    else:
        print("📋 Webull watchlist is empty.")

# ── Win32 direct-message helpers ─────────────────────────────────────────────
# PostMessage sends keystrokes straight to a window's message queue by HWND,
# completely bypassing focus — nothing else on the desktop can be affected.
_PostMessage = ctypes.windll.user32.PostMessageW if _IS_WINDOWS else None

WM_KEYDOWN = 0x0100
WM_KEYUP   = 0x0101
WM_CHAR    = 0x0102
VK_RETURN  = 0x0D
VK_CONTROL = 0x11
VK_2       = 0x32   # virtual-key code for the '2' key


def _wb_post_char(hwnd: int, char: str):
    """Send a single character directly to a window handle via WM_CHAR."""
    _PostMessage(hwnd, WM_CHAR, ord(char.upper()), 0)


def _wb_post_ctrl2(hwnd: int):
    """Send Ctrl+2 directly to a window handle."""
    _PostMessage(hwnd, WM_KEYDOWN, VK_CONTROL, 0)
    _PostMessage(hwnd, WM_KEYDOWN, VK_2,       0)
    _PostMessage(hwnd, WM_KEYUP,   VK_2,       0)
    _PostMessage(hwnd, WM_KEYUP,   VK_CONTROL, 0)


def _wb_post_enter(hwnd: int):
    """Send Enter directly to a window handle."""
    _PostMessage(hwnd, WM_KEYDOWN, VK_RETURN, 0)
    _PostMessage(hwnd, WM_KEYUP,   VK_RETURN, 0)


# ── TradingView Win32 window helpers ─────────────────────────────────────────
# Use EnumWindows directly — pygetwindow can't reliably detect Store/UWP apps.

_SW_RESTORE = 9
if _IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t)

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _tv_enum_windows(fragment: str) -> list[tuple[int, str]]:
    """Scan all windows for fragment in title. Tries visible-only first, then all."""
    frag = fragment.lower()

    def _scan(require_visible: bool) -> list[tuple[int, str]]:
        found = []

        def cb(hwnd, _):
            if require_visible and not _user32.IsWindowVisible(hwnd):
                return 1
            n = _user32.GetWindowTextLengthW(hwnd)
            if n == 0:
                return 1
            buf = ctypes.create_unicode_buffer(n + 1)
            _user32.GetWindowTextW(hwnd, buf, n + 1)
            if frag in buf.value.lower():
                found.append((hwnd, buf.value))
            return 1

        proc = _EnumWindowsProc(cb)   # keep reference so GC doesn't free it
        _user32.EnumWindows(proc, 0)
        return found

    return _scan(True) or _scan(False)


def _tv_get_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = _RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _tv_focus(hwnd: int) -> bool:
    """Bring TradingView to the foreground using ctypes."""
    try:
        _user32.ShowWindow(hwnd, _SW_RESTORE)
        _time_mod.sleep(0.15)
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
        _time_mod.sleep(0.4)
        return True
    except Exception as e:
        print(f"⚠️  TV focus error: {e}")
        return False


def _tv_ensure_open() -> int | None:
    """
    Return the TradingView HWND (open + focused), launching if needed.
    Returns None on failure.
    """
    hit = _tv_enum_windows("TradingView")
    if hit:
        hwnd = hit[0][0]
        _tv_focus(hwnd)
        return hwnd

    print(f"   🚀 Launching TradingView ({_TV_AUMID})...")
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command",
             f'Start-Process "shell:appsFolder\\{_TV_AUMID}"'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"   ❌ TV launch failed: {e}")
        return None

    deadline = _time_mod.time() + LAUNCH_TIMEOUT
    while _time_mod.time() < deadline:
        hit = _tv_enum_windows("TradingView")
        if hit:
            hwnd = hit[0][0]
            _time_mod.sleep(2.5)   # let app fully load
            _tv_focus(hwnd)
            return hwnd
        _time_mod.sleep(0.5)

    print(f"   ❌ TradingView did not appear within {LAUNCH_TIMEOUT}s")
    return None


# ========================= CONFIG =========================

LIVE_MODE = _IS_WINDOWS  # GUI automation only available on Windows

# Window title fragments (used when LIVE_MODE is True)
TV_WINDOW = "TradingView"
WB_WINDOW = "Webull Desktop"

# App launch paths (used when LIVE_MODE is True)
WB_LAUNCH = r"C:\Program Files (x86)\Webull Desktop\Webull Desktop.exe"
_TV_AUMID = "TradingView.Desktop_n534cwy3pjxzj!TradingView.Desktop"

_TV_CANDIDATES = [
    r"C:\Program Files\WindowsApps\TradingView.Desktop_3.0.0.7652_x64__n534cwy3pjxzj\TradingView.exe",
    r"%LOCALAPPDATA%\TradingView\TradingView.exe",
    r"%LOCALAPPDATA%\Programs\TradingView\TradingView.exe",
    r"%PROGRAMFILES%\TradingView\TradingView.exe",
    r"%PROGRAMFILES(X86)%\TradingView\TradingView.exe",
    r"%APPDATA%\TradingView\TradingView.exe",
]
TV_LAUNCH = _TV_CANDIDATES[0]   # resolved at startup when LIVE_MODE is True

LAUNCH_TIMEOUT = 20


# ========================= LIVE MODE SETUP =========================
# Only import and resolve GUI dependencies when actually needed.

def _init_live():
    """Import GUI libraries and resolve TV exe path. Called once if LIVE_MODE=True."""
    global _pyautogui, _gw, _os, _subprocess, _time, TV_LAUNCH
    import os, subprocess
    import pyautogui as _pag
    import pygetwindow as _pgw
    _pyautogui  = _pag
    _gw         = _pgw
    _os         = os
    _subprocess = subprocess

    for candidate in _TV_CANDIDATES:
        expanded = os.path.expandvars(candidate)
        if os.path.isfile(expanded):
            TV_LAUNCH = expanded
            print(f"✅ TradingView found: {expanded}")
            return
    # Store app in WindowsApps — launched via PowerShell AUMID, no plain exe needed
    print(f"✅ TradingView: Store app ({_TV_AUMID})")


if LIVE_MODE:
    _init_live()


# ========================= WINDOW HELPERS (live only) =========================

def _find_window(title_fragment: str):
    try:
        wins = _gw.getWindowsWithTitle(title_fragment)
        return wins[0] if wins else None
    except Exception:
        return None


def _focus_window(title_fragment: str) -> bool:
    """
    Bring a window to the foreground using ctypes directly.
    Bypasses pygetwindow's activate() which misreads Win32 error code 0
    (operation succeeded) as a failure.
    """
    import time
    try:
        win = _find_window(title_fragment)
        if not win:
            return False
        hwnd = win._hWnd
        # Restore if minimised
        ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE = 9
        time.sleep(0.15)
        # Bring to foreground
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        ctypes.windll.user32.BringWindowToTop(hwnd)
        time.sleep(0.35)
        return True
    except Exception as e:
        print(f"⚠️  focus error for '{title_fragment}': {e}")
        return False


def _ensure_open(title_fragment: str, launch_path: str) -> bool:
    if _find_window(title_fragment):
        return _focus_window(title_fragment)

    print(f"   🚀 Launching '{title_fragment}'...")
    try:
        expanded = _os.path.expandvars(launch_path)
        if _os.path.isfile(expanded):
            # Direct exe launch
            _subprocess.Popen([expanded])
        elif "WindowsApps" in expanded:
            # Store app — PowerShell Start-Process with correct AUMID
            _subprocess.Popen(
                ["powershell", "-NoProfile", "-Command",
                 f'Start-Process "shell:appsFolder\\{_TV_AUMID}"'],
                stdout=_subprocess.DEVNULL,
                stderr=_subprocess.DEVNULL,
            )
        else:
            _subprocess.Popen(expanded, shell=True)
    except Exception as e:
        print(f"   ❌ Launch failed: {e}")
        return False

    import time
    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline:
        if _find_window(title_fragment):
            time.sleep(1.2)
            return _focus_window(title_fragment)
        time.sleep(0.5)

    print(f"   ❌ '{title_fragment}' did not appear within {LAUNCH_TIMEOUT}s")
    return False


def find_window_title(title_fragment: str) -> str | None:
    """Public helper used by test_workflows.py preflight check."""
    if not LIVE_MODE:
        return None
    win = _find_window(title_fragment)
    return win.title if win else None


# ========================= WORKFLOWS =========================

def _wb_add_one(hwnd: int, win, ticker: str) -> bool:
    """
    Type one ticker into the already-focused Webull window and confirm with Enter.
    Caller is responsible for opening/focusing the window first.
    """
    import time

    # Ctrl+2 → Stocks tab
    _pyautogui.hotkey("ctrl", "2")
    time.sleep(0.5)

    # Type each character directly into the window's message queue, then confirm
    for letter in ticker:
        _wb_post_char(hwnd, letter)
        time.sleep(0.05)
    time.sleep(0.5)
    _wb_post_enter(hwnd)
    print(f"   ✅ ADD_WB done for {ticker}")
    return True


def workflow_add_wb(ticker: str, dry_run: bool = False) -> bool:
    """
    Open/focus Webull Desktop, switch to the Stocks tab (Ctrl+2),
    type the ticker letter-by-letter directly into the window's message queue
    (bypasses focus — no other window can be accidentally typed into),
    wait for the search dropdown to appear, then send Enter.
    """
    ticker = ticker.upper()

    if not LIVE_MODE or dry_run:
        print(f"📊 [LOG] ADD_WB → {ticker}")
        wb_watchlist_add(ticker)
        return True

    print(f"📊 ADD_WB → {ticker}")
    if not _ensure_open(WB_WINDOW, WB_LAUNCH):
        print("   ❌ ADD_WB failed — could not open Webull Desktop")
        return False

    import time
    win  = _find_window(WB_WINDOW)
    hwnd = win._hWnd
    time.sleep(0.4)

    _wb_add_one(hwnd, win, ticker)
    wb_watchlist_add(ticker)
    return True


def workflow_add_wb_bulk(tickers: list, dry_run: bool = False) -> dict:
    """
    Add multiple tickers to Webull watchlist sequentially in a single session.
    Opens/focuses Webull once, then processes each ticker one at a time so
    keystrokes never interleave. Returns counts of added/skipped/failed tickers.
    """
    results: dict = {"added": [], "skipped": [], "failed": []}
    tickers = [t.upper() for t in tickers]

    to_add           = [t for t in tickers if not wb_watchlist_contains(t)]
    results["skipped"] = [t for t in tickers if wb_watchlist_contains(t)]

    if not to_add:
        return results

    if not LIVE_MODE or dry_run:
        for t in to_add:
            print(f"📊 [LOG] ADD_WB → {t}")
            wb_watchlist_add(t)
            results["added"].append(t)
        return results

    if not _ensure_open(WB_WINDOW, WB_LAUNCH):
        print("   ❌ ADD_WB_BULK failed — could not open Webull Desktop")
        results["failed"] = to_add
        return results

    import time
    win = _find_window(WB_WINDOW)
    if not win:
        results["failed"] = to_add
        return results

    hwnd = win._hWnd
    time.sleep(0.4)

    for i, ticker in enumerate(to_add, 1):
        print(f"📊 ADD_WB → {ticker} ({i}/{len(to_add)})")
        try:
            _wb_add_one(hwnd, win, ticker)
            wb_watchlist_add(ticker)
            results["added"].append(ticker)
            time.sleep(0.3)   # brief pause between tickers
        except Exception as e:
            print(f"   ❌ ADD_WB failed for {ticker}: {e}")
            results["failed"].append(ticker)

    return results


def workflow_add_tv(ticker: str, dry_run: bool = False) -> bool:
    """
    Open/focus TradingView, type the ticker letter-by-letter to open the search
    overlay, wait for the dropdown, confirm with Enter, then Alt+W to add to
    the watchlist.
    """
    ticker = ticker.upper()

    if not LIVE_MODE or dry_run:
        print(f"👁️  [LOG] ADD_TV → {ticker}")
        return True

    print(f"👁️  ADD_TV → {ticker}")

    import numpy as np
    import pyautogui as _pag

    hwnd = _tv_ensure_open()
    if not hwnd:
        print("   ❌ ADD_TV failed — could not open TradingView")
        return False

    _time_mod.sleep(0.5)   # let click-focus settle

    # Capture baseline for dropdown detection
    left, top, w, h = _tv_get_rect(hwnd)
    margin = 10
    region = (left + margin, top + 40, w - margin * 2, min(300, h // 3))
    try:
        baseline = np.array(_pag.screenshot(region=region), dtype=np.int16)
    except Exception:
        baseline = None

    # Type each letter — window is focused so pyautogui reaches it directly
    for letter in ticker:
        _pag.press(letter.lower())
        _time_mod.sleep(0.1)

    # Poll for search dropdown (pixel-delta detection)
    detected = False
    if baseline is not None:
        deadline = _time_mod.time() + 5.0
        while _time_mod.time() < deadline:
            _time_mod.sleep(0.1)
            try:
                current = np.array(_pag.screenshot(region=region), dtype=np.int16)
                if np.abs(current - baseline).mean() >= 3.0:
                    detected = True
                    break
            except Exception:
                break

    _time_mod.sleep(0.2 if detected else 0.5)

    # Confirm selection and wait for chart to load
    _pag.press("enter")
    _time_mod.sleep(2.0)

    # Add to TradingView watchlist
    _pag.hotkey("alt", "w")

    print(f"   ✅ ADD_TV done for {ticker}" + ("" if detected else " (dropdown timeout — Enter sent anyway)"))
    return True


def _find_brave_window() -> int | None:
    """Return HWND of the first visible Brave browser window, or None."""
    hwnd_found = []

    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls, 256)
        if cls.value == "Chrome_WidgetWin_1":
            title = ctypes.create_unicode_buffer(512)
            _user32.GetWindowTextW(hwnd, title, 512)
            if "Brave" in title.value and _user32.IsWindowVisible(hwnd):
                hwnd_found.append(hwnd)
                return 0  # stop enumeration
        return 1

    proc = _EnumWindowsProc(cb)
    _user32.EnumWindows(proc, 0)
    return hwnd_found[0] if hwnd_found else None


def workflow_add_brave_tv(ticker: str, cfg: dict = None) -> bool:
    """
    Focus Brave, switch to the pinned TradingView tab, click the page body to
    grab focus, type the ticker (opens TV symbol search), confirm with Enter,
    then Alt+W to add to the watchlist.
    """
    import pyautogui as _pag

    ticker = ticker.upper()
    cfg = cfg or {}
    tab_num = str(int(cfg.get("brave_tv_tab", 1)))

    if not LIVE_MODE:
        print(f"🌐 [LOG] ADD_BRAVE_TV → {ticker}")
        return True

    print(f"🌐 ADD_BRAVE_TV → {ticker}")
    hwnd = _find_brave_window()
    if not hwnd:
        print("   ❌ ADD_BRAVE_TV failed — Brave browser window not found")
        return False

    # Bring Brave to front without resizing — SW_RESTORE (9) would un-maximize it,
    # so use SW_SHOW (5) unless the window is actually minimized (IsIconic).
    _SW_SHOW = 5
    _user32.ShowWindow(hwnd, _SW_RESTORE if _user32.IsIconic(hwnd) else _SW_SHOW)
    _time_mod.sleep(0.15)
    _user32.SetForegroundWindow(hwnd)
    _user32.BringWindowToTop(hwnd)
    _time_mod.sleep(0.4)

    # Switch to the pinned TradingView tab
    _pag.hotkey("ctrl", tab_num)
    _time_mod.sleep(0.4)

    # Click center of the window to focus the page body (chart area)
    left, top, w, h = _tv_get_rect(hwnd)
    _pag.click(left + w // 2, top + h // 2)
    _time_mod.sleep(0.2)

    # Type ticker — TradingView opens symbol search on any keypress in the chart
    for letter in ticker:
        _pag.press(letter.lower())
        _time_mod.sleep(0.05)

    _time_mod.sleep(0.3)
    _pag.press("enter")
    _time_mod.sleep(0.2)

    # Add to TradingView watchlist
    _pag.hotkey("alt", "w")

    print(f"   ✅ ADD_BRAVE_TV done for {ticker}")
    return True


def workflow_add_wb_and_tv(ticker: str, cfg: dict = None) -> bool:
    """Add to Webull watchlist, then load ticker in Brave TradingView."""
    wb_ok = workflow_add_wb(ticker)
    tv_ok = workflow_add_brave_tv(ticker, cfg)
    return wb_ok and tv_ok


def workflow_buy(ticker: str, dry_run: bool = False) -> bool:
    if not LIVE_MODE or dry_run:
        print(f"💸 [LOG] BUY → {ticker}")
        return True

    print(f"💸 BUY → {ticker}")
    if _ensure_open(WB_WINDOW, WB_LAUNCH):
        # import time
        # time.sleep(0.4)
        # _pyautogui.hotkey("shift", "b")
        print(f"   ✅ BUY (Shift+B) sent for {ticker}")
        return True
    print("   ❌ BUY failed — could not open Webull Desktop")
    return False


def workflow_sell_all(ticker: str, dry_run: bool = False) -> bool:
    if not LIVE_MODE or dry_run:
        print(f"🔴 [LOG] SELL ALL → {ticker}")
        return True

    print(f"🔴 SELL ALL → {ticker}")
    if _ensure_open(WB_WINDOW, WB_LAUNCH):
        # import time
        # time.sleep(0.4)
        # _pyautogui.hotkey("shift", "a")
        print(f"   ✅ SELL ALL (Shift+A) sent for {ticker}")
        return True
    print("   ❌ SELL failed — could not open Webull Desktop")
    return False


# ── TradingView alert creation ────────────────────────────────────────────────

def _macos_create_tv_alert(ticker: str, cfg: dict) -> bool:
    """Navigate TradingView to ticker and create a squeeze alert on macOS.

    Uses AppleScript to activate the browser, pyautogui to type the ticker
    and trigger the Create Alert dialog (Option+A), then confirms with Enter.

    Prerequisite: create one alert manually first so TradingView pre-fills
    the webhook URL and message for all subsequent Option+A dialogs.
    """
    try:
        import pyautogui as _pag
    except ImportError:
        print("   ❌ CREATE_TV_ALERT: pyautogui not installed — run: pip install pyautogui")
        return False

    import subprocess

    tab_num = str(int(cfg.get("brave_tv_tab", 1)))
    browser = cfg.get("tv_browser_macos", "Brave Browser")

    # Activate browser
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "{browser}" to activate'],
            check=True, capture_output=True, timeout=5,
        )
    except Exception as e:
        print(f"   ❌ CREATE_TV_ALERT: could not activate {browser}: {e}")
        return False

    _time_mod.sleep(0.4)

    # Switch to pinned TradingView tab
    _pag.hotkey("ctrl", tab_num)
    _time_mod.sleep(0.4)

    # Click center of screen to focus the chart area
    sw, sh = _pag.size()
    _pag.click(sw // 2, sh // 2)
    _time_mod.sleep(0.2)

    # Type ticker — TradingView opens symbol search on any keypress in the chart
    for letter in ticker:
        _pag.press(letter.lower())
        _time_mod.sleep(0.05)
    _time_mod.sleep(0.3)
    _pag.press("enter")
    _time_mod.sleep(2.5)   # wait for chart to load

    # Open Create Alert dialog: Option+A (Alt+A on macOS via pyautogui)
    _pag.hotkey("alt", "a")
    _time_mod.sleep(1.5)   # wait for dialog to open

    # Confirm — pre-filled webhook URL and message are reused automatically
    _pag.press("enter")
    _time_mod.sleep(0.3)

    print(f"   ✅ CREATE_TV_ALERT done for {ticker}")
    return True


def workflow_create_tv_alert(ticker: str, cfg: dict = None) -> bool:
    """Navigate TradingView to ticker and create a squeeze alert using Alt/Option+A.

    The alert dialog opens with the last-used settings pre-filled (webhook URL
    and message), so a single Enter keystroke is all that's needed after the
    first manual alert is set up.

    Works on macOS (pyautogui + AppleScript) and Windows (pyautogui + Win32).
    On other platforms, or when neither pyautogui nor a browser is available,
    logs the intent and returns False.
    """
    ticker = ticker.upper()
    cfg    = cfg or {}

    if _IS_MACOS:
        print(f"📡 CREATE_TV_ALERT → {ticker}")
        return _macos_create_tv_alert(ticker, cfg)

    if not LIVE_MODE:
        print(f"📡 [LOG] CREATE_TV_ALERT → {ticker}")
        return True

    # Windows path — reuse Brave window focus
    import pyautogui as _pag

    tab_num = str(int(cfg.get("brave_tv_tab", 1)))

    hwnd = _find_brave_window()
    if not hwnd:
        print("   ❌ CREATE_TV_ALERT: Brave browser window not found")
        return False

    print(f"📡 CREATE_TV_ALERT → {ticker}")

    _SW_SHOW = 5
    _user32.ShowWindow(hwnd, _SW_RESTORE if _user32.IsIconic(hwnd) else _SW_SHOW)
    _time_mod.sleep(0.15)
    _user32.SetForegroundWindow(hwnd)
    _user32.BringWindowToTop(hwnd)
    _time_mod.sleep(0.4)

    _pag.hotkey("ctrl", tab_num)
    _time_mod.sleep(0.4)

    # Click center of window to focus chart
    left, top, w, h = _tv_get_rect(hwnd)
    _pag.click(left + w // 2, top + h // 2)
    _time_mod.sleep(0.2)

    # Navigate to ticker
    for letter in ticker:
        _pag.press(letter.lower())
        _time_mod.sleep(0.05)
    _time_mod.sleep(0.3)
    _pag.press("enter")
    _time_mod.sleep(2.5)

    # Open Create Alert dialog
    _pag.hotkey("alt", "a")
    _time_mod.sleep(1.5)

    # Confirm with pre-filled settings
    _pag.press("enter")
    _time_mod.sleep(0.3)

    print(f"   ✅ CREATE_TV_ALERT done for {ticker}")
    return True