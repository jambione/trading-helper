"""Momentum monitor — the Brasfield Momentum watchlist in your terminal.

Polls the dashboard's /api/state feed (the same data as
trading.jbrasfield.com) and shows the live momentum tickers so you don't
have to keep the webpage open in another tab. Newest momentum names sit on
top; a fresh arrival or a mention burst beeps and pops a desktop toast.

Press the number shown beside a symbol (1-9) to load that ticker into BOTH
TradingView and Webull Desktop, using the existing windows_agent workflows.
SPACE loads the top (newest) symbol.

The bottom strip shows the top movers OCR'd straight off the Webull
watchlist sidebar (symbol + (pre)market percent), scraped the same way the
L2 book and Time&Sales are — see watchlist_ocr.py. Needs the webull-l2 OCR
deps (cv2/mss/pytesseract) and the Webull window on screen; without them
the strip just says it's off.

Run:  python momentum_signal.py       (repo .venv has rich + plyer)

Windows-only for the hotkey / Webull-TV loading; on other platforms it still
renders the list, just without the load hotkeys.
"""
from __future__ import annotations

import contextlib
import io
import json
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

# windows_agent (repo root) owns the dashboard auth + the WB/TV load
# workflows. Import is side-effect-free (its server/listener only start under
# __main__), so we borrow the pieces we need. On a box without the automation
# deps the load hotkeys simply switch off and the list still renders.
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    import windows_agent as wa
    workflow_add_wb = wa.workflow_add_wb
    workflow_add_tv = wa.workflow_add_tv
except Exception:                                          # noqa: BLE001
    wa = None
    workflow_add_wb = workflow_add_tv = None

# shared session clock (repo root) — optional
try:
    from session_clock import session_line
except Exception:                                          # noqa: BLE001
    session_line = None

try:
    from plyer import notification as _plyer
except Exception:                                          # noqa: BLE001
    _plyer = None

# Webull watchlist scraper (same OCR stack as webull-l2); import never
# hard-fails — without cv2/mss/pytesseract the reader reports itself off.
try:
    from watchlist_ocr import WatchlistReader, top_movers
except Exception:                                          # noqa: BLE001
    WatchlistReader = top_movers = None

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "momentum_config.json"

# defaults — override any of these in momentum_config.json
DEFAULTS = {
    "poll_interval": 2.0,     # seconds between /api/state polls
    "hotkey_slots": 9,        # rows that get a load number (1-9 max)
    "new_ttl": 120.0,         # seconds a symbol wears the NEW badge / stays on top
    "alert_new": True,        # beep/toast when a symbol enters the list
    "alert_burst": True,      # beep/toast on a mention-burst rising edge
    "alert_buy": False,       # beep/toast when signal status hits buy-zone
    "alert_cooldown": 60.0,   # per-symbol, per-alert-type cooldown
    "desktop_toast": True,    # plyer desktop notification alongside the beep
    "watchlist_enabled": True,  # OCR the Webull watchlist sidebar for movers
    "watchlist_poll": 2.0,    # seconds between watchlist captures
    "watchlist_top": 3,       # movers shown in the bottom strip
    "watchlist_rank": "gainers",  # "gainers" (signed %) or "abs" (magnitude)
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


# ── hotkeys: load a symbol into TradingView + Webull ─────────────────────────

class LoadHotkey:
    """Press a slot number (1-9) to load that row's symbol into BOTH
    TradingView and Webull Desktop; SPACE loads the top (newest) row. A
    daemon thread reads keys and runs the load off the render loop (each
    load steals focus for ~2s and prints, which we swallow so the Rich
    display stays clean). Windows-only; a no-op if the workflows didn't
    import, in which case the on-screen hint is hidden."""

    SPACE = " "

    def __init__(self):
        self.enabled = (sys.platform == "win32"
                        and workflow_add_wb is not None
                        and workflow_add_tv is not None)
        self._by_key: dict[str, str] = {}
        self._top: str | None = None
        self._status = ""
        self._busy = False
        self._lock = threading.Lock()
        if self.enabled:
            threading.Thread(target=self._reader, daemon=True).start()

    def update(self, ordered: list[str]):
        """ordered = symbols in display order; key N -> ordered[N-1]."""
        with self._lock:
            self._by_key = {str(i + 1): s for i, s in enumerate(ordered[:9])}
            self._top = ordered[0] if ordered else None

    def status(self) -> str:
        with self._lock:
            return self._status

    def _set(self, msg: str):
        with self._lock:
            self._status = f"{datetime.now():%H:%M:%S}  {msg}"

    def _reader(self):
        try:
            import msvcrt
        except Exception:                                  # noqa: BLE001
            return
        while True:
            try:
                ch = msvcrt.getwch()
            except Exception:                              # noqa: BLE001
                return                                     # no real console
            with self._lock:
                if self._busy:
                    continue                               # ignore keys mid-load
                sym = (self._top if ch == self.SPACE
                       else self._by_key.get(ch))
                if sym:
                    self._busy = True
            if not sym:
                continue
            try:
                self._load(sym)
            finally:
                with self._lock:
                    self._busy = False

    def _load(self, sym: str):
        self._set(f"loading {sym} into TradingView + Webull …")
        wb_ok = tv_ok = False
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                wb_ok = bool(workflow_add_wb(sym))
                time.sleep(0.5)
                tv_ok = bool(workflow_add_tv(sym))
        except Exception as e:                             # noqa: BLE001
            self._set(f"load error for {sym}: {e}")
            return
        parts = []
        parts.append("WB✓" if wb_ok else "WB✗")
        parts.append("TV✓" if tv_ok else "TV✗")
        self._set(f"{sym}: {' '.join(parts)}")


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

_STATUS_LABEL = {
    "buy": "🔥 BUY", "buy_zone": "🔥 BUY ZONE", "watching": "watching",
    "approaching": "approaching", "on_deck": "on deck",
}


def _fmt(v, spec, none="—"):
    return format(v, spec) if v is not None else none


def momentum_table(feed: Feed, now: float, hz: float,
                   hotkeys_on: bool) -> Table:
    t = Table(expand=False)
    t.add_column("#", justify="right", style="bold")
    t.add_column("Symbol")
    t.add_column("Added", justify="right")
    t.add_column("Price", justify="right")
    t.add_column("Chg%", justify="right")
    t.add_column("Mentions", justify="right")
    t.add_column("Signal")
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
        st = _signal_status(r)
        st_lbl = _STATUS_LABEL.get((st or "").lower(), st or "")
        sc = ("green" if _is_buy(r) else "yellow" if st else "dim")
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
            f"[{sc}]{st_lbl}[/{sc}]" if st_lbl else "[dim]—[/dim]",
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


def footer_panel(alerter: Alerter, hotkeys: LoadHotkey,
                 hotkey_slots: int) -> Panel:
    lines = []
    if alerter.recent:
        lines.append("[dim]" + "   ·   ".join(alerter.recent[:3]) + "[/dim]")
    if hotkeys.enabled:
        hint = (f"[dim]keys 1-{hotkey_slots}: load that symbol into "
                "TradingView + Webull   ·   space: load the top symbol[/dim]")
        st = hotkeys.status()
        if st:
            hint += f"     [bold green]{st}[/bold green]"
        lines.append(hint)
    else:
        lines.append("[dim]load hotkeys off (needs Windows + windows_agent "
                     "automation deps)[/dim]")
    return Panel(Align.center("\n".join(lines)), border_style="grey37",
                 padding=(0, 1))


def movers_panel(snap: dict | None, now: float, top: int,
                 rank: str) -> Panel:
    """Bottom strip: top watchlist movers straight off the Webull sidebar."""
    label = "[bold magenta]WB watchlist[/bold magenta]"
    if snap is None or not snap.get("on"):
        body = (f"{label}   [dim]scraper off (needs the webull-l2 OCR deps: "
                "cv2 / mss / pytesseract)[/dim]")
    elif not snap["rows"]:
        why = ("waiting for the Webull window / watchlist header"
               if not snap["located"] else "no readable rows yet")
        body = f"{label}   [dim]{why}[/dim]"
    else:
        movers = top_movers(snap["rows"], top, rank)
        bits = []
        for i, m in enumerate(movers, 1):
            c = "green" if m.pct >= 0 else "red"
            px = f" [dim]{m.price:.2f}[/dim]" if m.price is not None else ""
            bits.append(f"[bold]{i}[/bold] [bold cyan]{m.sym}[/bold cyan] "
                        f"[{c}]{m.pct:+.1f}%[/{c}]{px}")
        pre = " (pre)" if any(m.pre for m in movers) else ""
        meta = f"{len(snap['rows'])} rows"
        age = (now - snap["updated"]) if snap.get("updated") else None
        if age is not None and age > 15:
            meta += f" · [yellow]{age:.0f}s stale[/yellow]"
        body = (f"{label}{pre}   " + "    ".join(bits)
                + f"   [dim]{meta}[/dim]")
    return Panel(Align.center(body), border_style="magenta", padding=(0, 1))


# ── main ─────────────────────────────────────────────────────────────────────

STALE_AFTER = 15.0   # seconds without a good poll before the FEED DOWN banner


def main():
    # the badges/arrows below are UTF-8; a classic cmd console defaults to
    # cp1252 and would crash Rich's writer, so force UTF-8 where possible.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8")           # type: ignore[attr-defined]
    cfg = load_config()
    interval = float(cfg.get("poll_interval", 2.0))
    hotkey_slots = min(9, int(cfg.get("hotkey_slots", 9)))
    console = Console()
    feed = Feed(cfg)
    alerter = Alerter(cfg)
    hotkeys = LoadHotkey()
    watchlist = (WatchlistReader(cfg, console)
                 if WatchlistReader and cfg.get("watchlist_enabled", True)
                 else None)
    wl_top = max(1, int(cfg.get("watchlist_top", 3)))
    wl_rank = str(cfg.get("watchlist_rank", "gainers"))

    console.print("[bold]Momentum monitor — Ctrl+C to stop.[/bold]  "
                  f"Polling {_dashboard_url()}/api/state")
    stamps: list[float] = []
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
            parts = [
                header_panel(feed, t0, hz, stale),
                momentum_table(feed, t0, hz, hotkeys.enabled),
                footer_panel(alerter, hotkeys, hotkey_slots),
            ]
            if cfg.get("watchlist_enabled", True):
                parts.append(movers_panel(
                    watchlist.snapshot() if watchlist else None,
                    t0, wl_top, wl_rank))
            live.update(Group(*parts))
            time.sleep(max(0.0, interval - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
