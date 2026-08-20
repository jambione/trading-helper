#!/usr/bin/env python3
"""One bar fetcher and one forward-return rule for every desk analysis tool.

Written because there were already three near-copies (slot_contention,
trail_exits, admission_null) and desk_report needed a fourth. Copies drift, and
a forward-return rule that differs between two tools makes their numbers
quietly incomparable — which is exactly how 2026-08-20 produced two
contradictory answers about the same contested moments.

Why bars rather than the shadow log: shadow samples only exist while the desk
was watching a name, so scoring off them can only measure an episode that
outlived half the horizon, and how long the desk keeps watching is decided
AFTER the decision being scored. On 2026-08-14..20 the median episode ran 383
seconds against a 30-minute horizon. Shadow samples also repeat a stale price
44% of the time, which piles exact-zero returns onto the median. Bars know
neither of those things.

Not a tape: 1-minute closes, no spread, no intra-bar order.
"""
from __future__ import annotations

import bisect
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = ZoneInfo("America/New_York")

_CLIENT = None
_CACHE: dict[tuple, tuple] = {}
_CLIENT_FAILED = False


def day_of(ts) -> str:
    """ET session date for a unix timestamp."""
    return datetime.fromtimestamp(
        float(ts), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def et_minutes(ts) -> int:
    dt = datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET)
    return dt.hour * 60 + dt.minute


def client():
    """Alpaca data client, or None when credentials are unavailable.

    Returns None rather than raising so a caller can degrade to "no forward
    returns" and say so, instead of dying halfway through a report.
    """
    global _CLIENT, _CLIENT_FAILED
    if _CLIENT is not None or _CLIENT_FAILED:
        return _CLIENT
    try:
        sec = json.load(open(os.path.join(ROOT, "config", "secrets.json")))
        import sys
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import alpaca_api as aa
        _CLIENT = aa.connect_data_client(
            {"api_key": sec["api_key"], "secret_key": sec["secret_key"]})
    except Exception:
        _CLIENT_FAILED = True
        _CLIENT = None
    return _CLIENT


def fetch(sym: str, day: str, feed: str = "sip") -> tuple[list | None, list | None]:
    """(stamps, closes) of 1m RTH bars for sym/day. ([], []) is cached too."""
    sym = str(sym).upper()
    key = (sym, day, feed)
    if key in _CACHE:
        return _CACHE[key]
    cl = client()
    if cl is None:
        _CACHE[key] = (None, None)
        return _CACHE[key]
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed

    out = (None, None)
    try:
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        df = cl.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=d.replace(hour=9, minute=25).astimezone(timezone.utc),
            end=d.replace(hour=16, minute=5).astimezone(timezone.utc),
            limit=10000, extended_hours=False,
            feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
        )).df
        import pandas as pd
        if df is not None and not df.empty:
            if isinstance(df.index, pd.MultiIndex):
                df = df.xs(sym, level="symbol")
            df = df.sort_index()
            out = ([t.timestamp() for t in df.index],
                   [float(v) for v in df["close"]])
    except Exception:
        out = (None, None)
    _CACHE[key] = out
    return out


def fetch_many(syms, day: str, feed: str = "sip", chunk: int = 100) -> None:
    """Warm the cache for many symbols with few requests.

    Alpaca returns a MultiIndex frame for a symbol list, so a universe control
    that needs a hundred names per day costs a handful of round trips instead
    of a hundred. Symbols the response omits are cached as (None, None) so a
    later fetch() does not retry them one at a time.
    """
    want = [str(s).upper() for s in syms]
    want = [s for s in want if (s, day, feed) not in _CACHE]
    if not want:
        return
    cl = client()
    if cl is None:
        for s in want:
            _CACHE[(s, day, feed)] = (None, None)
        return
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    import pandas as pd

    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    for i in range(0, len(want), chunk):
        batch = want[i:i + chunk]
        got: dict[str, tuple] = {}
        try:
            df = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=d.replace(hour=9, minute=25).astimezone(timezone.utc),
                end=d.replace(hour=16, minute=5).astimezone(timezone.utc),
                limit=1000000, extended_hours=False,
                feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
            )).df
            if df is not None and not df.empty:
                if isinstance(df.index, pd.MultiIndex):
                    for sym in df.index.get_level_values("symbol").unique():
                        sub = df.xs(sym, level="symbol").sort_index()
                        got[str(sym).upper()] = (
                            [t.timestamp() for t in sub.index],
                            [float(v) for v in sub["close"]])
                elif len(batch) == 1:
                    sub = df.sort_index()
                    got[batch[0]] = ([t.timestamp() for t in sub.index],
                                     [float(v) for v in sub["close"]])
        except Exception:
            got = {}
        for s in batch:
            _CACHE[(s, day, feed)] = got.get(s, (None, None))


def forward_return(stamps, closes, t0: float, horizon: float) -> float | None:
    """Pct change from the bar at/just before t0 to the last bar within horizon.

    None when the window barely opened — a truncated window is missing data,
    not a flat return, and averaging zeros in biases every slice toward
    nothing-happened. Same rule shadow_report.forward_return applies, so
    numbers stay comparable across tools.
    """
    if not stamps:
        return None
    i = bisect.bisect_right(stamps, t0) - 1
    if i < 0:
        return None
    j = bisect.bisect_right(stamps, t0 + horizon) - 1
    if j <= i:
        return None
    if stamps[j] - stamps[i] < horizon * 0.5:
        return None
    p0 = closes[i]
    if not p0:
        return None
    return (closes[j] - p0) / p0 * 100.0


def fwd(sym: str, t0: float, horizon: float, feed: str = "sip") -> float | None:
    """forward_return for a symbol at a timestamp, fetching its day's bars."""
    stamps, closes = fetch(sym, day_of(t0), feed)
    return forward_return(stamps, closes, t0, horizon)
