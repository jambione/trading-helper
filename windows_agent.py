"""
windows_agent.py — Local Windows agent for Webull + TradingView automation.

Run this on your Windows machine:
    python windows_agent.py   (or double-click windows_agent.bat)

Listens on http://localhost:8889
The trading dashboard's Add button POSTs here to automate Webull Desktop
and TradingView (Brave) locally. Self-contained — no other files needed.

Endpoints:
    POST /add-wb        {"ticker": "NVDA"}   → add to Webull Desktop watchlist
    POST /add-tv        {"ticker": "NVDA"}   → switch to pinned TV tab, load + Alt+W
    GET  /health        → {"ok": true, "platform": "win32"}

Config (environment variables):
    BRAVE_TV_TAB    Ctrl+N tab number for the pinned TradingView tab (default: 1)
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

# ── Platform ──────────────────────────────────────────────────────────────────
_IS_WINDOWS = sys.platform == "win32"

# ── Config ────────────────────────────────────────────────────────────────────
WB_WINDOW      = "Webull Desktop"
WB_LAUNCH      = r"C:\Program Files (x86)\Webull Desktop\Webull Desktop.exe"
LAUNCH_TIMEOUT = 20
BRAVE_TV_TAB   = int(os.environ.get("BRAVE_TV_TAB", "1"))  # Ctrl+N to switch tab

# ── Win32 setup ───────────────────────────────────────────────────────────────
WM_KEYDOWN = 0x0100
WM_KEYUP   = 0x0101
WM_CHAR    = 0x0102
VK_RETURN  = 0x0D
VK_CONTROL = 0x11
VK_2       = 0x32
_SW_RESTORE = 9
_SW_SHOW    = 5

if _IS_WINDOWS:
    _user32      = ctypes.windll.user32
    _PostMessage = ctypes.windll.user32.PostMessageW
    _EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t)

    class _RECT(ctypes.Structure):
        _fields_ = [("left",  ctypes.c_long), ("top",    ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    import pyautogui  as _pag
    import pygetwindow as _gw


# ── Shared window helpers ─────────────────────────────────────────────────────

def _find_window(title_fragment: str):
    """Return first pygetwindow window whose title contains fragment."""
    try:
        wins = _gw.getWindowsWithTitle(title_fragment)
        return wins[0] if wins else None
    except Exception:
        return None


def _focus_window(title_fragment: str) -> bool:
    """Bring a window to the foreground."""
    try:
        win = _find_window(title_fragment)
        if not win:
            return False
        hwnd = win._hWnd
        _user32.ShowWindow(hwnd, _SW_RESTORE)
        time.sleep(0.15)
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
        time.sleep(0.35)
        return True
    except Exception as e:
        print(f"  ⚠️  focus error: {e}")
        return False


def _ensure_open(title_fragment: str, launch_path: str) -> bool:
    """Open and focus a window, launching the exe if not already running."""
    if _find_window(title_fragment):
        return _focus_window(title_fragment)

    print(f"  🚀 Launching '{title_fragment}'...")
    try:
        expanded = os.path.expandvars(launch_path)
        if os.path.isfile(expanded):
            subprocess.Popen([expanded])
        else:
            subprocess.Popen(launch_path, shell=True)
    except Exception as e:
        print(f"  ❌ Launch failed: {e}")
        return False

    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline:
        if _find_window(title_fragment):
            time.sleep(1.2)
            return _focus_window(title_fragment)
        time.sleep(0.5)

    print(f"  ❌ '{title_fragment}' did not appear within {LAUNCH_TIMEOUT}s")
    return False


def _get_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = _RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


# ── Webull: PostMessage helpers (no focus steal) ──────────────────────────────

def _post_char(hwnd: int, char: str):
    _PostMessage(hwnd, WM_CHAR, ord(char.upper()), 0)

def _post_ctrl2(hwnd: int):
    _PostMessage(hwnd, WM_KEYDOWN, VK_CONTROL, 0)
    _PostMessage(hwnd, WM_KEYDOWN, VK_2,       0)
    _PostMessage(hwnd, WM_KEYUP,   VK_2,       0)
    _PostMessage(hwnd, WM_KEYUP,   VK_CONTROL, 0)

def _post_enter(hwnd: int):
    _PostMessage(hwnd, WM_KEYDOWN, VK_RETURN, 0)
    _PostMessage(hwnd, WM_KEYUP,   VK_RETURN, 0)


# ── Webull workflow ───────────────────────────────────────────────────────────

def workflow_add_wb(ticker: str) -> bool:
    """Open/focus Webull Desktop, switch to Stocks tab, type ticker, Enter."""
    ticker = ticker.upper().strip()

    if not _IS_WINDOWS:
        print(f"  [DRY RUN] ADD_WB → {ticker}")
        return True

    print(f"  📊 ADD_WB → {ticker}")
    if not _ensure_open(WB_WINDOW, WB_LAUNCH):
        print("  ❌ ADD_WB failed — could not open Webull Desktop")
        return False

    win = _find_window(WB_WINDOW)
    if not win:
        return False

    hwnd = win._hWnd
    time.sleep(0.4)

    # Ctrl+2 → Stocks tab
    _pag.hotkey("ctrl", "2")
    time.sleep(0.5)

    # Type ticker letter-by-letter directly into Webull's message queue
    for letter in ticker:
        _post_char(hwnd, letter)
        time.sleep(0.05)

    time.sleep(0.5)
    _post_enter(hwnd)
    print(f"  ✅ ADD_WB done: {ticker}")
    return True


# ── TradingView: find browser window (Brave or Chrome) ───────────────────────

def _find_browser_hwnd() -> tuple[int | None, str]:
    """
    Return (HWND, browser_name) of the first visible Brave or Chrome window.
    Checks window class Chrome_WidgetWin_1 — shared by both browsers.
    Brave windows end with '- Brave'; Chrome windows end with '- Google Chrome'.
    Falls back to any visible Chrome_WidgetWin_1 window if neither matches.
    """
    brave_hwnd  = None
    chrome_hwnd = None
    any_hwnd    = None

    def cb(hwnd, _):
        nonlocal brave_hwnd, chrome_hwnd, any_hwnd
        if not _user32.IsWindowVisible(hwnd):
            return 1
        cls = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls, 256)
        if cls.value != "Chrome_WidgetWin_1":
            return 1
        n = _user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return 1
        title = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, title, n + 1)
        t = title.value
        if any_hwnd is None:
            any_hwnd = hwnd
        if "Brave" in t and brave_hwnd is None:
            brave_hwnd = hwnd
        if "Google Chrome" in t and chrome_hwnd is None:
            chrome_hwnd = hwnd
        return 1

    proc = _EnumWindowsProc(cb)
    _user32.EnumWindows(proc, 0)

    if brave_hwnd:
        return brave_hwnd, "Brave"
    if chrome_hwnd:
        return chrome_hwnd, "Chrome"
    if any_hwnd:
        return any_hwnd, "browser"
    return None, ""


# ── TradingView workflow ──────────────────────────────────────────────────────

def workflow_add_tv(ticker: str, tab_num: int = BRAVE_TV_TAB) -> bool:
    """
    Focus Brave/Chrome, switch to the pinned TradingView tab (Ctrl+tab_num),
    click the chart area, type the ticker, press Enter to load the symbol,
    then Alt+W to add to the TradingView watchlist.
    """
    ticker = ticker.upper().strip()

    if not _IS_WINDOWS:
        print(f"  [DRY RUN] ADD_TV → {ticker}")
        return True

    print(f"  🌐 ADD_TV → {ticker}")
    hwnd, browser = _find_browser_hwnd()
    if not hwnd:
        print("  ❌ ADD_TV failed — no Brave/Chrome window found")
        return False

    print(f"  Found: {browser}")

    # Bring browser to front
    _user32.ShowWindow(hwnd, _SW_RESTORE if _user32.IsIconic(hwnd) else _SW_SHOW)
    time.sleep(0.2)
    _user32.SetForegroundWindow(hwnd)
    _user32.BringWindowToTop(hwnd)
    time.sleep(0.6)   # wait for window to be fully focused

    # Switch to pinned TradingView tab
    _pag.hotkey("ctrl", str(tab_num))
    time.sleep(0.6)   # wait for tab to switch

    # Ctrl+1 leaves the URL bar focused — press Escape to return focus to the page
    _pag.press("escape")
    time.sleep(0.2)

    # Click in the content area (skip the browser toolbar ~90px from top)
    left, top, w, h = _get_rect(hwnd)
    toolbar_h = 90
    _pag.click(left + w // 2, top + toolbar_h + (h - toolbar_h) // 2)
    time.sleep(0.3)

    # Type ticker — TradingView opens symbol search on any keypress
    for letter in ticker:
        _pag.press(letter.lower())
        time.sleep(0.05)

    time.sleep(0.4)
    _pag.press("enter")      # confirm symbol
    time.sleep(0.3)

    _pag.hotkey("alt", "w")  # add to TradingView watchlist

    print(f"  ✅ ADD_TV done: {ticker}")
    return True


# ── HTTP handler ──────────────────────────────────────────────────────────────

class AgentHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Allow Private Network Access from remote HTTPS origins (required for browser security policy)
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {
                "ok":          True,
                "platform":    sys.platform,
                "agent":       "windows_agent",
                "brave_tv_tab": BRAVE_TV_TAB,
            })
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return

        ticker = data.get("ticker", "").strip().upper()

        if path in ("/add-wb", "/add-tv"):
            if not ticker:
                self._json(400, {"error": "missing ticker"})
                return
            if path == "/add-wb":
                ok = workflow_add_wb(ticker)
            else:
                ok = workflow_add_tv(ticker)
            self._json(200 if ok else 500, {"ok": ok, "ticker": ticker})

        else:
            self._json(404, {"error": "not found"})

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)


# ── Entry point ───────────────────────────────────────────────────────────────

PORT = 8889

if __name__ == "__main__":
    if not _IS_WINDOWS:
        print("⚠️  WARNING: Not running on Windows — automation will be dry-run only.")
    print(f"  Brave TradingView tab: Ctrl+{BRAVE_TV_TAB}  (set BRAVE_TV_TAB env var to change)")
    server = HTTPServer(("127.0.0.1", PORT), AgentHandler)
    print(f"✅ Windows local agent running on http://localhost:{PORT}")
    print(f"   POST /add-wb  {{\"ticker\": \"NVDA\"}}  → Webull Desktop")
    print(f"   POST /add-tv  {{\"ticker\": \"NVDA\"}}  → TradingView (Brave, Ctrl+{BRAVE_TV_TAB}, Alt+W)")
    print(f"   GET  /health  → status")
    print(f"   Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
