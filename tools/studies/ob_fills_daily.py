#!/usr/bin/env python3
"""Held-out record for the order-block skip rule: score each day's desk BUY fills with the point-in-time LuxAlgo port.

For every paper BUY fill on DAY (FIFO-matched sells give the realized round trip), on 1-minute SIP bars (prior day +
DAY, premarket + RTH), using only blocks known before the fill and the charted last 3 per side:
  resist_0.3   inside or within 0.3% under a resistance block (the pre-registered rule, docs/studies/order_block_gate_prereg.json)
  room_pct     % from the fill up to the nearest resistance block bottom (None = none above)
Appends one JSON line per fill to ai_reports/order_block_gate/heldout_fills.jsonl (idempotent per day) and prints a table.

Run on the mini after the close (SIP is 15 minutes delayed):
  .venv/bin/python tools/studies/ob_fills_daily.py 2026-10-05
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import bars as B  # noqa: E402
import indicator_levels_study as ILS  # noqa: E402
import order_blocks as OB  # noqa: E402
from order_block_gate import WITHIN  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = os.path.join(ROOT, "ai_reports", "order_block_gate", "heldout_fills.jsonl")


def minute_bars(cl, sym, day):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    df = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                                            start=(d - timedelta(days=4)).replace(hour=4).astimezone(timezone.utc),
                                            end=min(d.replace(hour=16), datetime.now(timezone.utc) - timedelta(minutes=16)),
                                            feed=DataFeed.SIP)).df
    if df is None or df.empty:
        return []
    df = df.xs(sym, level="symbol")
    rows = []
    for t, r in df.iterrows():
        tl = t.astimezone(ET)
        if 4 * 60 <= tl.hour * 60 + tl.minute < 16 * 60:          # premarket + RTH, as the study's cache
            rows.append((t.timestamp(), float(r.open), float(r.high), float(r.low), float(r.close)))
    days = sorted({datetime.fromtimestamp(x[0], ET).strftime("%Y-%m-%d") for x in rows})
    keep = set(days[-2:])                                            # prior trading day + DAY
    return [x for x in rows if datetime.fromtimestamp(x[0], ET).strftime("%Y-%m-%d") in keep]


def main():
    day = sys.argv[1]
    ILS.FILLS_FROM = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET) - timedelta(days=1)
    fills = [f for f in ILS.fills() if f["day"] == day]
    cl = B.client()
    out = []
    for f in sorted(fills, key=lambda x: x["t"]):
        rows = minute_bars(cl, f["sym"], day)
        flag = room = None
        if rows:
            last = None
            for i, blocks in OB.order_blocks(rows, bar_sec=60):
                if rows[i][0] + 60 <= f["t"]:
                    last = blocks
                else:
                    break
            if last is not None:
                ch = OB.charted([b for b in last if b.known_ts <= f["t"]])
                flag = bool(OB.overhead_resistance(ch, f["fill"], WITHIN))
                above = [b.btm for b in ch if ((b.kind == "bear" and not b.breaker) or (b.kind == "bull" and b.breaker))
                         and b.btm > f["fill"]]
                inside = [b for b in ch if ((b.kind == "bear" and not b.breaker) or (b.kind == "bull" and b.breaker))
                          and b.btm <= f["fill"] <= b.top]
                room = 0.0 if inside else ((min(above) / f["fill"] - 1) * 100 if above else None)
        out.append({"day": day, "sym": f["sym"], "t": f["t"], "fill": f["fill"], "qty": f["qty"],
                    "realized_bp": None if f["realized"] is None else round(f["realized"] * 1e4, 1),
                    "resist_0.3": flag, "room_pct": None if room is None else round(room, 3)})
    old = [json.loads(x) for x in open(OUT)] if os.path.exists(OUT) else []
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        for r in [x for x in old if x["day"] != day] + out:
            fh.write(json.dumps(r) + "\n")
    print(f"{day}: {len(out)} buy fills")
    for r in out:
        print(f"  {datetime.fromtimestamp(r['t'], ET):%H:%M:%S} {r['sym']:<6} {r['fill']:>9.2f}  resist_0.3={r['resist_0.3']}"
              f"  room={r['room_pct']}  realized={r['realized_bp']} bp")
    for lab, g in (("flagged", True), ("not flagged", False)):
        xs = [r["realized_bp"] for r in out if r["resist_0.3"] is g and r["realized_bp"] is not None]
        if xs:
            print(f"  {lab}: n {len(xs)} mean {statistics.fmean(xs):+.1f} bp")
    allr = [json.loads(x) for x in open(OUT)]
    for lab, g in (("flagged", True), ("not flagged", False)):
        xs = [r["realized_bp"] for r in allr if r["resist_0.3"] is g and r["realized_bp"] is not None]
        if xs:
            print(f"  HELD-OUT TO DATE ({len({r['day'] for r in allr})} days) {lab}: n {len(xs)} mean {statistics.fmean(xs):+.1f} bp")


if __name__ == "__main__":
    main()
