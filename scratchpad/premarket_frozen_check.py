#!/usr/bin/env python3
"""Is the desk's premarket price frozen, or am I reading a cached field?

The shadow log shows zero consecutive price changes across ~363 premarket
samples on every name today. That is a large claim, so this checks it the
ways it could be wrong:

  1. are the samples genuinely distinct instants (ts advancing)?
  2. does ANY numeric field move, or just `price`?
  3. does the same name move once RTH starts, on the same field?
  4. what does Alpaca return for that symbol right now, live?

Read-only.
"""
from __future__ import annotations

import sys
import time
from collections import defaultdict

sys.path.insert(0, ".")
sys.path.insert(0, "tools")

import bars  # noqa: E402
import shadow_report as sr  # noqa: E402

rows = sr.load()
days = sorted({bars.day_of(r["ts"]) for r in rows if r.get("ts")})
today, prev = days[-1], days[-2]

t = [r for r in rows if r.get("ts") and bars.day_of(r["ts"]) == today]
sym = max(defaultdict(int, {s: sum(1 for r in t if r.get("symbol") == s)
                            for s in {r.get("symbol") for r in t}}).items(),
          key=lambda kv: kv[1])[0]
mine = [r for r in t if r.get("symbol") == sym]
mine.sort(key=lambda r: float(r["ts"]))
print(f"session {today}, busiest symbol {sym}, {len(mine)} samples")

ts = [float(r["ts"]) for r in mine]
gaps = [round(ts[i] - ts[i - 1], 1) for i in range(1, len(ts))]
print(f"  ts span {ts[-1]-ts[0]:.0f}s   "
      f"median gap {sorted(gaps)[len(gaps)//2]:.1f}s   "
      f"distinct ts {len(set(ts))}/{len(ts)}")
print(f"  first {bars.et_minutes(ts[0])//60:02d}:{bars.et_minutes(ts[0])%60:02d}"
      f"  last {bars.et_minutes(ts[-1])//60:02d}:{bars.et_minutes(ts[-1])%60:02d} ET")

print("\n  which numeric fields ever change across those samples?")
numeric = defaultdict(set)
for r in mine:
    for k, v in r.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            numeric[k].add(round(float(v), 6))
moved = sorted(k for k, v in numeric.items() if len(v) > 1 and k != "ts")
froze = sorted(k for k, v in numeric.items() if len(v) == 1)
print(f"    MOVED ({len(moved)}): {', '.join(moved[:18])}")
print(f"    FROZE ({len(froze)}): {', '.join(froze[:18])}")

print(f"\n  same symbol on the previous session ({prev}), premarket vs RTH:")
p = [r for r in rows if r.get("ts") and bars.day_of(r["ts"]) == prev
     and r.get("symbol") == sym and r.get("price") is not None]
p.sort(key=lambda r: float(r["ts"]))
for label, lo, hi in (("premarket", 0, 9 * 60 + 30), ("RTH", 9 * 60 + 30, 16 * 60)):
    v = [float(r["price"]) for r in p if lo <= bars.et_minutes(r["ts"]) < hi]
    if len(v) < 2:
        print(f"    {label:<10} {len(v)} samples — too few")
        continue
    ch = sum(1 for i in range(1, len(v)) if v[i] != v[i - 1])
    print(f"    {label:<10} {ch:>4} changes in {len(v):>4} samples "
          f"= {100*ch/(len(v)-1):>3.0f}%   range {min(v):.2f}-{max(v):.2f}")

print("\n  live Alpaca right now:")
try:
    cl = bars.client()
    from alpaca.data.requests import (StockLatestQuoteRequest,
                                      StockLatestTradeRequest)
    from alpaca.data.enums import DataFeed
    q = cl.get_stock_latest_quote(StockLatestQuoteRequest(
        symbol_or_symbols=sym, feed=DataFeed.IEX))[sym]
    tr = cl.get_stock_latest_trade(StockLatestTradeRequest(
        symbol_or_symbols=sym, feed=DataFeed.IEX))[sym]
    now = time.time()
    print(f"    IEX quote  bid {float(q.bid_price):.2f} ask "
          f"{float(q.ask_price):.2f}  age {now-q.timestamp.timestamp():.0f}s")
    print(f"    IEX trade  {float(tr.price):.2f}  "
          f"age {now-tr.timestamp.timestamp():.0f}s")
    print(f"    shadow log last price {float(mine[-1]['price']):.2f}  "
          f"age {now-ts[-1]:.0f}s")
except Exception as e:  # noqa: BLE001
    print(f"    unavailable: {type(e).__name__}: {e}")
