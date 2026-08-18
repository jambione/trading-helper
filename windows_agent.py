"""
windows_agent.py — Local Windows agent for TradingView automation.

Run this on your Windows machine:
    python windows_agent.py   (or double-click windows_agent.bat)

The agent does two things:
  1. Watches the Brasfield Momentum dashboard for alerts (mention burst / BUY).
     When a ticker hits the alert threshold it automatically adds it to
     TradingView — no browser toggle needed.
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
from urllib.parse import urlparse, parse_qs
from urllib.request import Request as _UReq, urlopen
from urllib.error import URLError

# ── Version ───────────────────────────────────────────────────────────────────
VERSION = "1.3.0"

try:
    import agent_bus as _agent_bus
except ImportError:  # pragma: no cover
    _agent_bus = None  # type: ignore

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

import desk_auth  # noqa: E402 — after _load_env so creds are in os.environ

# ── Config — set values in .env (see .env.example) ───────────────────────────
DASHBOARD_URL  = os.environ.get("DASHBOARD_URL",  "https://trading.jbrasfield.com")
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "")
POLL_INTERVAL  = float(os.environ.get("POLL_INTERVAL", "1.5"))  # seconds between polls

# ── Token — managed automatically by desk_auth, do not edit ───────────────────
TOKEN_TTL   = 30 * 86400  # fallback if login response omits expires_in
REFRESH_BEFORE = 300      # re-login 5 minutes before expiry

# Browser-like User-Agent so Cloudflare doesn't block the requests with 403
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

LAUNCH_TIMEOUT = 20
BRAVE_TV_TAB   = int(os.environ.get("BRAVE_TV_TAB", "1"))  # Ctrl+N to switch tab


def _webull_installed() -> bool:
    """Retired — Webull Desktop integration removed (compat for old clients)."""
    return False

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


# ── Webull workflow (retired) ─────────────────────────────────────────────────

def workflow_add_wb(ticker: str) -> bool:
    """Retired — Webull Desktop integration removed. Use TradingView only."""
    print("  ⏭  ADD_WB skipped — Webull integration retired (TradingView only)")
    return False


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

# desk_auth owns the token cache, the lock and the floor between attempts.
# _ensure_token() runs on every /api/state poll, so a login that kept failing
# used to re-POST /auth/login on every one of them.
_dash_auth = desk_auth.for_process(
    "windows_agent",
    Path(__file__).parent,
    default_url=DASHBOARD_URL,
    user_agent=_UA,          # browser-like — Cloudflare 403s the default
    log_prefix="  🔑",
    refresh_before=REFRESH_BEFORE,
)


def _login() -> bool:
    """Force a fresh login. False on failure or while backing off."""
    _dash_auth.set_creds(DASHBOARD_URL, DASHBOARD_USER, DASHBOARD_PASS)
    return bool(_dash_auth.token(force=True))


def _ensure_token():
    """Log in if there is no usable token. Cheap enough to call per poll."""
    _dash_auth.set_creds(DASHBOARD_URL, DASHBOARD_USER, DASHBOARD_PASS)
    _dash_auth.token()


def _auth_header() -> dict:
    tok = _dash_auth.token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


# ── Alert listener — polls dashboard and triggers workflow on alerts ──────────

# Tracks previous ticker state so we only fire on rising edges
_prev_bursts   = {}   # ticker → last known mention_burst bool
_prev_statuses = {}   # ticker → last known status string

# Cooldown: don't re-fire the same ticker within this many seconds
_COOLDOWN = 60
_last_fired: dict[str, float] = {}

# Activated-ticker history: once a ticker has been pushed to TradingView we skip
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

# Serialise all workflow calls through a single worker thread so TV
# automation never overlap (pyautogui is not thread-safe).
# Queue entries are (ticker, mode) where mode is 'wb', 'tv', or 'both'.
_work_queue: list[tuple[str, str]] = []
_work_lock  = threading.Lock()
_work_event = threading.Event()


def _enqueue(ticker: str, mode: str = "both"):
    """Add ticker+mode to the workflow queue (deduplicated within cooldown window)."""
    now = time.time()
    if _is_recently_activated(ticker):
        print(f"  ⏭  {ticker} already activated in the last 15 min — skipping")
        return
    if now - _last_fired.get(ticker, 0) < _COOLDOWN:
        print(f"  ⏱  {ticker} cooldown — skipping")
        return
    _last_fired[ticker] = now
    with _work_lock:
        if not any(t == ticker and m == mode for t, m in _work_queue):
            _work_queue.append((ticker, mode))
            print(f"  📥 Queued {ticker} [{mode}]  (queue depth: {len(_work_queue)})")
    _work_event.set()


def _worker():
    """Background thread: drain the queue and run the workflow for each ticker."""
    while True:
        _work_event.wait()
        _work_event.clear()
        while True:
            with _work_lock:
                item = _work_queue.pop(0) if _work_queue else None
            if item is None:
                break
            ticker, mode = item
            label = {"wb": "WB", "tv": "TV", "both": "WB+TV"}.get(mode, mode.upper())
            print(f"\n🚨 ALERT → {ticker}  running {label} workflow…")
            wb_ok = tv_ok = False
            if mode in ("wb", "both"):
                wb_ok = workflow_add_wb(ticker)
                if mode == "both":
                    time.sleep(0.5)
            if mode in ("tv", "both"):
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

    def _bus_deps(self):
        if _agent_bus is None:
            return None
        return _agent_bus.BusDeps(
            enqueue=_enqueue,
            publish_focus=lambda sym, src: _agent_bus.publish_focus_file(
                sym, source=src or "agent"),
            agent_version=VERSION,
        )

    def _dispatch_bus(self, data: dict) -> None:
        deps = self._bus_deps()
        if deps is None:
            self._json(500, {"ok": False, "error": "agent_bus not available"})
            return
        result = _agent_bus.dispatch(deps, data)
        payload = result.as_dict(agent_version=VERSION)
        if result.symbol:
            payload.setdefault("ticker", result.symbol)
        self._json(result.http_status, payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/v1/actions":
            actions = _agent_bus.list_actions() if _agent_bus else []
            self._json(200, {
                "ok": True, "bus": "v1", "version": VERSION, "actions": actions,
            })
            return
        if path == "/v1/action":
            qs = parse_qs(parsed.query)
            data = {
                "action": (qs.get("action") or [""])[0],
                "symbol": (qs.get("symbol") or qs.get("ticker") or [""])[0],
                "source": (qs.get("source") or ["manual"])[0],
            }
            self._dispatch_bus(data)
            return
        if path == "/health":
            actions = (
                [a["action"] for a in _agent_bus.list_actions()]
                if _agent_bus else []
            )
            self._json(200, {
                "ok":               True,
                "agent":            "wb-tv-agent",
                "version":          VERSION,
                "bus":              "v1",
                "actions":          actions,
                "platform":         sys.platform,
                "dashboard_url":    DASHBOARD_URL,
                "polling":          True,
                "brave_tv_tab":     BRAVE_TV_TAB,
                "webull_installed": False,
                "activated_count":  len(_activated),
            })
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return
        if not isinstance(data, dict):
            self._json(400, {"error": "JSON object required"})
            return

        if path in ("/v1/action", "/action"):
            self._dispatch_bus(data)
            return

        if path in ("/add", "/add-wb", "/add-tv"):
            ticker = str(data.get("ticker") or data.get("symbol") or "").strip().upper()
            if not ticker:
                self._json(400, {"error": "missing ticker"})
                return
            if path == "/add-wb":
                action, mode = "add_wb", "wb"
            elif path == "/add-tv":
                action, mode = "add_tv", "tv"
            else:
                mode = str(data.get("mode", "both")).strip().lower()
                if mode not in ("wb", "tv", "both"):
                    mode = "both"
                action = (
                    _agent_bus.legacy_add_to_action(mode)
                    if _agent_bus else "add"
                )
            self._dispatch_bus({
                "action": action,
                "symbol": ticker,
                "source": data.get("source") or "legacy_post",
                "mode": mode,
            })
            return

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
    print(f"  TV Agent  v{VERSION}")
    print(f"{'='*54}")
    print(f"  Dashboard : {DASHBOARD_URL}")
    print(f"  User      : {DASHBOARD_USER or 'NOT SET — edit DASHBOARD_USER in script'}")
    print(f"  Password  : {'set ✓' if DASHBOARD_PASS else 'NOT SET — edit DASHBOARD_PASS in script'}")
    print(f"  Poll every: {POLL_INTERVAL}s")
    print(f"  TV tab    : Ctrl+{BRAVE_TV_TAB}")
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
    print("   GET  /health /v1/actions")
    print("   POST /v1/action  {\"action\":\"load_tv\",\"symbol\":\"NVDA\"}")
    print(f"   POST /add-tv    legacy TV load (Ctrl+{BRAVE_TV_TAB})")
    print("   Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
