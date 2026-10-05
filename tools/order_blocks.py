#!/usr/bin/env python3
"""Point-in-time port of "Order Blocks & Breaker Blocks [LuxAlgo]" (TradingView, Pine v5), default inputs:
swing lookback 10, wicks (not bodies). Port is from the published logic as remembered; verify against a chart
before trusting it (see __main__).

Per bar t (bars oldest -> newest; each bar = (ts, open, high, low, close)), the state is computed using ONLY bars
<= t. A block is "known" from the close of the bar that confirmed it, NOT from its origin candle (TradingView draws
the box back to the origin candle, which looks like look-ahead on a chart).

  swing high/low: a bar len bars back whose high (low) exceeds the highest high (lowest low) of the len bars after it.
  bullish OB: when close crosses above the last swing high, the lowest-low candle between that swing and now;
              zone [its low, its high]. Becomes a BREAKER when min(open, close) < zone bottom; a breaker is removed
              when close > zone top.
  bearish OB: when close crosses below the last swing low, the highest-high candle between that swing and now;
              zone [its low, its high]. Becomes a BREAKER when max(open, close) > zone top; removed when close < bottom.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Block:
    kind: str            # "bull" or "bear"
    top: float
    btm: float
    origin_ts: float     # origin candle (where TradingView starts the box)
    known_ts: float      # close of the bar that confirmed it (first moment a live system could know it)
    breaker: bool = False
    break_ts: float | None = None


@dataclass
class OBState:
    blocks: list = field(default_factory=list)


def order_blocks(bars, length: int = 10, bar_sec: float = 300.0, use_body: bool = False):
    """Yield (i, [live blocks]) after each bar i. Blocks are point-in-time: only bars <= i are used."""
    hi = [b[2] for b in bars]
    lo = [b[3] for b in bars]
    op = [b[1] for b in bars]
    cl = [b[4] for b in bars]
    mx = [max(o, c) for o, c in zip(op, cl)] if use_body else hi
    mn = [min(o, c) for o, c in zip(op, cl)] if use_body else lo
    os_prev = os_ = 0
    top = {"y": None, "x": None, "crossed": False}
    btm = {"y": None, "x": None, "crossed": False}
    blocks: list[Block] = []
    for n in range(len(bars)):
        known = bars[n][0] + bar_sec
        if n >= length:
            upper = max(hi[n - length + 1:n + 1])
            lower = min(lo[n - length + 1:n + 1])
            os_prev = os_
            if hi[n - length] > upper:
                os_ = 0
            elif lo[n - length] < lower:
                os_ = 1
            if os_ == 0 and os_prev != 0:
                top = {"y": hi[n - length], "x": n - length, "crossed": False}
            if os_ == 1 and os_prev != 1:
                btm = {"y": lo[n - length], "x": n - length, "crossed": False}
        # bullish OB
        if top["y"] is not None and cl[n] > top["y"] and not top["crossed"]:
            top["crossed"] = True
            minima, maxima, loc = mn[n - 1], mx[n - 1], n - 1
            for i in range(1, n - top["x"]):
                if mn[n - i] <= minima:
                    minima, maxima, loc = mn[n - i], mx[n - i], n - i
            blocks.insert(0, Block("bull", maxima, minima, bars[loc][0], known))
        # bearish OB
        if btm["y"] is not None and cl[n] < btm["y"] and not btm["crossed"]:
            btm["crossed"] = True
            minima, maxima, loc = mn[n - 1], mx[n - 1], n - 1
            for i in range(1, n - btm["x"]):
                if mx[n - i] >= maxima:
                    minima, maxima, loc = mn[n - i], mx[n - i], n - i
            blocks.insert(0, Block("bear", maxima, minima, bars[loc][0], known))
        # breaker / removal
        keep = []
        for b in blocks:
            if b.kind == "bull":
                if not b.breaker:
                    if min(op[n], cl[n]) < b.btm:
                        b.breaker, b.break_ts = True, known
                elif cl[n] > b.top:
                    continue
            else:
                if not b.breaker:
                    if max(op[n], cl[n]) > b.top:
                        b.breaker, b.break_ts = True, known
                elif cl[n] < b.btm:
                    continue
            keep.append(b)
        blocks = keep
        yield n, [Block(**vars(b)) for b in blocks]


def charted(blocks, show: int = 3):
    """LuxAlgo draws only the last `show` bullish and last `show` bearish blocks (breakers included), newest first."""
    out, n = [], {"bull": 0, "bear": 0}
    for b in blocks:                      # blocks are kept newest first
        if n[b.kind] < show:
            out.append(b)
            n[b.kind] += 1
    return out


def overhead_resistance(blocks, price: float, within_pct: float = 0.3):
    """Resistance at or just above price: an unbroken BEARISH OB, or a broken BULLISH OB (bullish breaker), whose zone
    contains price or starts within within_pct % above it."""
    out = []
    for b in blocks:
        res = (b.kind == "bear" and not b.breaker) or (b.kind == "bull" and b.breaker)
        if res and b.top >= price and b.btm <= price * (1 + within_pct / 100):
            out.append(b)
    return out


if __name__ == "__main__":
    import sys
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    import bars as B
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    ET = ZoneInfo("America/New_York")
    sym, day = sys.argv[1], sys.argv[2]
    tf = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    end = min(d.replace(hour=20), datetime.now(timezone.utc) - timedelta(minutes=16))
    df = B.client().get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame(tf, TimeFrameUnit.Minute),
                                                    start=(d - timedelta(days=4)).astimezone(timezone.utc), end=end,
                                                    feed=DataFeed.SIP)).df.xs(sym, level="symbol")
    rows = [(t.timestamp(), float(r.open), float(r.high), float(r.low), float(r.close)) for t, r in df.iterrows()]
    seen = set()
    for i, blocks in order_blocks(rows, bar_sec=tf * 60):
        t = datetime.fromtimestamp(rows[i][0] + tf * 60, ET)
        if t.date() != d.date():
            continue
        for b in blocks:
            key = (b.kind, round(b.top, 4), round(b.btm, 4), b.breaker)
            if key not in seen:
                seen.add(key)
                print(f"{t:%H:%M} {'BREAKER ' if b.breaker else ''}{b.kind} OB {b.btm:.2f}-{b.top:.2f} "
                      f"origin {datetime.fromtimestamp(b.origin_ts, ET):%m-%d %H:%M} known {datetime.fromtimestamp(b.known_ts, ET):%m-%d %H:%M}")
