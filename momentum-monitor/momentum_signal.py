"""Momentum desk monitor — watchlist + TradingView load + Alpaca trade keys.

Polls the dashboard /api/state feed and shows live momentum tickers.
RSI column (reuses former Signal slot): green FOCUS when CM RSI-2 is in the
TV green-long band [0, rsi_focus_max) — data from signal_proximity only
(signal engine); no list reorder, no local Alpaca bar fetch.

Cross-platform (macOS + Windows):

  1-9     focus that row + load TradingView
  SPACE   focus newest + load TradingView
  B       buy focused symbol (Alpaca paper/live via TRADER_MODE)
  S       sell / close focused symbol

Focused symbol is written to repo root active_symbol.json.

Run (from repo root or this folder):
  python3 momentum-monitor/momentum_signal.py
  python3 momentum_signal.py
"""
from __future__ import annotations

import contextlib
import json
import sys
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

WatchlistReader = top_movers = None  # OCR movers retired

CONFIG_PATH = HERE / "momentum_config.json"

DEFAULTS = {
    "poll_interval": 2.0,
    "hotkey_slots": 9,
    "new_ttl": 120.0,
    "alert_new": True,
    "alert_burst": True,
    "alert_buy": False,
    "alert_cooldown": 60.0,
    "desktop_toast": True,
    "watchlist_enabled": False,
    "trade_enabled": True,       # B/S keys → Alpaca
    "trader_mode": None,          # override TRADER_MODE from env (paper|live|off)
    "trade_amount": None,        # override TRADE_AMOUNT (dollars per buy)
    "buy_order_style": "auto",   # "auto" (mkt when open, limit off-hours) | "limit_ask" | "market"; all whole shares only
    "limit_pad_pct": 0.1,        # % above ask (buys) / below bid (ext-hours sells)
    "extended_hours": True,      # allow pre/post-market B/S
    "positions_refresh": 3.0,    # seconds between live P&L refreshes
    # CM RSI-2 green long zone (TV bottom spike): value in [0, rsi_focus_max)
    "rsi_focus_max": 35.0,
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


def _cm_rsi_value(row: dict) -> float | None:
    """CM RSI-2 (preferred) or classic RSI from signal_proximity, if the engine
    is publishing it. No local Alpaca fetch — blank when the engine is off."""
    sp = row.get("signal_proximity")
    if not isinstance(sp, dict):
        return None
    for key in ("cm_rsi", "rsi"):
        v = sp.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def rsi_focus_trigger(row: dict, max_lvl: float = 35.0) -> tuple[float | None, bool]:
    """Green long zone at the bottom of the CM RSI pane: value from 0 up to
    (but not including) max_lvl (default 35). Returns (rsi, is_trigger)."""
    rsi = _cm_rsi_value(row)
    if rsi is None:
        return None, False
    # Match the TV green long marker: deep oversold band toward 0
    triggered = 0.0 <= rsi < float(max_lvl)
    return rsi, triggered


def rsi_focus_empty_reason(row: dict) -> str:
    """Why the RSI cell is blank: 'untracked' | 'pending' | '' (has value)."""
    sp = row.get("signal_proximity")
    if not isinstance(sp, dict):
        return "untracked"
    if _cm_rsi_value(row) is None:
        return "pending"
    return ""


def _rsi_focus_cell(row: dict, max_lvl: float = 35.0) -> str:
    """Rich markup for the RSI focus column (reuses former Signal column).

    Empty states (distinct so pipeline gaps are obvious):
      —   engine not tracking this symbol (no signal_proximity)
      …   tracked, waiting on bars / CM RSI
      N   RSI value outside focus zone
      FOCUS N  CM RSI in green-long band [0, max)
    """
    rsi, hit = rsi_focus_trigger(row, max_lvl)
    if rsi is None:
        reason = rsi_focus_empty_reason(row)
        if reason == "pending":
            return "[dim]…[/dim]"
        return "[dim]—[/dim]"
    if hit:
        # Sharp green long — focus this symbol
        return f"[bold black on green] FOCUS [/] [bold green]{rsi:.0f}[/bold green]"
    return f"[dim]{rsi:.0f}[/dim]"


# ── alerting ─────────────────────────────────────────────────────────────────

def _beep():
    if sys.platform == "win32":
        try:
            import winsound
            winsound.Beep(880, 180)
        except Exception:                                  # noqa: BLE001
            pass


class Alerter:
    """Beep + optional desktop toast, with a per-symbol, per-kind cooldown so
    a symbol that keeps re-bursting doesn't machine-gun the speaker."""

    def __init__(self, cfg: dict):
        self.cooldown = float(cfg.get("alert_cooldown", 60.0))
        self.toast = bool(cfg.get("desktop_toast", True))
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
               "buy": f"BUY {sym}"}.get(kind, f"{kind} {sym}")
        if detail:
            msg += f"  {detail}"
        self.recent.insert(0, f"{datetime.now():%H:%M:%S}  {msg}")
        del self.recent[6:]
        if self.toast and _plyer is not None:
            try:
                _plyer.notify(title=f"Momentum · {msg.split()[0]} {sym}",
                              message=detail or "momentum alert",
                              timeout=6)
            except Exception:                              # noqa: BLE001
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
                   rsi_focus_max: float = 35.0) -> Table:
    t = Table(expand=False)
    t.add_column("#", justify="right", style="bold")
    t.add_column("Symbol")
    t.add_column("Added", justify="right")
    t.add_column("Price", justify="right")
    t.add_column("Chg%", justify="right")
    t.add_column("Mentions", justify="right")
    # Reuses the former Signal column: CM RSI green-long focus cue (RSI < max)
    t.add_column("RSI", justify="right")
    t.add_column("")
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
        t.add_row(
            key,
            f"[bold cyan]{sym}[/bold cyan]",
            f"[dim]{added}[/dim]",
            _fmt(r.get("price"), ".2f"),
            f"[{cc}]{_fmt(chg, '+.1f')}[/{cc}]" if chg is not None else "—",
            mtxt,
            _rsi_focus_cell(r, rsi_focus_max),
            " ".join(flags),
        )
    if not feed.rows:
        t.add_row("", "[dim]no momentum tickers in the feed[/dim]",
                  "", "", "", "", "", "")
    return t


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
                 hotkey_slots: int, trader_mode: str,
                 trade_amount: float) -> Panel:
    lines = []
    if alerter.recent:
        lines.append("[dim]" + "   ·   ".join(alerter.recent[:3]) + "[/dim]")
    focus = hotkeys.focus_symbol() or "—"
    mode_c = {"paper": "yellow", "live": "red", "off": "dim"}.get(
        trader_mode, "dim")
    style = {"auto": "auto mkt/lim", "limit_ask": "limit@ask", "market": "market"}.get(
        desk.buy_style(), desk.buy_style())
    ext = "  EXT" if desk.extended_hours() else ""
    lines.append(
        f"[bold]FOCUS[/bold] [bold cyan]{focus}[/bold cyan]   "
        f"Alpaca [{mode_c}]{trader_mode.upper()}[/{mode_c}]   "
        f"${trade_amount:.0f}/buy {style}{ext}   [{desk.platform_label()}]"
    )
    if hotkeys.enabled:
        hint = (f"[dim]1-{hotkey_slots}/space: load TV + focus   ·   "
                f"T: focus = TV chart   ·   B: buy   ·   S: sell[/dim]")
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
    trade_on = bool(cfg.get("trade_enabled", True))
    rsi_focus_max = float(cfg.get("rsi_focus_max", 35.0))

    # Alpaca paper/live for B/S keys
    trade_cfg = {}
    if cfg.get("trader_mode"):
        trade_cfg["trader_mode"] = cfg["trader_mode"]
    if cfg.get("trade_amount") is not None:
        trade_cfg["trade_amount"] = cfg["trade_amount"]
    if cfg.get("buy_order_style"):
        trade_cfg["buy_order_style"] = cfg["buy_order_style"]
    if cfg.get("limit_pad_pct") is not None:
        trade_cfg["limit_pad_pct"] = cfg["limit_pad_pct"]
    if cfg.get("extended_hours") is not None:
        trade_cfg["extended_hours"] = cfg["extended_hours"]
    mode = desk.init_trader(trade_cfg) if trade_on else "off"
    amount = desk.trade_amount()

    console = Console()
    feed = Feed(cfg)
    alerter = Alerter(cfg)
    hotkeys = DeskHotkeys(trade_enabled=trade_on and mode != "off")

    _style = {"auto": "auto mkt/lim", "limit_ask": "limit@ask", "market": "market"}.get(
        desk.buy_style(), desk.buy_style())
    _ext = " +EXT" if desk.extended_hours() else ""
    console.print(
        f"[bold]Momentum desk[/bold]  {desk.platform_label()}  "
        f"Alpaca [bold]{mode.upper()}[/bold]  ${amount:.0f}/buy {_style}{_ext}  "
        f"TV={'on' if desk.tv_load_available() else 'off'}  "
        f"— Ctrl+C to stop.\n"
        f"Polling {_dashboard_url()}/api/state\n"
        f"[dim]1-9/space load TV · T focus=TV chart · B buy · S sell[/dim]"
    )
    if mode == "live":
        console.print("[bold red]LIVE money enabled — B/S place real orders[/bold red]")

    stamps: list[float] = []
    pos_on = trade_on and mode != "off"
    pos_refresh = float(cfg.get("positions_refresh", 3.0))
    pos_cache: dict = {"ts": 0.0, "data": {}}
    with Live(console=console, refresh_per_second=2, screen=False) as live:
        while True:
            t0 = time.time()
            state = fetch_state()
            if state is not None:
                feed.ingest(state, t0, alerter, cfg)
                ordered = [str(r.get("ticker") or "").upper()
                           for r in feed.rows[:hotkey_slots]]
                hotkeys.update(ordered)
            stale = (t0 - feed.last_ok) > STALE_AFTER if feed.last_ok else \
                    (state is None)
            stamps.append(t0)
            stamps[:] = [x for x in stamps if t0 - x <= 5]
            hz = len(stamps) / 5.0

            # Live P&L for open positions (throttled Alpaca call)
            if pos_on and (t0 - pos_cache["ts"] >= pos_refresh):
                d = desk.positions_detail()
                if d is not None:                 # None = call failed; keep last good
                    pos_cache["data"] = d
                pos_cache["ts"] = t0

            panels = [header_panel(feed, t0, hz, stale)]
            if pos_cache["data"]:
                panels.append(positions_panel(pos_cache["data"],
                                              hotkeys.focus_symbol()))
            panels.append(momentum_table(feed, t0, hz, hotkeys.enabled,
                                         rsi_focus_max))
            panels.append(footer_panel(alerter, hotkeys, hotkey_slots, mode, amount))
            live.update(Group(*panels))
            time.sleep(max(0.0, interval - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
