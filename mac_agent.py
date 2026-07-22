"""
mac_agent.py — Local macOS agent for TradingView automation.

Run this on your Mac:
    bash mac_agent.sh          (recommended — checks deps first)
    python mac_agent.py        (if deps already installed)

The agent does two things:
  1. Watches the Brasfield Momentum dashboard for alerts (mention burst / BUY)
     and posts a native toast for each. By default it does NOT auto-add (so you
     can run everything minimized) — click a toast to add that ticker to TradingView
     Desktop + TradingView. Set AUTO_ADD=1 for the old hands-free behavior
     (auto-adds on every burst/BUY, which steals window focus).
  2. Listens on http://localhost:8889 so the dashboard (and the toast click)
     can trigger an add manually.

Config (edit the values below — no environment variables needed):
    DASHBOARD_URL   Full URL of your dashboard
    DASHBOARD_USER  Your dashboard login username
    DASHBOARD_PASS  Your dashboard login password
    BRAVE_TV_TAB    Cmd+N tab number for the pinned TradingView tab (default: 1)
    AUTO_ADD        1 = auto-add on every alert (steals focus); default 0 =
                    toast-only, click a toast to add (run minimized)

macOS prerequisites (one-time):
  - Grant Terminal (or your IDE) Accessibility access:
      System Settings → Privacy & Security → Accessibility → add Terminal ✓
  - pip install pyautogui

The agent logs in automatically and refreshes the token before it expires —
you never need to touch the token manually.
"""

from __future__ import annotations

import json
import os
import re
import socket
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
VERSION = "1.2.0"

# ── Platform ──────────────────────────────────────────────────────────────────
_IS_MAC = sys.platform == "darwin"

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
# Clicking a burst toast opens this chart; {sym} is replaced with the ticker.
TV_CHART_URL   = os.environ.get(
    "TV_CHART_URL", "https://www.tradingview.com/chart/x04Gfcu8/?symbol={sym}"
)
# AUTO_ADD=1 → hands-free: auto-add to WB+TV on every burst/BUY (old behavior,
# steals focus). Default off → run minimized; click a toast to add that ticker.
AUTO_ADD       = os.environ.get("AUTO_ADD", "0") == "1"

# ── Token — managed automatically, do not edit ────────────────────────────────
_token      = ""
_token_lock = threading.Lock()
TOKEN_TTL      = 86400   # server issues 24-hour tokens
REFRESH_BEFORE = 300     # re-login 5 minutes before expiry

# Browser-like User-Agent so Cloudflare doesn't block requests with 403
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_token_expires_at = 0.0

BRAVE_TV_TAB = int(os.environ.get("BRAVE_TV_TAB", "1"))

# ── macOS: pyautogui import ───────────────────────────────────────────────────
try:
    import pyautogui as _pag
    _pag.FAILSAFE = False   # don't abort when mouse hits corner
    _PAG_OK = True
except ImportError:
    _PAG_OK = False
    print("[WARN] pyautogui not installed — keyboard automation disabled. Run: pip install pyautogui")


# =============================================================================
# macOS window helpers (AppleScript via osascript)
# =============================================================================

def _osascript(script: str) -> tuple[int, str]:
    """Run an AppleScript snippet, return (returncode, stdout)."""
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


# BrasfieldNotifier.app listens here (see mac_notifier/notifier.swift). It posts
# native banners via the modern UNUserNotificationCenter API — the only path that
# works on macOS 26 (terminal-notifier/osascript use the removed NSUserNotification
# API) — and runs the add-to-TV+WB workflow when a banner is clicked.
NOTIFIER_PORT = 8890


def _notify_mac(title: str, message: str, subtitle: str = "",
                ticker: str = "") -> None:
    """
    Post a native macOS notification banner via BrasfieldNotifier.app.

    Sends one line of JSON to the notifier on 127.0.0.1:NOTIFIER_PORT. When
    `ticker` is set, clicking the banner fires the add-to-TradingView
    workflow back on this agent (no browser tab opens). Fire-and-forget — never
    raises; if the notifier isn't running the alert is simply skipped (the
    terminal log line still prints at the call site).
    """
    if not _IS_MAC:
        print(f"  [DRY RUN] NOTIFY → {title}: {message}")
        return
    payload = json.dumps({
        "title":    title,
        "body":     message,
        "subtitle": subtitle,
        "ticker":   ticker,
    }) + "\n"
    try:
        with socket.create_connection(("127.0.0.1", NOTIFIER_PORT), timeout=2) as s:
            s.sendall(payload.encode())
    except OSError as e:
        print(f"  ⚠️  notifier not reachable on :{NOTIFIER_PORT} ({e}) — banner skipped")


def _app_is_running(app_name: str) -> bool:
    """Return True if the named app has at least one running process."""
    _, out = _osascript(
        f'tell application "System Events" to return (name of processes) contains "{app_name}"'
    )
    return "true" in out.lower()


def _focus_app(app_name: str) -> bool:
    """Bring an app to the foreground via AppleScript."""
    code, _ = _osascript(f'tell application "{app_name}" to activate')
    time.sleep(0.4)
    return code == 0


def _launch_app(app_name: str, bundle_path: str | None = None) -> bool:
    """
    Launch an app by bundle name (preferred) or full .app path.
    Returns True if the launch command succeeded.
    """
    try:
        if bundle_path and Path(bundle_path).exists():
            subprocess.Popen(["open", bundle_path])
        else:
            subprocess.Popen(["open", "-a", app_name])
        return True
    except Exception as e:
        print(f"  ❌ Launch failed for '{app_name}': {e}")
        return False


def _ensure_app_open(app_name: str, bundle_path: str | None = None,
                     timeout: int = 20) -> bool:
    """Open and focus an app, launching it if not already running."""
    if _app_is_running(app_name):
        return _focus_app(app_name)

    print(f"  🚀 Launching '{app_name}'…")
    if not _launch_app(app_name, bundle_path):
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _app_is_running(app_name):
            time.sleep(1.2)
            return _focus_app(app_name)
        time.sleep(0.5)

    print(f"  ❌ '{app_name}' did not appear within {timeout}s")
    return False


# =============================================================================
# Webull workflow (retired)
# =============================================================================

def _webull_installed() -> bool:
    return False


def workflow_add_wb(ticker: str) -> bool:
    """Retired — Webull Desktop integration removed. Use TradingView only."""
    print("  ⏭  ADD_WB skipped — Webull integration retired (TradingView only)")
    return False


# =============================================================================
# TradingView workflow — find Brave or Chrome, switch tab, type ticker
# =============================================================================

def _find_browser() -> str | None:
    """
    Return the running browser app name ('Brave Browser' or 'Google Chrome')
    or None if neither is running.
    """
    for app in ("Brave Browser", "Google Chrome"):
        if _app_is_running(app):
            return app
    return None


def workflow_add_tv(ticker: str, tab_num: int = BRAVE_TV_TAB) -> bool:
    """
    Focus Brave/Chrome, switch to the pinned TradingView tab (Cmd+tab_num),
    click the chart area, type the ticker, press Enter to load the symbol,
    then Option+W to add to the TradingView watchlist.

    Desk path is type-into-chart (character keystrokes on the TV page) — not
    URL navigation.
    """
    ticker = ticker.upper().strip()

    if not _IS_MAC:
        print(f"  [DRY RUN] ADD_TV → {ticker}")
        return True

    if not _PAG_OK:
        print("  ⏭  ADD_TV skipped — pyautogui not available")
        return False

    browser = _find_browser()
    if not browser:
        print("  ❌ ADD_TV failed — Brave Browser / Google Chrome not running")
        return False

    print(f"  🌐 ADD_TV → {ticker}  [{browser}]")

    if not _focus_app(browser):
        print(f"  ❌ ADD_TV failed — could not focus {browser}")
        return False

    time.sleep(0.3)

    # Cmd+tab_num — switch to the pinned TradingView tab
    # On macOS, browser tab shortcuts are Cmd+1…Cmd+9 (not Ctrl)
    _pag.hotkey("command", str(tab_num))
    time.sleep(0.6)

    # Escape — return focus from address bar to the page
    _pag.press("escape")
    time.sleep(0.2)

    # Click the chart canvas to ensure it has keyboard focus.
    # Layout: Brave = left 45% of screen; TV right panel (watchlist) ≈ right 45%
    # of the Brave window. Clicking at 15% of screen width (~1/3 of Brave's width)
    # lands solidly on the chart canvas and avoids the right panel.
    sw, sh = _pag.size()
    _pag.click(int(sw * 0.15), sh // 2)
    time.sleep(0.2)
    time.sleep(0.3)

    # Type ticker — TradingView opens symbol search on first keypress
    for letter in ticker:
        _pag.press(letter.lower())
        time.sleep(0.05)

    time.sleep(0.4)
    _pag.press("enter")       # confirm symbol
    time.sleep(0.3)

    _pag.hotkey("option", "w")  # macOS: Option+W = Add to TradingView watchlist

    print(f"  ✅ ADD_TV done: {ticker}")
    return True


# ── Read the current TradingView chart symbol (browser tab title) ─────────────

# Literal app names on purpose: `tell application (variable)` breaks AppleScript
# terminology resolution, so `tabs`/`URL`/`title` silently fail. Keep them literal.
_TV_READ_SCRIPT = '''
set out to ""
try
  if application "Brave Browser" is running then
    tell application "Brave Browser"
      repeat with w in windows
        repeat with t in tabs of w
          try
            if (URL of t) contains "tradingview.com/chart" then
              set out to (title of t) & linefeed & (URL of t)
            end if
          end try
        end repeat
      end repeat
    end tell
  end if
end try
if out is "" then
  try
    if application "Google Chrome" is running then
      tell application "Google Chrome"
        repeat with w in windows
          repeat with t in tabs of w
            try
              if (URL of t) contains "tradingview.com/chart" then
                set out to (title of t) & linefeed & (URL of t)
              end if
            end try
          end repeat
        end repeat
      end tell
    end if
  end try
end if
return out
'''


def _parse_tv_symbol(title: str, url: str = "") -> str | None:
    """Extract the ticker from a TradingView chart tab title (URL as fallback).

    Handles "ENHA 3.16 … — TradingView" (leading token) and
    "… NYSE:ENHA — TradingView" (EXCHANGE:TICKER), plus a ?symbol= URL param.
    """
    title = (title or "").strip()
    # EXCHANGE:TICKER anywhere in the title (e.g. "NYSE:ENHA")
    m = re.search(r'\b[A-Z]{2,6}:([A-Z][A-Z0-9.\-]{0,9})\b', title)
    if m:
        return m.group(1).upper()
    # Leading token of the title is usually the symbol
    if title:
        cand = re.sub(r'[^A-Z0-9.\-]', '', title.split()[0].upper())
        if re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}', cand or ''):
            return cand
    # Fallback: ?symbol=EXCHANGE:TICKER in the chart URL (may be stale)
    m = re.search(r'[?&]symbol=([^&]+)', url or '')
    if m:
        from urllib.parse import unquote
        s = unquote(m.group(1)).upper()
        if ':' in s:
            s = s.split(':', 1)[1]
        s = re.sub(r'[^A-Z0-9.\-]', '', s)
        if re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}', s or ''):
            return s
    return None


def read_tv_symbol() -> str | None:
    """Current TradingView chart symbol, read from the browser tab title.

    Reads Brave/Chrome tabs via AppleScript — no OCR, no stolen window focus.
    Returns an uppercase ticker, or None when no TV chart tab is found.
    """
    if not _IS_MAC:
        return None
    try:
        code, out = _osascript(_TV_READ_SCRIPT)
    except Exception:
        return None
    if code != 0 or not (out or "").strip():
        return None
    lines = out.strip().splitlines()
    title = lines[0] if lines else ""
    url = lines[1] if len(lines) > 1 else ""
    return _parse_tv_symbol(title, url)


# =============================================================================
# Auth — auto-login and token refresh
# =============================================================================

def _login() -> bool:
    global _token, _token_expires_at
    if not DASHBOARD_USER or not DASHBOARD_PASS:
        return False
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
    with _token_lock:
        expires = _token_expires_at
        has_tok = bool(_token)
    if not has_tok or time.time() >= expires - REFRESH_BEFORE:
        _login()


def _auth_header() -> dict:
    with _token_lock:
        tok = _token
    return {"Authorization": f"Bearer {tok}"} if tok else {}


# =============================================================================
# Alert listener — polls dashboard and triggers workflows on alerts
# =============================================================================

_prev_bursts   = {}
_prev_statuses = {}

_COOLDOWN = 60
_last_fired: dict[str, float] = {}

ACTIVATED_TTL = 15 * 60
_activated: dict[str, float] = {}
_activated_lock = threading.Lock()


def _is_recently_activated(ticker: str) -> bool:
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
    now = time.time()
    with _activated_lock:
        _activated[ticker] = now
        for sym, ts in list(_activated.items()):
            if now - ts >= ACTIVATED_TTL:
                del _activated[sym]


# Serialise all workflow calls through one worker thread — pyautogui is not thread-safe.
_work_queue: list[tuple[str, str]] = []
_work_lock  = threading.Lock()
_work_event = threading.Event()


def _enqueue(ticker: str, mode: str = "both"):
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
    """
    print(f"👂 Alert listener started — polling {DASHBOARD_URL} every {POLL_INTERVAL}s")
    if not DASHBOARD_USER:
        print("  ⚠️  DASHBOARD_USER not set — requests may be rejected if auth is required")

    while True:
        state = _fetch_state()
        if state:
            for row in state.get("tickers", []):
                sym    = row.get("ticker", "")
                burst  = row.get("mention_burst", False)
                sp     = row.get("signal_proximity") or {}
                status = sp.get("status", "")

                prev_burst  = _prev_bursts.get(sym)
                prev_status = _prev_statuses.get(sym)

                if burst and prev_burst is False:
                    count = row.get("mention_window", 0)
                    print(f"  🔥 Burst detected: {sym}  (mention_window={count})")
                    _notify_mac(
                        f"🔥 {sym}  burst",
                        f"{count}x mentions — click to add to TV + WB",
                        subtitle=f"${row['price']:.2f}" if row.get("price") is not None else "",
                        ticker=sym,
                    )
                    if AUTO_ADD:
                        _enqueue(sym)

                if status == "buy_zone" and prev_status is not None and prev_status != "buy_zone":
                    print(f"  📈 BUY signal: {sym}")
                    price = f"${row['price']:.2f} — " if row.get("price") is not None else ""
                    _notify_mac(
                        f"📈 BUY  {sym}",
                        f"{price}signal aligning — click to add to TV + WB",
                        ticker=sym,
                    )
                    if AUTO_ADD:
                        _enqueue(sym)

                _prev_bursts[sym]   = burst
                _prev_statuses[sym] = status

        time.sleep(POLL_INTERVAL)


# =============================================================================
# HTTP handler (manual / browser-triggered calls)
# =============================================================================

class AgentHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",  "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",  "Content-Type, Authorization, X-Requested-With")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/add":
            # Manual add: curl http://localhost:8889/add?ticker=NVDA&mode=both
            qs     = parse_qs(parsed.query)
            ticker = (qs.get("ticker", [""])[0]).strip().upper()
            mode   = (qs.get("mode",   ["both"])[0]).strip().lower()
            if mode not in ("wb", "tv", "both"):
                mode = "both"
            if not ticker:
                self._json(400, {"error": "missing ticker"})
                return
            _enqueue(ticker, mode)
            self._json(202, {"ok": True, "ticker": ticker, "queued": True, "mode": mode})
            return
        if parsed.path == "/health":
            self._json(200, {
                "ok":               True,
                "agent":            "mac-tv-agent",
                "version":          VERSION,
                "platform":         sys.platform,
                "dashboard_url":    DASHBOARD_URL,
                "polling":          True,
                "brave_tv_tab":     BRAVE_TV_TAB,
                "webull_installed": False,
                "pyautogui_ok":     _PAG_OK,
                "activated_count":  len(_activated),
            })
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path   = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return

        ticker = data.get("ticker", "").strip().upper()

        if path in ("/add", "/add-wb", "/add-tv"):
            if not ticker:
                self._json(400, {"error": "missing ticker"})
                return
            if path == "/add-wb":
                mode = "wb"
            elif path == "/add-tv":
                mode = "tv"
            else:
                mode = data.get("mode", "both").strip().lower()
                if mode not in ("wb", "tv", "both"):
                    mode = "both"
            _enqueue(ticker, mode)
            self._json(202, {"ok": True, "ticker": ticker, "queued": True, "mode": mode})
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


# =============================================================================
# Entry point
# =============================================================================

PORT = 8889

if __name__ == "__main__":
    # --test-toast: fire a sample burst through the real notify path, then exit.
    # Requires BrasfieldNotifier.app running (startup.command launches it; or run
    # `open BrasfieldNotifier.app`). If no banner pops, check System Settings →
    # Notifications → Brasfield Trading: Allow on, Alert Style Banners/Alerts,
    # and "Summarize notifications" OFF (Scheduled Summary diverts banners to
    # Notification Center).
    if "--test-toast" in sys.argv:
        print("🔔 Firing test burst notification via BrasfieldNotifier …")
        print("   (the agent must be running for a click to reach /add)")
        _notify_mac(
            "🔥 TSLA  burst",
            "7x mentions — click to add to TV + WB",
            subtitle="$240.50",
            ticker="TSLA",
        )
        print(f"   sent to notifier on 127.0.0.1:{NOTIFIER_PORT}")
        sys.exit(0)

    print(f"\n{'='*56}")
    print(f"  WB+TV Agent (macOS)  v{VERSION}")
    print(f"{'='*56}")
    print(f"  Dashboard  : {DASHBOARD_URL}")
    print(f"  User       : {DASHBOARD_USER or 'NOT SET — set DASHBOARD_USER in .env'}")
    print(f"  Password   : {'set ✓' if DASHBOARD_PASS else 'NOT SET — set DASHBOARD_PASS in .env'}")
    print(f"  Poll every : {POLL_INTERVAL}s")
    print(f"  Add mode   : {'AUTO_ADD (hands-free, steals focus)' if AUTO_ADD else 'toast-click (run minimized)'}")
    print(f"  TV tab     : Cmd+{BRAVE_TV_TAB}")
    print(f"  pyautogui  : {'✓' if _PAG_OK else '✗ not installed — run: pip install pyautogui'}")
    print("  Webull     : retired (Alpaca only)")
    print(f"  Skip-add window: {ACTIVATED_TTL // 60} min after activation")
    print(f"{'='*56}")
    print()
    print("  ⚠️  Accessibility permission required for keyboard automation:")
    print("      System Settings → Privacy & Security → Accessibility → add Terminal ✓")
    print()

    if DASHBOARD_USER and DASHBOARD_PASS:
        _login()

    threading.Thread(target=_worker,         daemon=True, name="worker").start()
    threading.Thread(target=_alert_listener, daemon=True, name="listener").start()

    server = HTTPServer(("0.0.0.0", PORT), AgentHandler)
    print(f"✅ HTTP server ready on http://localhost:{PORT}")
    print("   GET  /health  → status")
    print("   GET  /add?ticker=NVDA&mode=both  → WB+TV (used by toast click)")
    print("   POST /add-wb  retired (use /add-tv)")
    print(f"   POST /add-tv  {{\"ticker\": \"NVDA\"}}  → TradingView (Brave, Cmd+{BRAVE_TV_TAB}, Option+W)")
    print("   Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
