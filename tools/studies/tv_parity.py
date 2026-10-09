#!/usr/bin/env python3
"""tv_parity.py — does the desk's %R port (signals.py) match the operator's TradingView "%R Trend Exhaustion [upslidedown]"?

Input: a CSV of readings copied from TradingView 1-minute charts, one per line:
    symbol,date,time_et,fast,slow           e.g.  RBLX,2026-10-09,14:19,-40.67,-27.15
time_et = the bar the crosshair is on (TradingView labels a bar by its START). The values read off a bar are that bar's CLOSE.
For each reading this computes %R from SIP 1-minute bars ending with that bar (inclusive), with the given settings, both
RTH-only (09:30+) and with extended hours (04:00+), and prints the gaps. Read-only; one SIP request per symbol-day.

USAGE (mini, repo root):
  .venv/bin/python tools/studies/tv_parity.py readings.csv [--fast 21 --fast-smooth 7 --slow 112 --slow-smooth 3]
Pass: both lines within 2 points on most readings under one of the two session settings = the port is right.
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
from datetime import datetime

ROOT = os.getcwd()
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]
import pandas as pd  # noqa: E402

import signals as S  # noqa: E402


def compute(df, fast, fs, slow, ss):
    return float(S._minute_grid_pr(df, fast, fs).iloc[-1]), float(S._minute_grid_pr(df, slow, ss).iloc[-1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--fast", type=int, default=21)
    ap.add_argument("--fast-smooth", type=int, default=7)
    ap.add_argument("--slow", type=int, default=112)
    ap.add_argument("--slow-smooth", type=int, default=3)
    a = ap.parse_args()
    import bars as B
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    rows = [r for r in csv.DictReader(open(a.csv)) if r.get("symbol")]
    cache, out = {}, []
    for r in rows:
        sym, day = r["symbol"].strip().upper(), r["date"].strip()
        if (sym, day) not in cache:
            d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
            res = B.client().get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                                                             start=d0.replace(hour=4),
                                                             end=min(d0.replace(hour=20), datetime.now(ET) - __import__("datetime").timedelta(minutes=16)),
                                                             feed=DataFeed.SIP))
            cache[(sym, day)] = pd.DataFrame([{"time": b.timestamp, "high": b.high, "low": b.low, "close": b.close}
                                              for b in (res.data or {}).get(sym, [])])
        df = cache[(sym, day)]
        hh, mm = (int(x) for x in r["time_et"].strip().split(":"))
        t_bar = pd.Timestamp(datetime.strptime(day, "%Y-%m-%d").replace(hour=hh, minute=mm, tzinfo=ET))
        tv_f, tv_s = float(r["fast"]), float(r["slow"])
        rec = {"sym": sym, "at": f"{day} {r['time_et']}", "tv": (tv_f, tv_s)}
        for lab, start in (("rth", (9, 30)), ("ext", (4, 0))):
            t0 = pd.Timestamp(datetime.strptime(day, "%Y-%m-%d").replace(hour=start[0], minute=start[1], tzinfo=ET))
            cut = df[(df["time"] >= t0) & (df["time"] <= t_bar)].reset_index(drop=True)
            rec[lab] = compute(cut, a.fast, a.fast_smooth, a.slow, a.slow_smooth) if len(cut) >= 5 else (float("nan"), float("nan"))
        out.append(rec)
    print(f"settings: fast %R({a.fast}) EMA {a.fast_smooth}, slow %R({a.slow}) EMA {a.slow_smooth}; {len(out)} readings\n")
    print(f"  {'symbol':6s} {'bar':16s} {'TV fast':>8s} {'TV slow':>8s} | {'RTH fast':>8s} {'RTH slow':>8s} | {'EXT fast':>8s} {'EXT slow':>8s}")
    for x in out:
        print(f"  {x['sym']:6s} {x['at']:16s} {x['tv'][0]:8.2f} {x['tv'][1]:8.2f} | {x['rth'][0]:8.2f} {x['rth'][1]:8.2f} | {x['ext'][0]:8.2f} {x['ext'][1]:8.2f}")
    for lab in ("rth", "ext"):
        gf = [abs(x[lab][0] - x["tv"][0]) for x in out if x[lab][0] == x[lab][0]]
        gs = [abs(x[lab][1] - x["tv"][1]) for x in out if x[lab][1] == x[lab][1]]
        if gf:
            print(f"\n  {lab.upper()}: fast gap median {statistics.median(gf):.2f} (within 2 pts {sum(g <= 2 for g in gf)}/{len(gf)}), "
                  f"slow gap median {statistics.median(gs):.2f} (within 2 pts {sum(g <= 2 for g in gs)}/{len(gs)})")


if __name__ == "__main__":
    main()
