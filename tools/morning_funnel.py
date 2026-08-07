#!/usr/bin/env python3
"""Morning funnel — rank the watchlist, name the day's battlefield.

The monitors (tv-monitor) confirm entries and exits on ONE
symbol: whatever is on screen. This tool decides which symbol deserves
that slot. It pulls Alpaca bars for every candidate, hard-rejects names
your ~$3K can't trade cleanly (price band, spread, dollar volume), and
ranks the survivors by relative volume pace, gap/move, and chart
readiness (%R + VWAP — the same signals.py math the scanner uses).

Advisory only: it displays a ranked table. It never places orders.

Candidates come from (merged in this order, deduped):
  1. tickers on the command line
  2. config/funnel_watchlist.txt          (one per line, # comments)
  3. transcription/wb_watchlist.json      (live TV/Discord mentions)
  4. swing_candidates.json                (swing screener output)

Usage:
    .venv/Scripts/python.exe tools/morning_funnel.py HOLO SPRC ZCMD
    .venv/Scripts/python.exe tools/morning_funnel.py --once
Alpaca keys come from config/secrets.json (copy secrets.example.json)
or ALPACA_API_KEY / ALPACA_SECRET_KEY env vars.
"""
import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import load_config                              # noqa: E402

# These fetches feed day_vol, rvol and every funnel score. They used to return
# {} on any failure, so an expired key or a rate-limit rejection looked exactly
# like a quiet premarket — the panel showed 'no data' and nothing said why.
log = logging.getLogger(__name__)
from alpaca_api import connect_data_client, _get_feed_arg   # noqa: E402
from signals import williams_pr, vwap_calc                  # noqa: E402
from session_clock import ET, next_shot, session_window     # noqa: E402,F401

OPEN_MIN = 9 * 60 + 30          # 9:30 ET in minutes-of-day


# ── expected intraday volume curve ──────────────────────────────────────────
# Cumulative fraction of an average day's volume by minutes since the 9:30
# open (premarket baked into the first point). U-shaped day: heavy open,
# dead lunch, heavy close. Used to turn "volume so far" into a pace vs. a
# normal day — rvol 3.0 at 9:50 means 3x the usual pace for 9:50.
CURVE = [(0, 0.05), (15, 0.13), (30, 0.20), (60, 0.30), (90, 0.38),
         (120, 0.44), (150, 0.49), (180, 0.54), (240, 0.63), (300, 0.73),
         (330, 0.80), (360, 0.89), (390, 1.0)]


def expected_fraction(mins_since_open: float) -> float:
    if mins_since_open < 0:      # premarket: ramp 4:00 → 9:30 up to 5%
        return max(0.01, 0.05 * (mins_since_open + 330) / 330)
    if mins_since_open >= CURVE[-1][0]:
        return 1.0
    for (x0, y0), (x1, y1) in zip(CURVE, CURVE[1:]):
        if mins_since_open <= x1:
            return y0 + (y1 - y0) * (mins_since_open - x0) / (x1 - x0)
    return 1.0


def rvol_pair(vol_so_far, avg_vol, mins_since_open: float,
              time_adjusted: bool = True) -> tuple:
    """(rvol, rvol_raw) from today's volume so far — or (None, None).

    `vol_so_far` MUST be today's cumulative volume including pre-market, i.e.
    summed from minute bars fetched with extended_hours=True. Do not pass a
    daily-bar volume: before the regular session opens, the latest daily bar
    is *yesterday's completed total*, and dividing that by a fraction of a day
    reports a wildly high pace for a perfectly ordinary stock.

    `rvol_raw` is the naive full-day ratio (what the old code reported, minus
    the broken numerator). `rvol` divides the average by the share of a normal
    day's volume that should be done by now, so 3.0 means "3x the usual pace
    for this time of day" at any hour, pre-market included.

    Returns (None, None) rather than a placeholder whenever the inputs cannot
    support a real answer — no volume yet, or no average to compare against.
    """
    try:
        vol = float(vol_so_far)
        avg = float(avg_vol)
    except (TypeError, ValueError):
        return None, None
    if not (vol > 0) or not (avg > 0):
        return None, None

    raw = vol / avg
    if not time_adjusted:
        return raw, raw
    frac = expected_fraction(mins_since_open)
    if not (frac > 0):
        return raw, raw
    return vol / (avg * frac), raw


# ── candidate gathering ─────────────────────────────────────────────────────
def _clean(t) -> str:
    t = str(t or "").strip().upper()
    return t if 1 <= len(t) <= 5 and t.isalpha() else ""


def gather_candidates(cli_tickers: list, root: Path, max_n: int = 25) -> list:
    seen, out = set(), []

    def add(raw):
        t = _clean(raw)
        if t and t not in seen:
            seen.add(t)
            out.append(t)

    for t in cli_tickers:
        add(t)

    wl = root / "config" / "funnel_watchlist.txt"
    if wl.exists():
        for line in wl.read_text(encoding="utf-8").splitlines():
            for tok in line.split("#")[0].replace(",", " ").split():
                add(tok)

    wb = root / "transcription" / "wb_watchlist.json"
    if wb.exists():
        try:
            for e in json.loads(wb.read_text(encoding="utf-8")):
                if isinstance(e, dict):
                    add(e.get("ticker"))
        except Exception:
            pass

    sc = root / "swing_candidates.json"
    if sc.exists():
        try:
            data = json.loads(sc.read_text(encoding="utf-8"))
            for c in data.get("candidates", []):
                if isinstance(c, dict):
                    add(c.get("symbol") or c.get("ticker"))
        except Exception:
            pass

    return out[:max_n]


# ── knobs ───────────────────────────────────────────────────────────────────
def knobs_from_cfg(cfg: dict) -> dict:
    g = cfg.get
    return {
        "min_price":       float(g("funnel_min_price", 1.5)),
        "max_price":       float(g("funnel_max_price", 60.0)),
        "min_rvol":        float(g("funnel_min_rvol", 1.5)),
        "max_spread_pct":  float(g("funnel_max_spread_pct", 0.35)),
        "pm_max_spread_pct": float(g("funnel_pm_max_spread_pct", 1.0)),
        "min_dollar_min":  float(g("funnel_min_dollar_per_min", 25000.0)),
        "max_candidates":  int(g("funnel_max_candidates", 25)),
        "refresh_sec":     float(g("funnel_refresh_sec", 150.0)),
        "avg_days":        int(g("funnel_avg_days", 10)),
    }


# ── per-ticker evaluation (pure — no network, unit-testable) ────────────────
def _et_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    idx = df.index
    return idx.tz_convert(ET) if getattr(idx, "tz", None) is not None else idx


# Completed sessions needed before an average means anything. A fresh listing
# or a name halted all week gives a denominator that is noise.
MIN_AVG_SESSIONS = 5


def sum_by_session(minutes: pd.DataFrame | None) -> dict:
    """{et_date: total volume} from a minute-bar frame. Pure."""
    if minutes is None or minutes.empty or "volume" not in minutes:
        return {}
    out: dict = {}
    for d, v in zip(_et_index(minutes).date, minutes["volume"].to_numpy()):
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out[d] = out.get(d, 0.0) + f
    return out


def avg_session_volume(minutes: pd.DataFrame | None, today,
                       avg_days: int = 10,
                       min_sessions: int = MIN_AVG_SESSIONS) -> float | None:
    """Mean total minute-bar volume over completed sessions, or None.

    THE denominator for any rvol whose numerator is a minute-bar sum. A daily
    bar is NOT interchangeable with a day of summed minute bars, and the gap
    is not small. Measured on IEX over 7 sessions (2026-07-24):

        >1M shares/day  minute-sum / daily-bar = 1.000
        ~150-650K                               0.955-0.996
        ~70K                                    0.926
        ~20K                                    0.812
        ~13K                                    0.698   (worst session 0.475)
        ~2K                                     0.670

    The daily bar carries odd-lot and off-exchange prints that appear in no
    minute bar. So dividing a minute-sum numerator by a daily-bar average
    understates rvol by up to ~30% — worst on exactly the thin, low-float names
    this desk exists to find, and directly under `funnel_min_rvol`, where a
    true 2.0 reads 1.4 and the candidate is dropped.

    Today is excluded however partial it is: averaging a half-finished session
    in drags the denominator down all morning and inflates the rvol taken
    from it.
    """
    sessions = {d: v for d, v in sum_by_session(minutes).items() if d < today}
    if len(sessions) < max(1, int(min_sessions)):
        return None
    tail = [v for _, v in sorted(sessions.items())[-max(1, int(avg_days)):]]
    avg = sum(tail) / len(tail)
    return avg if avg > 0 else None


def evaluate(sym: str, daily: pd.DataFrame | None, minutes: pd.DataFrame | None,
             quote: tuple | None, now_et: datetime, knobs: dict,
             avg_vol: float | None = None) -> dict:
    """Score one candidate. Returns a row dict; row["rejects"] non-empty
    means it failed a hard tradeability filter and gets score 0.

    `avg_vol` must be the MINUTE-BASED average session volume — see
    `avg_session_volume` for why a daily-bar average cannot stand in for it.
    Without it no rvol is reported, rather than a figure biased low by as much
    as a third. `daily` is still used for prev_close, which is unaffected.
    """
    row = {"sym": sym, "price": None, "chg_pct": None, "gap_pct": None,
           "rvol": None, "spread_pct": None, "dollar_min": None,
           "wr": None, "state": "…", "rejects": [], "score": 0.0}

    today = now_et.date()
    mins_open = (now_et.hour * 60 + now_et.minute) - OPEN_MIN

    if daily is None or daily.empty:
        row["rejects"].append("no data")
        return row
    d_dates = pd.Series(_et_index(daily).date, index=daily.index)
    completed = daily[d_dates < today]
    if len(completed) < 5:
        row["rejects"].append("no data")
        return row
    prev_close = float(completed["close"].iloc[-1])

    if minutes is None or minutes.empty:
        row["rejects"].append("no data")
        return row
    m_et = _et_index(minutes)
    m = minutes[pd.Series(m_et.date, index=minutes.index) == today]
    if m.empty:
        row["rejects"].append("no data")
        return row
    m_et = _et_index(m)

    price = float(m["close"].iloc[-1])
    row["price"] = price
    row["chg_pct"] = (price / prev_close - 1) * 100

    sess = m[(m_et.hour * 60 + m_et.minute) >= OPEN_MIN]
    if len(sess):
        row["gap_pct"] = (float(sess["open"].iloc[0]) / prev_close - 1) * 100
    else:                                   # still premarket
        row["gap_pct"] = row["chg_pct"]

    # One implementation of the ratio, shared with the dashboard and the desk.
    row["rvol"], _ = rvol_pair(float(m["volume"].sum()), avg_vol, mins_open)
    # dollar volume per wall-clock minute over the last 15 min — bar-only
    # minutes (sparse premarket tape) must not inflate the pace
    recent = m[m_et >= now_et - timedelta(minutes=15)]
    row["dollar_min"] = float((recent["close"] * recent["volume"]).sum()) / 15.0

    if quote:
        bid, ask = quote
        if bid and ask and ask >= bid > 0:
            row["spread_pct"] = (ask - bid) / ((ask + bid) / 2) * 100

    # ── chart readiness: %R(14) + VWAP, same math the scanner trusts ──
    five = (m.resample("5min")
             .agg({"open": "first", "high": "max", "low": "min",
                   "close": "last", "volume": "sum"})
             .dropna(subset=["close"]))
    src = five if len(five) >= 15 else m
    if len(src) >= 14:
        wr = williams_pr(src["high"], src["low"], src["close"], 14).iloc[-1]
        if not pd.isna(wr):
            row["wr"] = float(wr)
    vw = vwap_calc(m).iloc[-1]
    above_vwap = not pd.isna(vw) and price >= float(vw)
    if row["wr"] is not None:
        if row["wr"] > -20:
            row["state"] = "EXTENDED"      # chasing — the move already ran
        elif row["wr"] >= -60:
            row["state"] = "READY" if above_vwap else "WEAK"
        else:
            row["state"] = "EARLY"         # washed out — wait for the turn
    elif not above_vwap:
        row["state"] = "WEAK"

    # ── hard tradeability rejects ──
    if not (knobs["min_price"] <= price <= knobs["max_price"]):
        row["rejects"].append("price")
    # premarket books are naturally wide — 0.35% would reject the whole list
    # at 7:00, so a looser ceiling applies until the open
    max_spread = (knobs.get("pm_max_spread_pct", 1.0) if mins_open < 0
                  else knobs["max_spread_pct"])
    if row["spread_pct"] is not None and row["spread_pct"] > max_spread:
        row["rejects"].append("spread")
    if mins_open >= 10:                    # premarket pace is too noisy to reject on
        if row["rvol"] is not None and row["rvol"] < knobs["min_rvol"]:
            row["rejects"].append("low rvol")
        if row["dollar_min"] < knobs["min_dollar_min"]:
            row["rejects"].append("illiquid")
    if row["rejects"]:
        return row

    # ── score 0-100: volume pace dominates, then move, readiness, liquidity ──
    v = min((row["rvol"] or 0) / 4.0, 1.0)
    x = abs(row["chg_pct"])
    if x < 2:
        mv = x / 2
    elif x <= 12:
        mv = 1.0
    else:
        mv = max(0.3, 1 - (x - 12) / 20)   # 30%+ movers: spent + ugly spreads
    r = {"READY": 1.0, "EARLY": 0.6, "WEAK": 0.35, "EXTENDED": 0.25}.get(row["state"], 0.4)
    liq = min((row["dollar_min"] or 0) / 100_000, 1.0)
    row["score"] = 45 * v + 25 * mv + 20 * r + 10 * liq
    return row


def rank(rows: list) -> list:
    ok = sorted([r for r in rows if not r["rejects"]],
                key=lambda r: r["score"], reverse=True)
    bad = [r for r in rows if r["rejects"]]
    return ok + bad


# ── data fetchers (network) ─────────────────────────────────────────────────
def _split_batch(bars, tickers: list) -> dict:
    if bars is None or bars.empty:
        return {}
    out = {}
    if isinstance(bars.index, pd.MultiIndex):
        for t in tickers:
            try:
                df = bars.xs(t, level="symbol")[
                    ["open", "high", "low", "close", "volume"]].copy()
                df = df.dropna(subset=["close"])
                if not df.empty:
                    out[t] = df
            except KeyError:
                continue
    elif tickers:
        out[tickers[0]] = bars[["open", "high", "low", "close", "volume"]].dropna(
            subset=["close"])
    return out


def fetch_daily(client, tickers: list, cfg: dict) -> dict:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    try:
        req = StockBarsRequest(
            symbol_or_symbols=tickers,
            timeframe=TimeFrame(1, TimeFrameUnit.Day),
            start=datetime.now(timezone.utc) - timedelta(days=45),
            **_get_feed_arg(cfg))
        return _split_batch(client.get_stock_bars(req).df, tickers)
    except Exception:
        return {}


def fetch_minutes_today(client, tickers: list, cfg: dict, now_et: datetime) -> dict:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    start = now_et.replace(hour=4, minute=0, second=0, microsecond=0)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=tickers,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=start.astimezone(timezone.utc),
            extended_hours=True,
            **_get_feed_arg(cfg))
        return _split_batch(client.get_stock_bars(req).df, tickers)
    except Exception as e:                                 # noqa: BLE001
        log.warning("[FUNNEL] today's minute bars failed for %d symbol(s): %s",
                    len(tickers), str(e)[:200])
        return {}


def fetch_minutes_history(client, tickers: list, cfg: dict, now_et: datetime,
                          days: int = 18) -> dict:
    """1Min bars over the trailing `days` calendar days, batched.

    Wide enough that MIN_AVG_SESSIONS..avg_days completed sessions fall inside
    it across weekends and holidays. Same endpoint and feed as
    `fetch_minutes_today`, which is the point — the rvol numerator and
    denominator have to come off the same tape. Measured cost: 30 symbols over
    18 days is ~113k bars in ~2.3s, and callers cache the result for the ET day.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    start = (now_et - timedelta(days=max(1, int(days)))).replace(
        hour=0, minute=0, second=0, microsecond=0)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=tickers,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=start.astimezone(timezone.utc),
            **_get_feed_arg(cfg))
        return _split_batch(client.get_stock_bars(req).df, tickers)
    except Exception as e:                                 # noqa: BLE001
        log.warning("[FUNNEL] history minute bars failed for %d symbol(s): %s",
                    len(tickers), str(e)[:200])
        # None means "could not ask", distinct from {} meaning "asked, nobody
        # had history". Callers cache a negative for the second and must not
        # for the first: this result feeds a cache keyed by the ET day, so
        # conflating them turned one 429 into a session with no rvol at all.
        return None


# Average session volume changes once every 24h. Cached for the ET day, with
# negative entries: the watchlist is different names most days and turns over
# intraday, so a symbol with too little history is routine and without the None
# it would be re-requested every scan and never resolve.
_AVG_VOL_CACHE: dict = {}
_AVG_VOL_DATE: str = ""


# Retry floor after a failed history fetch. Not caching the failure (see below)
# is right, but retrying it every cycle is not: 28 trending symbols over 18
# days is the desk's heaviest request, and two processes asking every 60s
# simply 429 forever and never resolve. Back off, then ask again.
_AVG_VOL_RETRY_SEC = 300.0
_AVG_VOL_RETRY_AT = 0.0


def avg_session_volumes(client, tickers: list, cfg: dict, now_et: datetime,
                        days: int = 18) -> dict:
    """{sym: minute-based average session volume or None}, day-cached."""
    global _AVG_VOL_DATE, _AVG_VOL_RETRY_AT
    today = now_et.date()
    stamp = today.isoformat()
    if _AVG_VOL_DATE != stamp:
        _AVG_VOL_CACHE.clear()
        _AVG_VOL_DATE = stamp

    wanted = [t for t in tickers if t not in _AVG_VOL_CACHE]
    if not wanted or client is None:
        return _AVG_VOL_CACHE

    if time.time() < _AVG_VOL_RETRY_AT:
        return _AVG_VOL_CACHE

    avg_days = int(knobs_from_cfg(cfg)["avg_days"])
    hist = fetch_minutes_history(client, wanted, cfg, now_et, days=days)
    if hist is None:
        _AVG_VOL_RETRY_AT = time.time() + _AVG_VOL_RETRY_SEC
        # The fetch could not be made (429, network, auth) — and
        # this cache is keyed by the ET day. Writing None for every symbol here
        # promotes one transient rejection into a day-long verdict, because
        # `wanted` skips anything already cached and nothing is retried until
        # tomorrow. A single 429 at startup is what left every trending row
        # with rvol=None for a whole session on 2026-08-07, which also emptied
        # the rvol column on half the A/B reject arm. Cache nothing; the next
        # poll asks again. An empty-but-successful {} still caches negatives,
        # so a fresh listing with no history is not re-requested every cycle.
        return _AVG_VOL_CACHE
    for sym in wanted:
        # The ask succeeded, so a symbol missing from the result genuinely has
        # no usable history — that None is a real answer and worth caching.
        _AVG_VOL_CACHE[sym] = avg_session_volume(
            hist.get(sym), today, avg_days)
    return _AVG_VOL_CACHE


def fetch_quotes(client, tickers: list, cfg: dict) -> dict:
    from alpaca.data.requests import StockLatestQuoteRequest
    try:
        resp = client.get_stock_latest_quote(StockLatestQuoteRequest(
            symbol_or_symbols=tickers, **_get_feed_arg(cfg)))
    except Exception:
        return {}
    out = {}
    for t in tickers:
        q = resp.get(t)
        if q is not None:
            bid = float(getattr(q, "bid_price", 0) or 0)
            ask = float(getattr(q, "ask_price", 0) or 0)
            if bid > 0 and ask > 0:
                out[t] = (bid, ask)
    return out


# ── display ─────────────────────────────────────────────────────────────────
def _fmt(v, spec="{:.1f}", dash="—"):
    return dash if v is None else spec.format(v)


def build_view(rows: list, now_et: datetime, feed: str = "IEX"):
    from rich.table import Table
    from rich.panel import Panel
    from rich.console import Group
    from rich.text import Text

    label, guide, color = session_window(now_et)
    head = Text()
    head.append(f" {now_et.strftime('%H:%M:%S')} ET   ", style="bold")
    head.append(f"{label}", style=f"bold {color}")
    head.append(f" — {guide}", style=color)
    shot = next_shot(now_et)
    if shot:
        shot_label, mins = shot
        eta = f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"
        head.append(f"   {shot_label} in {eta}", style="bold cyan")
    premarket = now_et.hour * 60 + now_et.minute < OPEN_MIN
    if premarket and str(feed).upper() != "SIP":
        head.append("\n IEX feed: premarket RVOL/$K-MIN understate the full tape"
                    " — treat them as floors, not truth", style="dim")

    t = Table(title="MORNING FUNNEL", title_style="bold cyan",
              header_style="bold", pad_edge=False)
    for col, just in [("#", "right"), ("SYM", "left"), ("PRICE", "right"),
                      ("CHG%", "right"), ("GAP%", "right"), ("RVOL", "right"),
                      ("SPRD%", "right"), ("$K/MIN", "right"), ("%R", "right"),
                      ("STATE", "left"), ("SCORE", "right")]:
        t.add_column(col, justify=just)

    state_style = {"READY": "bold green", "EARLY": "yellow",
                   "WEAK": "dim", "EXTENDED": "dark_orange"}
    n = 0
    for r in rows:
        if r["rejects"]:
            t.add_row("", r["sym"], _fmt(r["price"], "{:.2f}"),
                      _fmt(r["chg_pct"], "{:+.1f}"), "", "", "", "", "",
                      "rejected: " + ",".join(r["rejects"]), "",
                      style="dim")
            continue
        n += 1
        style = "bold" if n == 1 else ""
        t.add_row(str(n), r["sym"], _fmt(r["price"], "{:.2f}"),
                  _fmt(r["chg_pct"], "{:+.1f}"), _fmt(r["gap_pct"], "{:+.1f}"),
                  _fmt(r["rvol"], "{:.1f}x"), _fmt(r["spread_pct"], "{:.2f}"),
                  _fmt(None if r["dollar_min"] is None else r["dollar_min"] / 1000,
                       "{:,.0f}"),
                  _fmt(r["wr"], "{:.0f}"),
                  Text(r["state"], style=state_style.get(r["state"], "")),
                  f"{r['score']:.0f}", style=style)

    parts = [head, t]
    top = next((r for r in rows if not r["rejects"]), None)
    if top and top["score"] >= 60:
        parts.append(Text(f" >> point the monitors at {top['sym']}",
                          style="bold green"))
    elif top:
        parts.append(Text(" no strong candidate yet — keep scanning",
                          style="yellow"))
    else:
        parts.append(Text(" nothing tradeable in the list right now",
                          style="red"))
    return Panel(Group(*parts), border_style="cyan")


# ── main ────────────────────────────────────────────────────────────────────
def scan_once(client, tickers: list, cfg: dict, knobs: dict, now_et=None) -> list:
    now_et = now_et or datetime.now(ET)
    daily = fetch_daily(client, tickers, cfg)
    minutes = fetch_minutes_today(client, tickers, cfg, now_et)
    quotes = fetch_quotes(client, tickers, cfg)
    # Day-cached, and off the same minute tape as the numerator above.
    avg = avg_session_volumes(client, tickers, cfg, now_et)
    return rank([evaluate(t, daily.get(t), minutes.get(t), quotes.get(t),
                          now_et, knobs, avg_vol=avg.get(t))
                 for t in tickers])


def main() -> int:
    ap = argparse.ArgumentParser(description="Rank the morning watchlist.")
    ap.add_argument("tickers", nargs="*", help="candidate tickers (highest priority)")
    ap.add_argument("--once", action="store_true", help="one snapshot, then exit")
    ap.add_argument("--refresh", type=float, help="seconds between scans")
    args = ap.parse_args()

    from rich.console import Console
    console = Console()

    cfg = load_config()
    knobs = knobs_from_cfg(cfg)
    if args.refresh:
        knobs["refresh_sec"] = args.refresh

    if not (cfg.get("api_key") and cfg.get("secret_key")):
        console.print("[red]No Alpaca keys.[/red] Copy config/secrets.example.json "
                      "to config/secrets.json and fill in api_key/secret_key, or set "
                      "ALPACA_API_KEY / ALPACA_SECRET_KEY.")
        return 1

    candidates = gather_candidates(args.tickers, ROOT, knobs["max_candidates"])
    if not candidates:
        console.print("[yellow]No candidates.[/yellow] Pass tickers on the command "
                      "line or list them in config/funnel_watchlist.txt.")
        return 1

    client = connect_data_client(cfg)
    console.print(f"[dim]funnel watching {len(candidates)}: "
                  f"{' '.join(candidates)}[/dim]")

    feed = str(cfg.get("data_feed", "IEX"))

    if args.once:
        console.print(build_view(scan_once(client, candidates, cfg, knobs),
                                 datetime.now(ET), feed))
        return 0

    from rich.live import Live
    with Live(console=console, auto_refresh=False) as live:
        while True:
            candidates = gather_candidates(args.tickers, ROOT,
                                           knobs["max_candidates"]) or candidates
            rows = scan_once(client, candidates, cfg, knobs)
            live.update(build_view(rows, datetime.now(ET), feed), refresh=True)
            time.sleep(knobs["refresh_sec"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
