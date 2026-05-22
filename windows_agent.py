"""
windows_agent.py — Local Windows agent for Webull + TradingView automation.

Run this on your Windows machine:
    python windows_agent.py   (or double-click windows_agent.bat)

The agent does two things:
  1. Watches the Brasfield Momentum dashboard for alerts (mention burst / BUY).
     When a ticker hits the alert threshold it automatically adds it to
     Webull Desktop and TradingView — no browser toggle needed.
  2. Listens on http://localhost:8889 so the dashboard can also trigger it
     manually via the Add button or Auto-Add toggle.

Config (edit the values below — no environment variables needed):
    DASHBOARD_URL   Full URL of your dashboard
    DASHBOARD_USER  Your dashboard login username
    DASHBOARD_PASS  Your dashboard login password
    BRAVE_TV_TAB    Ctrl+N tab number for the pinned TradingView tab (default: 1)

The agent logs in automatically and refreshes the token before it expires —
you never need to touch the token manually.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request as _UReq, urlopen
from urllib.error import URLError

# ── Version ───────────────────────────────────────────────────────────────────
VERSION = "1.1.0"

# ── Platform ──────────────────────────────────────────────────────────────────
_IS_WINDOWS = sys.platform == "win32"

# ── .env loader ───────────────────────────────────────────────────────────────
def _load_env(path: Path):
    """
    Read KEY=VALUE pairs from a .env file into os.environ.
    Existing environment variables always take precedence.
    Supports plain, "double-quoted", and 'single-quoted' values.
    Lines starting with # are ignored.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val

_load_env(Path(__file__).parent / ".env")

# ── Config — set values in .env (see .env.example) ───────────────────────────
DASHBOARD_URL  = os.environ.get("DASHBOARD_URL",  "https://trading.jbrasfield.com")
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "")
POLL_INTERVAL  = float(os.environ.get("POLL_INTERVAL", "1.5"))  # seconds between polls

# ── Token — managed automatically, do not edit ────────────────────────────────
_token      = ""          # current JWT, refreshed automatically
_token_lock = threading.Lock()
TOKEN_TTL   = 86400       # server issues 24-hour tokens
REFRESH_BEFORE = 300      # re-login 5 minutes before expiry

# Browser-like User-Agent so Cloudflare doesn't block the requests with 403
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_token_expires_at = 0.0   # unix timestamp when current token expires

WB_WINDOW      = "Webull Desktop"
WB_LAUNCH      = r"C:\Program Files (x86)\Webull Desktop\Webull Desktop.exe"
# Fallback locations that Webull's installer has used over the years.
WB_LAUNCH_CANDIDATES = [
    WB_LAUNCH,
    r"C:\Program Files\Webull Desktop\Webull Desktop.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Webull Desktop\Webull Desktop.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Webull Desktop\Webull Desktop.exe"),
]
LAUNCH_TIMEOUT = 20


def _webull_installed() -> bool:
    """True if any known Webull Desktop install path exists on this PC."""
    if not _IS_WINDOWS:
        return False
    return any(os.path.isfile(p) for p in WB_LAUNCH_CANDIDATES if p)
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

    if not _webull_installed():
        print("  ⏭  ADD_WB skipped — Webull Desktop is not installed")
        return False

    # Prefer whichever known install path actually exists on this PC.
    launch_path = next(
        (p for p in WB_LAUNCH_CANDIDATES if p and os.path.isfile(p)),
        WB_LAUNCH,
    )

    print(f"  📊 ADD_WB → {ticker}")
    if not _ensure_open(WB_WINDOW, launch_path):
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


# ── Auth — auto-login and token refresh ──────────────────────────────────────

def _login() -> bool:
    """
    POST to /auth/login with username + password.
    Stores the returned token and calculates its expiry time.
    Returns True on success.
    """
    global _token, _token_expires_at
    if not DASHBOARD_USER or not DASHBOARD_PASS:
        return False  # no credentials configured — skip auth
    url  = DASHBOARD_URL.rstrip("/") + "/auth/login"
    body = json.dumps({"username": DASHBOARD_USER, "password": DASHBOARD_PASS}).encode()
    try:
        req  = _UReq(url, data=body, headers={"Content-Type": "application/json", "User-Agent": _UA})
        resp = urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        tok  = data.get("token") or data.get("access_token", "")
        if tok:
            with _token_lock:
                _token            = tok
                _token_expires_at = time.time() + TOKEN_TTL
            print("  🔑 Logged in — token valid for 24 h")
            return True
        print(f"  ⚠️  Login response had no token: {data}")
        return False
    except Exception as e:
        print(f"  ⚠️  Login failed: {e}")
        return False


def _ensure_token():
    """Re-login if the token is missing or about to expire."""
    with _token_lock:
        expires = _token_expires_at
        has_tok = bool(_token)
    if not has_tok or time.time() >= expires - REFRESH_BEFORE:
        _login()


def _auth_header() -> dict:
    with _token_lock:
        tok = _token
    return {"Authorization": f"Bearer {tok}"} if tok else {}


# ── Alert listener — polls dashboard and triggers workflow on alerts ──────────

# Tracks previous ticker state so we only fire on rising edges
_prev_bursts   = {}   # ticker → last known mention_burst bool
_prev_statuses = {}   # ticker → last known status string

# Cooldown: don't re-fire the same ticker within this many seconds
_COOLDOWN = 60
_last_fired: dict[str, float] = {}

# Activated-ticker history: once a ticker has been pushed to Webull/TV we skip
# it for this many seconds so the agent doesn't keep re-adding the same symbol.
ACTIVATED_TTL = 15 * 60   # 15 minutes
_activated: dict[str, float] = {}
_activated_lock = threading.Lock()


def _is_recently_activated(ticker: str) -> bool:
    """Return True if `ticker` was added within the last ACTIVATED_TTL seconds."""
    now = time.time()
    with _activated_lock:
        ts = _activated.get(ticker)
        if ts is None:
            return False
        if now - ts >= ACTIVATED_TTL:
            del _activated[ticker]
            return False
        return True


def _mark_activated(ticker: str):
    """Record that `ticker` was just added; opportunistically purge stale entries."""
    now = time.time()
    with _activated_lock:
        _activated[ticker] = now
        for sym, ts in list(_activated.items()):
            if now - ts >= ACTIVATED_TTL:
                del _activated[sym]

# Serialise all workflow calls through a single worker thread so Webull and TV
# automation never overlap (pyautogui is not thread-safe).
_work_queue: list[str] = []
_work_lock  = threading.Lock()
_work_event = threading.Event()


def _enqueue(ticker: str):
    """Add ticker to the workflow queue (deduplicated within cooldown window)."""
    now = time.time()
    if _is_recently_activated(ticker):
        print(f"  ⏭  {ticker} already activated in the last 15 min — skipping")
        return
    if now - _last_fired.get(ticker, 0) < _COOLDOWN:
        print(f"  ⏱  {ticker} cooldown — skipping")
        return
    _last_fired[ticker] = now
    with _work_lock:
        if ticker not in _work_queue:
            _work_queue.append(ticker)
    _work_event.set()


def _worker():
    """Background thread: drain the queue and run the workflow for each ticker."""
    while True:
        _work_event.wait()
        _work_event.clear()
        while True:
            with _work_lock:
                ticker = _work_queue.pop(0) if _work_queue else None
            if ticker is None:
                break
            print(f"\n🚨 ALERT → {ticker}  running WB+TV workflow…")
            wb_ok = workflow_add_wb(ticker)
            time.sleep(0.5)
            tv_ok = workflow_add_tv(ticker)
            if wb_ok or tv_ok:
                _mark_activated(ticker)


def _fetch_state() -> dict | None:
    """Fetch /api/state from the dashboard. Returns parsed JSON or None on error."""
    _ensure_token()
    url     = DASHBOARD_URL.rstrip("/") + "/api/state"
    headers = {"Accept": "application/json", "User-Agent": _UA, **_auth_header()}
    try:
        req  = _UReq(url, headers=headers)
        resp = urlopen(req, timeout=5)
        return json.loads(resp.read().decode())
    except URLError as e:
        print(f"  ⚠️  fetch error: {e.reason}")
        return None
    except Exception as e:
        print(f"  ⚠️  fetch error: {e}")
        return None


def _alert_listener():
    """
    Polls the dashboard every POLL_INTERVAL seconds.
    Fires the WB+TV workflow when:
      - mention_burst rises from False → True  (rapid mention spike)
      - status transitions to 'BUY'
    Same rising-edge logic as notifications.js in the browser.
    """
    print(f"👂 Alert listener started — polling {DASHBOARD_URL} every {POLL_INTERVAL}s")
    if not DASHBOARD_USER:
        print("  ⚠️  DASHBOARD_USER not set — requests may be rejected if auth is required")

    while True:
        state = _fetch_state()
        if state:
            tickers = state.get("tickers", [])
            for row in tickers:
                sym   = row.get("ticker", "")
                burst = row.get("mention_burst", False)
                status = row.get("status", "")

                prev_burst  = _prev_bursts.get(sym)
                prev_status = _prev_statuses.get(sym)

                # Rising edge: mention_burst False → True
                if burst and prev_burst is False:
                    print(f"  🔥 Burst detected: {sym}  (mention_window={row.get('mention_window')})")
                    _enqueue(sym)

                # BUY signal transition
                if status == "BUY" and prev_status is not None and prev_status != "BUY":
                    print(f"  📈 BUY signal: {sym}")
                    _enqueue(sym)

                _prev_bursts[sym]   = burst
                _prev_statuses[sym] = status

        time.sleep(POLL_INTERVAL)


# ── HTTP handler (manual / browser-triggered calls) ───────────────────────────

class AgentHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _cors(self):
        """
        Add CORS and Private Network Access (PNA) headers.
        PNA headers are required for secure origins (HTTPS) to access local loopback (HTTP).
        """
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")

        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        # Required handshake for Chrome/Brave PNA policy
        self.send_header("Access-Control-Allow-Private-Network", "true")
        # Cache the preflight to improve performance
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self):
        """Handle CORS/PNA preflight request."""
        self.send_response(204)  # 'No Content' is preferred for OPTIONS
        self._cors()
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {
                "ok":               True,
                "agent":            "wb-tv-agent",
                "version":          VERSION,
                "platform":         sys.platform,
                "dashboard_url":    DASHBOARD_URL,
                "polling":          True,
                "brave_tv_tab":     BRAVE_TV_TAB,
                "webull_installed": _webull_installed(),
                "activated_count":  len(_activated),
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
            if _is_recently_activated(ticker):
                print(f"  ⏭  {ticker} already activated in the last 15 min — skipping")
                self._json(200, {"ok": True, "ticker": ticker, "skipped": "recently_activated"})
                return
            if path == "/add-wb":
                ok = workflow_add_wb(ticker)
            else:
                ok = workflow_add_tv(ticker)
            if ok:
                _mark_activated(ticker)
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

    print(f"\n{'='*54}")
    print(f"  WB+TV Agent  v{VERSION}")
    print(f"{'='*54}")
    print(f"  Dashboard : {DASHBOARD_URL}")
    print(f"  User      : {DASHBOARD_USER or 'NOT SET — edit DASHBOARD_USER in script'}")
    print(f"  Password  : {'set ✓' if DASHBOARD_PASS else 'NOT SET — edit DASHBOARD_PASS in script'}")
    print(f"  Poll every: {POLL_INTERVAL}s")
    print(f"  TV tab    : Ctrl+{BRAVE_TV_TAB}")
    if _IS_WINDOWS:
        if _webull_installed():
            print("  Webull    : detected ✓")
        else:
            print("  Webull    : NOT installed — Webull adds will be skipped")
    print(f"  Skip-add window: {ACTIVATED_TTL // 60} min after activation")
    print(f"{'='*54}\n")

    # Log in immediately on startup so the listener has a token ready
    if DASHBOARD_USER and DASHBOARD_PASS:
        _login()

    # Start workflow worker thread
    threading.Thread(target=_worker, daemon=True, name="worker").start()

    # Start alert listener thread (polls dashboard for bursts / BUY signals)
    threading.Thread(target=_alert_listener, daemon=True, name="listener").start()

    # Start HTTP server (for manual calls from dashboard Auto-Add toggle)
    server = HTTPServer(("0.0.0.0", PORT), AgentHandler)
    print(f"✅ HTTP server ready on http://localhost:{PORT}")
    print("   GET  /health  → status")
    print("   POST /add-wb  {\"ticker\": \"NVDA\"}  → Webull Desktop")
    print(f"   POST /add-tv  {{\"ticker\": \"NVDA\"}}  → TradingView (Brave, Ctrl+{BRAVE_TV_TAB}, Alt+W)")
    print("   Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
