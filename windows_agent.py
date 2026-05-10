"""
windows_agent.py — Local Windows agent for Webull Desktop automation.

Run this on your Windows machine:
    python windows_agent.py

Listens on http://localhost:8889
The trading dashboard's Add button will POST to this agent to automate
Webull Desktop locally — no server-side automation needed.

Endpoints:
    POST /add-wb        {"ticker": "NVDA"}   → add to Webull watchlist
    GET  /health        → {"ok": true, "platform": "win32"}
"""

from __future__ import annotations

import ctypes
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

# ── Platform guard ────────────────────────────────────────────────────────────
_IS_WINDOWS = sys.platform == "win32"

# ── Win32 constants ───────────────────────────────────────────────────────────
WM_KEYDOWN = 0x0100
WM_KEYUP   = 0x0101
WM_CHAR    = 0x0102
VK_RETURN  = 0x0D
VK_CONTROL = 0x11
VK_2       = 0x32

WB_WINDOW  = "Webull Desktop"
WB_LAUNCH  = r"C:\Program Files (x86)\Webull Desktop\Webull Desktop.exe"
LAUNCH_TIMEOUT = 20

if _IS_WINDOWS:
    _user32      = ctypes.windll.user32
    _PostMessage = ctypes.windll.user32.PostMessageW

    _EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t
    )


# ── Window helpers ─────────────────────────────────────────────────────────────

def _find_window(fragment: str) -> int | None:
    """Return HWND of first visible window whose title contains fragment."""
    if not _IS_WINDOWS:
        return None
    frag  = fragment.lower()
    found = []

    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return 1
        n = _user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return 1
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        if frag in buf.value.lower():
            found.append(hwnd)
            return 0  # stop
        return 1

    proc = _EnumWindowsProc(cb)
    _user32.EnumWindows(proc, 0)
    return found[0] if found else None


def _focus_window(hwnd: int):
    _user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    time.sleep(0.15)
    _user32.SetForegroundWindow(hwnd)
    _user32.BringWindowToTop(hwnd)
    time.sleep(0.35)


def _ensure_webull_open() -> int | None:
    """Return focused Webull HWND, launching if needed."""
    hwnd = _find_window(WB_WINDOW)
    if hwnd:
        _focus_window(hwnd)
        return hwnd

    print(f"  🚀 Launching Webull Desktop...")
    import subprocess, os
    try:
        path = os.path.expandvars(WB_LAUNCH)
        if os.path.isfile(path):
            subprocess.Popen([path])
        else:
            subprocess.Popen(WB_LAUNCH, shell=True)
    except Exception as e:
        print(f"  ❌ Launch failed: {e}")
        return None

    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline:
        hwnd = _find_window(WB_WINDOW)
        if hwnd:
            time.sleep(1.5)
            _focus_window(hwnd)
            return hwnd
        time.sleep(0.5)

    print("  ❌ Webull did not appear — timed out")
    return None


# ── Keystroke helpers ─────────────────────────────────────────────────────────

def _post_char(hwnd: int, char: str):
    _PostMessage(hwnd, WM_CHAR, ord(char.upper()), 0)

def _post_ctrl2(hwnd: int):
    _PostMessage(hwnd, WM_KEYDOWN, VK_CONTROL, 0)
    _PostMessage(hwnd, WM_KEYDOWN, VK_2, 0)
    _PostMessage(hwnd, WM_KEYUP,   VK_2, 0)
    _PostMessage(hwnd, WM_KEYUP,   VK_CONTROL, 0)

def _post_enter(hwnd: int):
    _PostMessage(hwnd, WM_KEYDOWN, VK_RETURN, 0)
    _PostMessage(hwnd, WM_KEYUP,   VK_RETURN, 0)


# ── Core automation ───────────────────────────────────────────────────────────

def add_to_webull(ticker: str) -> tuple[bool, str]:
    """
    Add ticker to Webull Desktop watchlist.
    Returns (success, message).
    """
    ticker = ticker.upper().strip()

    if not ticker or not ticker.isalpha() or len(ticker) > 8:
        return False, f"Invalid ticker: {ticker!r}"

    if not _IS_WINDOWS:
        print(f"  [DRY RUN] Would add {ticker} to Webull (not Windows)")
        return True, f"dry-run: {ticker}"

    hwnd = _ensure_webull_open()
    if not hwnd:
        return False, "Could not open Webull Desktop"

    time.sleep(0.4)

    # Ctrl+2 → Stocks tab
    _post_ctrl2(hwnd)
    time.sleep(0.5)

    # Type ticker letter-by-letter directly into message queue
    for letter in ticker:
        _post_char(hwnd, letter)
        time.sleep(0.05)

    time.sleep(0.5)
    _post_enter(hwnd)

    print(f"  ✅ Added {ticker} to Webull")
    return True, f"added: {ticker}"


# ── HTTP handler ──────────────────────────────────────────────────────────────

class AgentHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {"ok": True, "platform": sys.platform, "agent": "windows_agent"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        # Read body
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return

        if path == "/add-wb":
            ticker = data.get("ticker", "").strip().upper()
            if not ticker:
                self._json(400, {"error": "missing ticker"})
                return
            ok, msg = add_to_webull(ticker)
            self._json(200 if ok else 500, {"ok": ok, "msg": msg, "ticker": ticker})

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
        print("⚠️  WARNING: Not running on Windows — Webull automation will be dry-run only.")
    server = HTTPServer(("127.0.0.1", PORT), AgentHandler)
    print(f"✅ Windows local agent running on http://localhost:{PORT}")
    print(f"   POST /add-wb  {{\"ticker\": \"NVDA\"}}  to add to Webull Desktop")
    print(f"   GET  /health  to check status")
    print(f"   Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
