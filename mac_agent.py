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
VERSION = "1.3.0"

# Command bus (POST /v1/action) — see docs/AGENT_COMMAND_BUS.md / agent_bus.py
try:
    import agent_bus as _agent_bus
except ImportError:  # pragma: no cover
    _agent_bus = None  # type: ignore

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

import desk_auth  # noqa: E402 — after _load_env so creds are in os.environ

# ── Config — set values in .env (see .env.example) ───────────────────────────
DASHBOARD_URL  = os.environ.get("DASHBOARD_URL",  "https://trading.jbrasfield.com")
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "")
POLL_INTERVAL  = float(os.environ.get("POLL_INTERVAL", "1.5"))  # seconds between polls
# Clicking a burst toast opens this chart; {sym} is replaced with the ticker.
TV_CHART_URL   = os.environ.get(
    "TV_CHART_URL", "https://www.tradingview.com/chart/x04Gfcu8/?symbol={sym}"
)
# AUTO_ADD=1 → hands-free auto for burst + buy_zone (legacy). Prefer EVENT_*.
AUTO_ADD       = os.environ.get("AUTO_ADD", "0") == "1"

# Per-event routing: off | toast | auto  (see agent_events.py / docs)
try:
    import agent_events as _agent_events
    _EVENT_MODES = _agent_events.load_event_modes(auto_add=AUTO_ADD)
except ImportError:  # pragma: no cover
    _agent_events = None  # type: ignore
    _EVENT_MODES = {
        "burst": "auto" if AUTO_ADD else "toast",
        "buy_zone": "auto" if AUTO_ADD else "toast",
        "ax": "toast",
    }

# ── Token — managed automatically by desk_auth, do not edit ───────────────────
TOKEN_TTL      = 30 * 86400  # fallback if login response omits expires_in
REFRESH_BEFORE = 300     # re-login 5 minutes before expiry

# Browser-like User-Agent so Cloudflare doesn't block requests with 403
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

BRAVE_TV_TAB = int(os.environ.get("BRAVE_TV_TAB", "1"))

# ── macOS: pyautogui import ───────────────────────────────────────────────────
try:
    import pyautogui as _pag
    _pag.FAILSAFE = False   # don't abort when mouse hits corner
    # pyautogui sleeps PAUSE seconds after EVERY public call, and the default is
    # 0.1s. workflow_add_tv made nine such calls for a 4-character ticker —
    # hotkey, escape, click, one press per letter, enter, Option+W — so the
    # default alone cost ~0.9s per load, invisible in the source. (press/hotkey/
    # click each cost exactly one PAUSE, not one per key: they call the private
    # platformModule._keyDown/_keyUp internally rather than the decorated
    # public wrappers.) Typing via write() instead of a press-per-letter loop
    # drops it further, to six calls. 20ms still yields the event loop between
    # keystrokes; the places that genuinely need to wait now poll for the thing
    # they are waiting on instead of guessing.
    _pag.PAUSE = float(os.environ.get("TV_PAG_PAUSE", "0.02"))
    _PAG_OK = True
except ImportError:
    _PAG_OK = False
    print("[WARN] pyautogui not installed — keyboard automation disabled. Run: pip install pyautogui")


# ── TradingView load tuning ───────────────────────────────────────────────────
# An AppleScript round-trip costs ~100ms (measured: frontmost 123ms, tab title
# 99ms, window bounds 86ms), so polling only beats a fixed sleep where the wait
# was longer than that. Where there is no cheap thing to poll — the chart canvas
# taking keyboard focus after a click — a short sleep is still the honest
# answer, just a smaller one.
TV_SETTLE_SEC = float(os.environ.get("TV_SETTLE_SEC", "0.12"))   # post-click focus
TV_TYPE_INTERVAL = float(os.environ.get("TV_TYPE_INTERVAL", "0.012"))
TV_POLL_SEC = float(os.environ.get("TV_POLL_SEC", "0.06"))
TV_FOCUS_TIMEOUT = float(os.environ.get("TV_FOCUS_TIMEOUT", "2.0"))
TV_LOAD_TIMEOUT = float(os.environ.get("TV_LOAD_TIMEOUT", "2.5"))


def _wait_until(predicate, timeout: float, poll: float = None) -> bool:
    """Poll `predicate` until it is true or `timeout` elapses.

    Returns True if it became true. The first check happens immediately — the
    common case is that the thing already happened, and a fixed sleep pays the
    full price every time for the rare case where it has not.
    """
    poll = TV_POLL_SEC if poll is None else poll
    deadline = time.time() + timeout
    while True:
        try:
            if predicate():
                return True
        except Exception:                                  # noqa: BLE001
            pass
        if time.time() >= deadline:
            return False
        time.sleep(poll)


def _frontmost_app() -> str:
    code, out = _osascript(
        'tell application "System Events" to return name of first '
        'application process whose frontmost is true')
    return (out or "").strip() if code == 0 else ""


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
    """Bring an app to the foreground via AppleScript.

    Returns once the app is actually frontmost rather than after a fixed 0.4s
    guess. When it is already frontmost — the normal case while working a list
    of symbols — this costs one AppleScript round-trip and no sleep at all.
    """
    if _frontmost_app() == app_name:
        return True
    code, _ = _osascript(f'tell application "{app_name}" to activate')
    if code != 0:
        return False
    return _wait_until(lambda: _frontmost_app() == app_name, TV_FOCUS_TIMEOUT)


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


def _front_window_bounds(app_name: str) -> tuple[int, int, int, int] | None:
    """(x, y, width, height) of app_name's front window, or None.

    System Events reports position/size in the same top-left origin point
    space pyautogui clicks in, so the values need no conversion.
    """
    script = f'''
tell application "System Events"
  tell process "{app_name}"
    if (count of windows) is 0 then return ""
    set p to position of front window
    set s to size of front window
    return ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & \
           ((item 1 of s) as text) & "," & ((item 2 of s) as text)
  end tell
end tell
'''
    try:
        code, out = _osascript(script)
    except Exception:
        return None
    if code != 0 or not (out or "").strip():
        return None
    try:
        x, y, w, h = (int(float(v)) for v in out.strip().split(","))
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    return (x, y, w, h)


def _chart_click_point(browser: str) -> tuple[int, int]:
    """Where to click to give the TradingView chart canvas keyboard focus.

    Derived from the browser window's ACTUAL bounds. The previous version
    hardcoded 15% of screen width, which assumed the browser sat on the left
    half of the display — when the window layout changed, that coordinate
    landed on whatever app was there instead (typically the Terminal running
    the monitor), so every keystroke went to the wrong window while pyautogui
    still reported success.

    35% of window width stays clear of TradingView's right-hand watchlist
    panel; 40% of window height stays above the bottom broker panel.
    """
    bounds = _front_window_bounds(browser)
    if bounds:
        wx, wy, ww, wh = bounds
        return (int(wx + ww * 0.35), int(wy + wh * 0.40))
    sw, sh = _pag.size()
    print("  ⚠️  could not read window bounds — falling back to screen-relative click")
    return (int(sw * 0.15), sh // 2)


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

    # Already parked on the TradingView tab? Then the tab switch and the escape
    # that undoes its address-bar focus are both pure cost. This is the common
    # case once you are working a list, and skipping it is most of the gain.
    on_chart = read_tv_symbol()

    if on_chart == ticker:
        # Nothing to type. The old path retyped the symbol, waited for the
        # chart to "change" to what it already showed, and then saved it —
        # same end state, several seconds later.
        #
        # Still click the canvas first: read_tv_symbol() reads the TAB TITLE,
        # which answers correctly even when keyboard focus is sitting in the
        # address bar, and Option+W sent there does nothing at all.
        cx, cy = _chart_click_point(browser)
        _pag.click(cx, cy)
        time.sleep(TV_SETTLE_SEC)
        _pag.hotkey("option", "w")
        print(f"  ✅ ADD_TV done: {ticker} (already on chart)")
        return True

    if on_chart is None:
        # Cmd+tab_num — switch to the pinned TradingView tab
        # On macOS, browser tab shortcuts are Cmd+1…Cmd+9 (not Ctrl)
        _pag.hotkey("command", str(tab_num))
        _wait_until(lambda: read_tv_symbol() is not None, TV_FOCUS_TIMEOUT)

        # Escape — return focus from address bar to the page
        _pag.press("escape")

    # Click the chart canvas to ensure it has keyboard focus. The point is
    # computed from the browser window's real bounds, so moving or resizing
    # windows no longer sends the keystrokes to the wrong app.
    cx, cy = _chart_click_point(browser)
    _pag.click(cx, cy)
    # No cheap way to observe the canvas taking focus, so this one stays a
    # sleep — just a short one. Everything downstream is verified anyway.
    time.sleep(TV_SETTLE_SEC)

    # Type ticker — TradingView opens symbol search on first keypress
    _pag.write(ticker.lower(), interval=TV_TYPE_INTERVAL)
    _pag.press("enter")       # confirm symbol

    # Confirm the chart actually loaded the symbol before saving it. Without
    # this the function reported success even when the keystrokes went to the
    # wrong window — and Option+W could add whatever symbol WAS on the chart
    # to the watchlist. Verify first, then save.
    #
    # This poll replaces the fixed 0.4s + 0.3s that used to run before it: the
    # read is the wait. It is also the only thing standing between a missed
    # keystroke and the wrong symbol in the watchlist, so it keeps its full
    # timeout — quicker must not mean looser.
    seen: list[str | None] = [None]

    def _chart_shows_ticker() -> bool:
        seen[0] = read_tv_symbol()
        return seen[0] == ticker

    _wait_until(_chart_shows_ticker, TV_LOAD_TIMEOUT)
    loaded = seen[0]

    if loaded != ticker:
        print(f"  ❌ ADD_TV failed: chart shows {loaded or 'unknown'}, expected {ticker}")
        print("     Keystrokes likely went to another window — skipping Option+W")
        print("     so the wrong symbol isn't saved to the watchlist.")
        return False

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

# desk_auth owns the token cache, the lock and — the part this file was
# missing — a floor between login attempts. _ensure_token() runs on every
# /api/state poll (POLL_INTERVAL, 1.5s), so a login that kept failing used to
# mean a POST /auth/login every 1.5s for as long as it stayed broken.
_dash_auth = desk_auth.for_process(
    "mac_agent",
    Path(__file__).parent,
    default_url=DASHBOARD_URL,
    user_agent=_UA,          # browser-like — Cloudflare 403s the default
    log_prefix="  🔑",
    refresh_before=REFRESH_BEFORE,
)


def _login() -> bool:
    """Force a fresh login. False on failure, backoff, or machine-secret path."""
    _dash_auth.set_creds(DASHBOARD_URL, DASHBOARD_USER, DASHBOARD_PASS)
    if _dash_auth.desk_secret:
        return True
    return bool(_dash_auth.token(force=True))


def _ensure_token():
    """Make sure the next request can authenticate. Cheap enough to call per poll."""
    _dash_auth.set_creds(DASHBOARD_URL, DASHBOARD_USER, DASHBOARD_PASS)
    if _dash_auth.desk_secret:
        return
    _dash_auth.token()


def _auth_header() -> dict:
    _dash_auth.set_creds(DASHBOARD_URL, DASHBOARD_USER, DASHBOARD_PASS)
    h = _dash_auth.headers()
    return {k: v for k, v in h.items() if k in ("Authorization", "X-Desk-Secret")}


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


def _event_mode(event: str) -> str:
    return _EVENT_MODES.get(event, "toast")


def _fire_desk_event(
    event: str,
    symbol: str,
    *,
    title: str,
    body: str,
    subtitle: str = "",
    meta: dict | None = None,
) -> None:
    """Toast and/or queue bus action per EVENT_* mode."""
    mode = _event_mode(event)
    if _agent_events:
        toast = _agent_events.should_toast(mode)
        auto = _agent_events.should_auto(mode)
        action = _agent_events.bus_action_for(event)
    else:
        toast, auto, action = (mode != "off"), (mode == "auto"), "load_tv"

    if mode == "off":
        return

    print(f"  📣 {event}  {symbol}  mode={mode}  action={action}")
    if toast:
        _notify_mac(title, body, subtitle=subtitle, ticker=symbol)

    if auto:
        # Prefer bus (focus / load_tv + journal meta); fall back to enqueue.
        if _agent_bus is not None:
            deps = _agent_bus.BusDeps(
                enqueue=_enqueue,
                publish_focus=lambda sym, src: _agent_bus.publish_focus_file(
                    sym, source=src or event),
                agent_version=VERSION,
            )
            payload = (
                _agent_events.build_event_payload(
                    event, symbol, source=event, meta=meta)
                if _agent_events
                else {"action": action, "symbol": symbol, "source": event}
            )
            result = _agent_bus.dispatch(deps, payload)
            print(f"     bus → {result.result} ({result.action})")
            # Lightweight journal of auto fires
            if result.ok and _agent_bus is not None:
                try:
                    _agent_bus.append_journal(
                        action=event,
                        symbol=symbol,
                        source="agent_auto",
                        meta={"mode": mode, "bus_action": action, **(meta or {})},
                    )
                except Exception:
                    pass
        else:
            _enqueue(symbol, "tv")


def _alert_listener():
    """
    Polls the dashboard every POLL_INTERVAL seconds.

    Event → bus routing (see agent_events / EVENT_* env):
      - mention_burst rising edge
      - signal_proximity status → buy_zone
      - AI suggestion newly marked AX (agreement)
    """
    modes = ", ".join(f"{k}={v}" for k, v in sorted(_EVENT_MODES.items()))
    print(f"👂 Alert listener started — polling {DASHBOARD_URL} every {POLL_INTERVAL}s")
    print(f"   event modes: {modes}")
    if not DASHBOARD_USER:
        print("  ⚠️  DASHBOARD_USER not set — requests may be rejected if auth is required")

    prev_ax: set[str] = set()
    ax_primed = False

    while True:
        state = _fetch_state()
        if state:
            for row in state.get("tickers", []):
                sym    = row.get("ticker", "")
                if not sym:
                    continue
                burst  = bool(row.get("mention_burst", False))
                sp     = row.get("signal_proximity") or {}
                status = sp.get("status", "") or ""

                prev_burst  = _prev_bursts.get(sym)
                prev_status = _prev_statuses.get(sym)

                # Rising edge only after we have a previous observation
                if burst and prev_burst is False:
                    count = row.get("mention_window", 0)
                    px = row.get("price")
                    _fire_desk_event(
                        "burst",
                        sym,
                        title=f"🔥 {sym}  burst",
                        body=f"{count}x mentions — click to load TV",
                        subtitle=f"${px:.2f}" if px is not None else "",
                        meta={"mention_window": count, "price": px},
                    )

                if (
                    status == "buy_zone"
                    and prev_status is not None
                    and prev_status != "buy_zone"
                ):
                    px = row.get("price")
                    price_bit = f"${px:.2f} — " if px is not None else ""
                    _fire_desk_event(
                        "buy_zone",
                        sym,
                        title=f"📈 Momentum BUY  {sym}",
                        body=f"{price_bit}click to focus + load TV",
                        subtitle="",
                        meta={
                            "price": px,
                            "proximity_pct": sp.get("proximity_pct"),
                            "cm_rsi": sp.get("cm_rsi"),
                            "pctr": sp.get("pctr"),
                        },
                    )

                _prev_bursts[sym]   = burst
                _prev_statuses[sym] = status

            # AI AX agreement — from merged ai_suggestions in /api/state
            ai = state.get("ai_suggestions") or {}
            rows = ai.get("rows") if isinstance(ai, dict) else None
            if _agent_events and rows is not None:
                cur_ax = _agent_events.ax_symbols(rows)
                by_sym = _agent_events.ax_rows_by_symbol(rows)
            else:
                cur_ax = set()
                by_sym = {}
                if isinstance(rows, list):
                    for r in rows:
                        if not isinstance(r, dict):
                            continue
                        if r.get("agreement") or str(r.get("source_mark") or "").upper() == "AX":
                            s = str(r.get("symbol") or "").upper()
                            if s:
                                cur_ax.add(s)
                                by_sym[s] = r

            if not ax_primed:
                prev_ax = set(cur_ax)
                ax_primed = True
            else:
                for sym in sorted(cur_ax - prev_ax):
                    r = by_sym.get(sym) or {}
                    score = r.get("trending_score", r.get("score"))
                    reason = str(r.get("reason") or "")[:40]
                    _fire_desk_event(
                        "ax",
                        sym,
                        title=f"✦ AX agree  {sym}",
                        body=(
                            f"A+X both list this"
                            + (f" · {reason}" if reason else "")
                            + " — click to focus"
                        ),
                        subtitle=f"score {score}" if score is not None else "",
                        meta={"score": score, "reason": reason},
                    )
                prev_ax = set(cur_ax)

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
        # Keep legacy clients happy
        if result.symbol:
            payload.setdefault("ticker", result.symbol)
        self._json(result.http_status, payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/v1/actions":
            actions = _agent_bus.list_actions() if _agent_bus else []
            self._json(200, {
                "ok": True, "bus": "v1", "version": VERSION,
                "actions": actions,
            })
            return

        if path == "/v1/action":
            # Allow GET ?action=&symbol= for Shortcuts / curl one-liners
            qs = parse_qs(parsed.query)
            data = {
                "action": (qs.get("action") or qs.get("cmd") or [""])[0],
                "symbol": (qs.get("symbol") or qs.get("ticker") or [""])[0],
                "source": (qs.get("source") or ["manual"])[0],
                "mode": (qs.get("mode") or ["tv"])[0],
            }
            reason = (qs.get("reason") or [""])[0]
            if reason:
                data["reason"] = reason
            self._dispatch_bus(data)
            return

        if path == "/add":
            # Legacy toast / curl — routed through the bus
            qs     = parse_qs(parsed.query)
            ticker = (qs.get("ticker", [""])[0]).strip().upper()
            mode   = (qs.get("mode",   ["both"])[0]).strip().lower()
            if mode not in ("wb", "tv", "both"):
                mode = "both"
            if not ticker:
                self._json(400, {"error": "missing ticker"})
                return
            action = (
                _agent_bus.legacy_add_to_action(mode)
                if _agent_bus else "add"
            )
            self._dispatch_bus({
                "action": action, "symbol": ticker,
                "source": "legacy_get", "mode": mode,
            })
            return

        if path == "/health":
            actions = (
                [a["action"] for a in _agent_bus.list_actions()]
                if _agent_bus else []
            )
            self._json(200, {
                "ok":               True,
                "agent":            "mac-tv-agent",
                "version":          VERSION,
                "bus":              "v1",
                "actions":          actions,
                "platform":         sys.platform,
                "dashboard_url":    DASHBOARD_URL,
                "polling":          True,
                "brave_tv_tab":     BRAVE_TV_TAB,
                "webull_installed": False,
                "pyautogui_ok":     _PAG_OK,
                "activated_count":  len(_activated),
            })
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        path   = urlparse(self.path).path.rstrip("/") or "/"
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
            payload = {
                "action": action,
                "symbol": ticker,
                "source": data.get("source") or "legacy_post",
                "mode": mode,
            }
            if data.get("reason"):
                payload["reason"] = data["reason"]
            self._dispatch_bus(payload)
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
    print(f"  AUTO_ADD   : {AUTO_ADD} (legacy; prefer EVENT_BURST / EVENT_BUY_ZONE / EVENT_AX)")
    print(f"  Events     : {', '.join(f'{k}={v}' for k, v in sorted(_EVENT_MODES.items()))}")
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
    print("   GET  /health              → status + action catalog")
    print("   GET  /v1/actions          → verb list")
    print("   POST /v1/action           → command bus (preferred)")
    print('        {"action":"load_tv","symbol":"NVDA","source":"manual"}')
    print("   verbs: load_tv | add_tv | add | focus | journal | ping")
    print("   GET  /add?ticker=NVDA     → legacy (toast) → bus")
    print(f"   POST /add-tv             → legacy TV load (Cmd+{BRAVE_TV_TAB})")
    print("   Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
