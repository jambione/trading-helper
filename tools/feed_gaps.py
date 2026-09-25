#!/usr/bin/env python3
"""SIP vs IEX trade gaps: is a "stale" name quiet, or just quiet on IEX?

The live feed is IEX (~3-15% of trades). For each symbol, counts trades and
the longest gap on SIP and IEX over the same window, and the share of the
window with the last trade older than 15 s. 2026-09-25 13:27-13:32: liquid
names were >15 s stale on IEX 16-63% of the time and ~0% on SIP.

Usage (on the mini)::

    .venv/bin/python tools/feed_gaps.py IONQ SMCI HIMS [--end-min-ago 17] [--minutes 5]
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_entry_watch as ew
from alpaca.data.requests import StockTradesRequest
from alpaca.data.enums import DataFeed
cl = ew._data_client()
ap = argparse.ArgumentParser()
ap.add_argument("symbols", nargs="+")
ap.add_argument("--end-min-ago", type=float, default=17.0, help="SIP is served once >15 min old")
ap.add_argument("--minutes", type=float, default=5.0)
a = ap.parse_args()
end = time.time() - a.end_min_ago * 60
span = a.minutes * 60
start = end - span
syms = [s.upper() for s in a.symbols]
def stats(feed, s):
    req = StockTradesRequest(symbol_or_symbols=s, start=datetime.fromtimestamp(start, timezone.utc),
                             end=datetime.fromtimestamp(end, timezone.utc), feed=feed, limit=10000)
    tr = cl.get_stock_trades(req).data.get(s) or []
    ts = sorted(t.timestamp.timestamp() for t in tr)
    if not ts:
        return 0, span, 1.0
    pts = [start] + ts + [end]
    gaps = [b - a for a, b in zip(pts, pts[1:])]
    over = sum(g - 15 for g in gaps if g > 15) / span   # share of the window with last trade > 15 s old
    return len(ts), max(gaps), over
print(f"window {datetime.fromtimestamp(start).strftime('%H:%M')}-{datetime.fromtimestamp(end).strftime('%H:%M')} ET")
print("sym    SIP trades  max gap | IEX trades  max gap  time stale(>15s)")
for s in syms:
    n1, g1, o1 = stats(DataFeed.SIP, s)
    n2, g2, o2 = stats(DataFeed.IEX, s)
    print(f"{s:5s} {n1:10d} {g1:7.1f}s | {n2:9d} {g2:7.1f}s   {o2:5.0%}  (SIP {o1:.0%})")
