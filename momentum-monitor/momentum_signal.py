"""Momentum desk monitor — Discord momentum + Stocktwits + TradingView load.

Polls the dashboard /api/state feed and Stocktwits trending.
Setup column: green FOCUS when CM RSI-2 and deep %R both fire (signal_proximity).

No Alpaca buy/sell in this desk (B/S/T are Stocktwits letter keys).

Cross-platform (macOS + Windows):

  1-9     focus momentum row + load TradingView
  SPACE   focus newest momentum + load TradingView
  A-J     focus Stocktwits trending row + load TradingView
          (A = 1st under-$max panel row, B = 2nd, … J = 10th)

Focused symbol is written to repo root active_symbol.json.

Run (from repo root or this folder):
  python3 momentum-monitor/momentum_signal.py
  python3 momentum_signal.py
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request as _UReq, urlopen

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:                                      # noqa: BLE001
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetProcessDPIAware()

# Dashboard auth helper (optional — Windows or Mac agent)
wa = None
try:
    if sys.platform == "darwin":
        import mac_agent as wa
    elif sys.platform == "win32":
        import windows_agent as wa
except Exception:                                          # noqa: BLE001
    wa = None

try:
    from session_clock import session_line
except Exception:                                          # noqa: BLE001
    session_line = None

try:
    from plyer import notification as _plyer
except Exception:                                          # noqa: BLE001
    _plyer = None

import desk_actions as desk
from desk_hotkeys import DeskHotkeys
from stocktwits_trending import StocktwitsTrending

WatchlistReader = top_movers = None  # OCR movers retired

CONFIG_PATH = HERE / "momentum_config.json"

DEFAULTS = {
    "poll_interval": 2.0,
    "hotkey_slots": 9,
    "new_ttl": 120.0,
    "alert_new": True,
    "alert_burst": True,
    "alert_buy": False,
    "alert_st_new": True,
    "alert_st_look": True,
    "alert_cooldown": 60.0,
    "alert_notify_interval": 180.0,  # min seconds between OS notification popups
    "alert_notify_duration": 5.0,    # seconds before an OS popup auto-dismisses
    "alert_only_when_hidden": True,  # skip the popup if Terminal is frontmost
    "desktop_toast": True,
    "watchlist_enabled": False,
    # FOCUS = CM RSI green-long AND both %R lines deep OS toward -100
    "rsi_focus_max": 35.0,       # CM RSI-2 in [0, max)
    "pctr_focus_lo": -100.0,     # both %R lines >= lo
    "pctr_focus_hi": -75.0,      # both %R lines <= hi
    # Stocktwits free trending (stocktwits.com/sentiment) — keys A-J
    "stocktwits_enabled": True,
    "stocktwits_poll": 60.0,     # seconds between ST API polls
    "stocktwits_stocks_only": True,
    "stocktwits_max_price": 30.0,  # panel filter when price known; None = no filter
    "stocktwits_panel_limit": 10,  # max 10 → keys A-J
    # LOOK badge: heat + |%chg| + vol + 52w extreme (EXT near high / WASH near low)
    "stocktwits_look_min_abs_chg": 3.0,
    "stocktwits_look_max": 2,
    "stocktwits_look_near_high": 0.70,
    "stocktwits_look_near_low": 0.30,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except Exception:                                      # noqa: BLE001
        pass
    return cfg


# ── feed ─────────────────────────────────────────────────────────────────────

def _dashboard_url() -> str:
    return (wa.DASHBOARD_URL if wa else
            "https://trading.jbrasfield.com").rstrip("/")

_UA = (wa._UA if wa else
       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def fetch_state() -> dict | None:
    """GET /api/state from the dashboard. Browser-like UA so Cloudflare
    doesn't 403 us; optional Bearer token when the server has auth on
    (managed by windows_agent — a no-op when auth is off, which is the
    default). All errors are swallowed so the Live display stays clean;
    the caller shows a 'feed down' banner instead."""
    headers = {"Accept": "application/json", "User-Agent": _UA}
    if wa:
        try:
            wa._ensure_token()
            headers.update(wa._auth_header())
        except Exception:                                  # noqa: BLE001
            pass
    url = _dashboard_url() + "/api/state"
    try:
        req = _UReq(url, headers=headers)
        with urlopen(req, timeout=6) as resp:
            return json.loads(resp.read().decode())
    except Exception:                                      # noqa: BLE001
        return None


def _signal_status(row: dict) -> str | None:
    sp = row.get("signal_proximity")
    return sp.get("status") if isinstance(sp, dict) else None


def _is_buy(row: dict) -> bool:
    return (_signal_status(row) or "").lower() in ("buy", "buy_zone")


def _sp(row: dict) -> dict:
    sp = row.get("signal_proximity")
    return sp if isinstance(sp, dict) else {}


def _fnum(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cm_rsi_value(row: dict) -> float | None:
    """CM RSI-2 (preferred) or classic RSI from signal_proximity."""
    sp = _sp(row)
    for key in ("cm_rsi", "rsi"):
        n = _fnum(sp.get(key))
        if n is not None:
            return n
    return None


def _rsi_leg_ok(row: dict, max_lvl: float = 35.0) -> bool:
    """CM RSI green-long band: value in [0, max_lvl)."""
    rsi = _cm_rsi_value(row)
    return rsi is not None and 0.0 <= rsi < float(max_lvl)


def _pctr_leg_ok(row: dict,
                 lo: float = -100.0,
                 hi: float = -75.0) -> bool:
    """Both %R lines in [lo, hi] and falling toward -100.

    Prefers engine flag `pctr_deep_os`. Falls back to pctr + pctr_slow +
    falling flags when the flag is absent (older engine builds).
    """
    sp = _sp(row)
    if "pctr_deep_os" in sp:
        return bool(sp.get("pctr_deep_os"))

    fast = _fnum(sp.get("pctr"))
    slow = _fnum(sp.get("pctr_slow"))
    if fast is None or slow is None:
        return False
    in_band = (float(lo) <= fast <= float(hi)
               and float(lo) <= slow <= float(hi))
    falling = bool(sp.get("pctr_falling")) and bool(sp.get("pctr_slow_falling"))
    return in_band and falling


def rsi_focus_trigger(row: dict,
                      max_lvl: float = 35.0,
                      pctr_lo: float = -100.0,
                      pctr_hi: float = -75.0) -> tuple[float | None, bool]:
    """FOCUS when RSI leg AND %R deep-OS leg are both true.

    Returns (cm_rsi_or_None, is_focus). FOCUS requires:
      • CM RSI-2 in [0, max_lvl)
      • both %R lines in [pctr_lo, pctr_hi] trending toward -100
    """
    rsi = _cm_rsi_value(row)
    if rsi is None:
        return None, False
    hit = _rsi_leg_ok(row, max_lvl) and _pctr_leg_ok(row, pctr_lo, pctr_hi)
    return rsi, hit


def rsi_focus_empty_reason(row: dict) -> str:
    """Why the Setup cell is blank: 'untracked' | 'pending' | '' (has value)."""
    if not _sp(row):
        return "untracked"
    if _cm_rsi_value(row) is None:
        return "pending"
    return ""


def _pctr_pair(row: dict) -> tuple[float | None, float | None]:
    """(fast %R, slow %R) from signal_proximity."""
    sp = _sp(row)
    return _fnum(sp.get("pctr")), _fnum(sp.get("pctr_slow"))


def _setup_readout(rsi: float,
                   fast: float | None,
                   slow: float | None) -> str:
    """Compact combined readout: RSI · fast/slow %R (omit missing %R)."""
    # e.g. "3/−99/−77" or "17/−96" or "17"
    parts = [f"{rsi:.0f}"]
    if fast is not None and slow is not None:
        parts.append(f"{fast:.0f}/{slow:.0f}")
    elif fast is not None:
        parts.append(f"{fast:.0f}")
    elif slow is not None:
        parts.append(f"—/{slow:.0f}")
    return "·".join(parts) if len(parts) > 1 else parts[0]


def _rsi_focus_cell(row: dict,
                    max_lvl: float = 35.0,
                    pctr_lo: float = -100.0,
                    pctr_hi: float = -75.0) -> str:
    """Rich markup for the Setup column (combined CM RSI + %R cue).

    Empty:
      —   engine not tracking
      …   tracked, waiting on bars / CM RSI
    Partial (no FOCUS):
      dim  17·−96/−40   RSI + both %R (full setup not ready)
      dim  17           RSI only (%R not published yet)
    Full setup:
      FOCUS  3·−99/−77  both legs true — number is RSI·fast/slow %R
    """
    rsi, hit = rsi_focus_trigger(row, max_lvl, pctr_lo, pctr_hi)
    if rsi is None:
        reason = rsi_focus_empty_reason(row)
        if reason == "pending":
            return "[dim]…[/dim]"
        return "[dim]—[/dim]"
    fast, slow = _pctr_pair(row)
    readout = _setup_readout(rsi, fast, slow)
    if hit:
        return (f"[bold black on green] FOCUS [/] "
                f"[bold green]{readout}[/bold green]")
    return f"[dim]{readout}[/dim]"


# ── alerting ─────────────────────────────────────────────────────────────────

def _beep():
    if sys.platform == "win32":
        try:
            import winsound
            winsound.Beep(880, 180)
        except Exception:                                  # noqa: BLE001
            pass


# Maps $TERM_PROGRAM (set by the terminal running this script) to the process
# name macOS System Events reports for that app's frontmost check, and to the
# bundle id terminal-notifier needs to activate that app on notification click.
_TERM_APP_NAMES = {
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm2",
}
_TERM_BUNDLE_IDS = {
    "Apple_Terminal": "com.apple.Terminal",
    "iTerm.app": "com.googlecode.iterm2",
}
_NOTIFY_GROUP = "brasfield-momentum"


def _macos_notify(title: str, message: str, sound: bool = True,
                   auto_dismiss: float = 5.0) -> None:
    """Notification Center banner. Prefers `terminal-notifier` (brew) so
    clicking the banner brings the monitor's terminal to the front via
    `-activate`; plain `osascript display notification` can't do that — Apple
    doesn't expose a click action to scripts, only to full app bundles.
    Falls back to osascript (no click action) if terminal-notifier is absent.

    `auto_dismiss` force-removes the banner after N seconds via a background
    timer + `-remove`, regardless of the system's Banners/Alerts style —
    macOS's own auto-hide timing isn't user- or script-configurable, so this
    is the only way to guarantee a specific duration."""
    if sys.platform != "darwin":
        return

    tn = shutil.which("terminal-notifier")
    if tn:
        cmd = [tn, "-title", title, "-message", message,
               "-group", _NOTIFY_GROUP]
        if sound:
            cmd += ["-sound", "default"]
        bundle_id = _TERM_BUNDLE_IDS.get(os.environ.get("TERM_PROGRAM", ""))
        if bundle_id:
            cmd += ["-activate", bundle_id]
        try:
            subprocess.run(cmd, check=False, timeout=5,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if auto_dismiss and auto_dismiss > 0:
                def _dismiss():
                    subprocess.run([tn, "-remove", _NOTIFY_GROUP], check=False,
                                    timeout=5, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
                timer = threading.Timer(auto_dismiss, _dismiss)
                timer.daemon = True
                timer.start()
            return
        except Exception:                                  # noqa: BLE001
            pass  # fall through to osascript

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{esc(message)}" with title "{esc(title)}"'
    if sound:
        script += ' sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:                                      # noqa: BLE001
        pass


def _monitor_visible() -> bool:
    """True if the terminal app running this monitor is currently the
    frontmost (focused) app — i.e. the user is already looking at it, so a
    notification would be redundant. False (assume hidden) if we can't tell,
    so notifications fail open rather than going silent."""
    if sys.platform != "darwin":
        return False
    app_name = _TERM_APP_NAMES.get(os.environ.get("TERM_PROGRAM", ""))
    if not app_name:
        return False
    script = ('tell application "System Events" to get name of '
              'first application process whose frontmost is true')
    try:
        out = subprocess.run(["osascript", "-e", script], check=False,
                              timeout=5, capture_output=True, text=True)
        return out.stdout.strip() == app_name
    except Exception:                                      # noqa: BLE001
        return False


class Alerter:
    """Beep + optional desktop toast, with a per-symbol, per-kind cooldown so
    a symbol that keeps re-bursting doesn't machine-gun the speaker, plus a
    separate global throttle on OS notifications so a burst of different
    symbols/kinds still caps out at one popup per `alert_notify_interval`."""

    def __init__(self, cfg: dict):
        self.cooldown = float(cfg.get("alert_cooldown", 60.0))
        self.toast = bool(cfg.get("desktop_toast", True))
        self.notify_interval = float(cfg.get("alert_notify_interval", 180.0))
        self.notify_duration = float(cfg.get("alert_notify_duration", 5.0))
        self.only_when_hidden = bool(cfg.get("alert_only_when_hidden", True))
        self._last_notify = 0.0
        self._last: dict[tuple[str, str], float] = {}
        self.recent: list[str] = []          # short on-screen alert log

    def fire(self, kind: str, sym: str, detail: str = ""):
        now = time.time()
        key = (kind, sym)
        if now - self._last.get(key, 0.0) < self.cooldown:
            return
        self._last[key] = now
        _beep()
        msg = {"new": f"NEW {sym}", "burst": f"BURST {sym}",
               "buy": f"BUY {sym}", "st_new": f"NEW-ST {sym}",
               "st_look": f"LOOK {sym}"}.get(kind, f"{kind} {sym}")
        if detail:
            msg += f"  {detail}"
        self.recent.insert(0, f"{datetime.now():%H:%M:%S}  {msg}")
        del self.recent[6:]
        if (self.toast and (now - self._last_notify) >= self.notify_interval
                and not (self.only_when_hidden and _monitor_visible())):
            self._last_notify = now
            if sys.platform == "darwin":
                _macos_notify(f"Momentum · {msg.split()[0]} {sym}",
                               detail or "momentum alert",
                               auto_dismiss=self.notify_duration)
            elif _plyer is not None:
                try:
                    _plyer.notify(title=f"Momentum · {msg.split()[0]} {sym}",
                                  message=detail or "momentum alert",
                                  timeout=6)
                except Exception:                          # noqa: BLE001
                    pass


# ── model ────────────────────────────────────────────────────────────────────

class Feed:
    """Turns successive /api/state snapshots into a display-ordered list and
    the new-symbol / burst rising edges that drive alerts. Newest symbols
    ride on top for `new_ttl` seconds, then settle into the server's
    mention-rank order."""

    def __init__(self, cfg: dict):
        self.new_ttl = float(cfg.get("new_ttl", 120.0))
        self.first_seen: dict[str, float] = {}
        self.prev_burst: dict[str, bool] = {}
        self.prev_buy: dict[str, bool] = {}
        self.seeded = False                # suppress alerts on the first poll
        self.rows: list[dict] = []
        self.last_ok = 0.0

    def ingest(self, state: dict, now: float, alerter: Alerter, cfg: dict):
        rows = list(state.get("tickers") or [])
        self.last_ok = now
        for r in rows:
            sym = str(r.get("ticker") or "").upper()
            if not sym:
                continue
            burst = bool(r.get("mention_burst"))
            buy = _is_buy(r)
            is_new = sym not in self.first_seen
            if is_new:
                self.first_seen[sym] = now
            if self.seeded:
                if is_new and cfg.get("alert_new"):
                    alerter.fire("new", sym, _detail(r))
                if (burst and not self.prev_burst.get(sym)
                        and cfg.get("alert_burst")):
                    alerter.fire("burst", sym,
                                 f"{r.get('mention_window', '?')} in window")
                if (buy and not self.prev_buy.get(sym)
                        and cfg.get("alert_buy")):
                    alerter.fire("buy", sym, _detail(r))
            self.prev_burst[sym] = burst
            self.prev_buy[sym] = buy

        # newest first_seen on top; server order (mention rank) breaks ties
        # via stable sort. Symbols no longer in the feed drop off the list.
        order = {r.get("ticker", "").upper(): i for i, r in enumerate(rows)}
        rows.sort(key=lambda r: (-self.first_seen.get(
            (r.get("ticker") or "").upper(), 0.0),
            order.get((r.get("ticker") or "").upper(), 999)))
        self.rows = rows
        self.seeded = True

    def is_fresh(self, sym: str, now: float) -> bool:
        return now - self.first_seen.get(sym, 0.0) <= self.new_ttl


def _detail(row: dict) -> str:
    px = row.get("price")
    chg = row.get("pct_change")
    bits = []
    if px is not None:
        bits.append(f"{px:.2f}")
    if chg is not None:
        bits.append(f"{chg:+.1f}%")
    return "  ".join(bits)


# ── render ───────────────────────────────────────────────────────────────────

def _fmt(v, spec, none="—"):
    return format(v, spec) if v is not None else none


def momentum_table(feed: Feed, now: float, hz: float,
                   hotkeys_on: bool,
                   rsi_focus_max: float = 35.0,
                   pctr_focus_lo: float = -100.0,
                   pctr_focus_hi: float = -75.0,
                   st_rank: dict[str, int] | None = None) -> Table:
    t = Table(expand=False)
    t.add_column("#", justify="right", style="bold")
    t.add_column("Symbol")
    t.add_column("Added", justify="right")
    t.add_column("Price", justify="right")
    t.add_column("Chg%", justify="right")
    t.add_column("Mentions", justify="right")
    # Combined CM RSI + %R deep-OS cue (not RSI alone)
    t.add_column("Setup", justify="right")
    t.add_column("")
    st_rank = st_rank or {}
    for i, r in enumerate(feed.rows):
        sym = str(r.get("ticker") or "?").upper()
        key = str(i + 1) if (hotkeys_on and i < 9) else ""
        seen = feed.first_seen.get(sym)
        added = (f"{datetime.fromtimestamp(seen):%H:%M:%S}"
                 if seen is not None else "—")
        chg = r.get("pct_change")
        cc = ("green" if (chg or 0) > 0 else "red" if (chg or 0) < 0
              else "white")
        win = r.get("mention_window") or 0
        day = r.get("mention_count") or 0
        mtxt = f"{win}/{day}" if (win or day) else "—"
        flags = []
        if r.get("find_it_first"):
            flags.append("[bold black on green]🥇FIRST[/]")
        if feed.is_fresh(sym, now):
            flags.append("[bold black on cyan] NEW [/]")
        if r.get("mention_burst"):
            flags.append("[bold black on yellow]🔥BURST[/]")
        conf = r.get("confluence")
        if isinstance(conf, dict) and conf.get("count", 0) >= 2:
            flags.append(f"[magenta]⚡{conf['count']}[/magenta]")
        # Stocktwits trending rank (same list as stocktwits.com/sentiment)
        rk = st_rank.get(sym)
        if rk is not None:
            flags.append(f"[bold black on magenta] ST#{rk} [/]")
        t.add_row(
            key,
            f"[bold cyan]{sym}[/bold cyan]",
            f"[dim]{added}[/dim]",
            _fmt(r.get("price"), ".2f"),
            f"[{cc}]{_fmt(chg, '+.1f')}[/{cc}]" if chg is not None else "—",
            mtxt,
            _rsi_focus_cell(r, rsi_focus_max, pctr_focus_lo, pctr_focus_hi),
            " ".join(flags),
        )
    if not feed.rows:
        t.add_row("", "[dim]no momentum tickers in the feed[/dim]",
                  "", "", "", "", "", "")
    return t


def stocktwits_panel(st: StocktwitsTrending,
                     price_by_sym: dict[str, float | None],
                     limit: int = 10,
                     hotkeys_on: bool = True) -> Panel:
    """Stocktwits trending — website columns + letter key (A-J) + LOOK badge."""
    from stocktwits_trending import fmt_mcap, fmt_vol

    rows = st.display_rows(price_by_sym, limit=min(limit, len(DeskHotkeys.ST_LETTERS)))
    t = Table(expand=False)
    t.add_column("Key", justify="right", style="bold")
    t.add_column("ST#", justify="right", style="bold magenta")
    t.add_column("Symbol")
    t.add_column("Last", justify="right")
    t.add_column("%Chg", justify="right")
    t.add_column("Volume", justify="right")
    t.add_column("52w Hi", justify="right")
    t.add_column("52w Lo", justify="right")
    t.add_column("Mkt Cap", justify="right")
    t.add_column("Score", justify="right")
    t.add_column("")  # LOOK badge
    n_look = 0
    if not rows:
        msg = st.error or "waiting for first poll…"
        t.add_row("", "—", f"[dim]{msg}[/dim]", "", "", "", "", "", "", "", "")
    else:
        for i, r in enumerate(rows):
            letter = (DeskHotkeys.ST_LETTERS[i].upper()
                      if hotkeys_on and i < len(DeskHotkeys.ST_LETTERS) else "")
            px = r.get("price")
            px_s = f"${px:.2f}" if px is not None else "[dim]—[/dim]"
            chg = r.get("pct_change")
            if chg is None:
                chg_s = "[dim]—[/dim]"
            else:
                cc = "green" if chg >= 0 else "red"
                chg_s = f"[{cc}]{chg:+.2f}%[/{cc}]"
            hi = r.get("high_52w")
            lo = r.get("low_52w")
            sc = r.get("trending_score")
            vol = r.get("volume") if r.get("volume") is not None else r.get("avg_vol")
            look_s = ""
            if r.get("look"):
                n_look += 1
                reason = r.get("look_reason") or ""
                look_s = f"[bold black on green] LOOK {reason} [/]"
            sym_s = f"[bold cyan]{r['symbol']}[/bold cyan]"
            if r.get("look"):
                sym_s = f"[bold green]{r['symbol']}[/bold green]"
            t.add_row(
                letter,
                str(r.get("rank") or "—"),
                sym_s,
                px_s,
                chg_s,
                fmt_vol(vol),
                f"${hi:.2f}" if hi is not None else "—",
                f"${lo:.2f}" if lo is not None else "—",
                fmt_mcap(r.get("market_cap")),
                f"{sc:.1f}" if sc is not None else "—",
                look_s,
            )
    age = ""
    if st.last_ok:
        age = f"  ·  {datetime.fromtimestamp(st.last_ok):%H:%M:%S}"
    cap = (f"  ·  max ${st.max_price:g}" if st.max_price is not None else "")
    look_n = f"  ·  {n_look} LOOK" if n_look else ""
    title = f"TRENDING  ·  A-J load TV{cap}{look_n}{age}"
    return Panel(t, title=title, title_align="left",
                 border_style="magenta", padding=(0, 1))


def header_panel(feed: Feed, now: float, hz: float,
                 stale: bool) -> Panel:
    n = len(feed.rows)
    src = _dashboard_url()
    if stale:
        line = "[bold white on red]  FEED DOWN  [/]  " \
               f"[dim]can't reach {src}[/dim]"
    else:
        line = (f"[bold cyan]Brasfield Momentum[/bold cyan]   "
                f"[green]{n}[/green] symbols   "
                f"[dim]{hz:.1f} polls/s · {src}[/dim]")
    if session_line:
        line += f"   [dim]{session_line()}[/dim]"
    return Panel(Align.center(line), border_style="red" if stale else "cyan",
                 padding=(0, 1))


def positions_panel(positions: dict, focus: str | None) -> Panel:
    """Live P&L for every open Alpaca position. Focused symbol highlighted."""
    total_pl = sum(float(p.get("pl") or 0) for p in positions.values())
    tcol = "green" if total_pl >= 0 else "red"
    t = Table.grid(expand=False, padding=(0, 2))
    t.add_column(justify="left")     # symbol
    t.add_column(justify="right")    # qty
    t.add_column(justify="right")    # entry
    t.add_column(justify="right")    # last
    t.add_column(justify="right")    # P&L $
    t.add_column(justify="right")    # P&L %
    t.add_column(justify="right")    # mkt value
    for sym in sorted(positions):
        p = positions[sym]
        pl, plpc = float(p.get("pl") or 0), float(p.get("plpc") or 0)
        c = "green" if pl >= 0 else "red"
        marker = "▶ " if focus and sym == focus.upper() else "  "
        name = f"[bold cyan]{marker}{sym}[/bold cyan]"
        t.add_row(
            name,
            f"[white]{p.get('qty', 0):g} sh[/white]",
            f"[dim]entry[/dim] ${p.get('avg_entry', 0):.2f}",
            f"[dim]last[/dim] ${p.get('current', 0):.2f}",
            f"[{c}]{'+' if pl >= 0 else ''}${pl:,.2f}[/{c}]",
            f"[{c}]{'+' if plpc >= 0 else ''}{plpc:.2f}%[/{c}]",
            f"[dim]${p.get('mkt_val', 0):,.0f}[/dim]",
        )
    title = (f"POSITIONS ({len(positions)})   "
             f"total P&L [{tcol}]{'+' if total_pl >= 0 else ''}${total_pl:,.2f}[/{tcol}]")
    return Panel(t, title=title, title_align="left",
                 border_style=tcol, padding=(0, 1))


def footer_panel(alerter: Alerter, hotkeys: DeskHotkeys,
                 hotkey_slots: int) -> Panel:
    lines = []
    if alerter.recent:
        lines.append("[dim]" + "   ·   ".join(alerter.recent[:3]) + "[/dim]")
    focus = hotkeys.focus_symbol() or "—"
    lines.append(
        f"[bold]FOCUS[/bold] [bold cyan]{focus}[/bold cyan]   "
        f"TV={'on' if desk.tv_load_available() else 'off'}   "
        f"[{desk.platform_label()}]"
    )
    if hotkeys.enabled:
        hint = (f"[dim]1-{hotkey_slots}/space: momentum → TV   ·   "
                f"A-J: Stocktwits → TV[/dim]")
        st = hotkeys.status()
        if st:
            hint += f"     [bold green]{st}[/bold green]"
        lines.append(hint)
    else:
        lines.append("[dim]hotkeys off — use a real Terminal on macOS/Windows[/dim]")
    return Panel(Align.center("\n".join(lines)), border_style="grey37",
                 padding=(0, 1))


# ── main ─────────────────────────────────────────────────────────────────────

STALE_AFTER = 15.0   # seconds without a good poll before the FEED DOWN banner


def main():
    # UTF-8 console (Windows cmd defaults break badge glyphs)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8")           # type: ignore[attr-defined]
    cfg = load_config()
    interval = float(cfg.get("poll_interval", 2.0))
    hotkey_slots = min(9, int(cfg.get("hotkey_slots", 9)))
    rsi_focus_max = float(cfg.get("rsi_focus_max", 35.0))
    pctr_focus_lo = float(cfg.get("pctr_focus_lo", -100.0))
    pctr_focus_hi = float(cfg.get("pctr_focus_hi", -75.0))
    st_on = bool(cfg.get("stocktwits_enabled", True))
    st_poll = float(cfg.get("stocktwits_poll", 60.0))
    st_max_px = cfg.get("stocktwits_max_price", 30.0)
    st_limit = min(10, int(cfg.get("stocktwits_panel_limit", 10)))

    console = Console()
    feed = Feed(cfg)
    alerter = Alerter(cfg)

    def _st_alert(kind: str, sym: str, detail: str) -> None:
        flag = "alert_st_new" if kind == "st_new" else "alert_st_look"
        if cfg.get(flag, True):
            alerter.fire(kind, sym, detail)

    hotkeys = DeskHotkeys()
    st = StocktwitsTrending(
        poll_interval=st_poll,
        stocks_only=bool(cfg.get("stocktwits_stocks_only", True)),
        max_price=float(st_max_px) if st_max_px is not None else None,
        look_min_abs_chg=float(cfg.get("stocktwits_look_min_abs_chg", 3.0)),
        look_max=int(cfg.get("stocktwits_look_max", 2)),
        look_near_high=float(cfg.get("stocktwits_look_near_high", 0.70)),
        look_near_low=float(cfg.get("stocktwits_look_near_low", 0.30)),
    ) if st_on else None

    st_note = f"  ST={'on' if st_on else 'off'}"
    console.print(
        f"[bold]Momentum desk[/bold]  {desk.platform_label()}  "
        f"TV={'on' if desk.tv_load_available() else 'off'}{st_note}  "
        f"— Ctrl+C to stop.\n"
        f"Polling {_dashboard_url()}/api/state"
        + (f"  ·  Stocktwits every {st_poll:.0f}s\n" if st_on else "\n")
        + f"[dim]1-9/space momentum → TV   ·   A-J Stocktwits → TV[/dim]"
    )

    stamps: list[float] = []
    with Live(console=console, refresh_per_second=2, screen=False) as live:
        while True:
            t0 = time.time()
            state = fetch_state()
            if state is not None:
                feed.ingest(state, t0, alerter, cfg)
            stale = (t0 - feed.last_ok) > STALE_AFTER if feed.last_ok else \
                    (state is None)
            stamps.append(t0)
            stamps[:] = [x for x in stamps if t0 - x <= 5]
            hz = len(stamps) / 5.0

            if st is not None:
                st.refresh(t0)

            # Prices from momentum feed (fallback for ST filter)
            price_map: dict[str, float | None] = {}
            for r in feed.rows:
                s = str(r.get("ticker") or "").upper()
                if s:
                    try:
                        price_map[s] = float(r["price"]) if r.get("price") is not None else None
                    except (TypeError, ValueError):
                        price_map[s] = None

            st_rank = {}
            st_ordered: list[str] = []
            if st is not None:
                st_rank = {sym: int(row["rank"])
                           for sym, row in st.by_symbol.items()
                           if row.get("rank") is not None}
                st_ordered = [r["symbol"] for r in
                              st.display_rows(price_map, limit=st_limit,
                                              on_change=_st_alert)]

            ordered = [str(r.get("ticker") or "").upper()
                       for r in feed.rows[:hotkey_slots]]
            hotkeys.update(ordered, st_ordered if st_on else None)

            panels = [header_panel(feed, t0, hz, stale)]
            panels.append(momentum_table(
                feed, t0, hz, hotkeys.enabled,
                rsi_focus_max, pctr_focus_lo, pctr_focus_hi, st_rank))
            if st is not None:
                panels.append(stocktwits_panel(
                    st, price_map, limit=st_limit, hotkeys_on=hotkeys.enabled))
            panels.append(footer_panel(alerter, hotkeys, hotkey_slots))
            live.update(Group(*panels))
            time.sleep(max(0.0, interval - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
