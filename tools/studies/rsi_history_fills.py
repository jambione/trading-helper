#!/usr/bin/env python3
"""rsi_history_fills.py — DESCRIPTIVE, IN-SAMPLE quick look (Indicator Usage Audit, RSI section, operator 10/9). Not a test.

For each closed desk trade (ai_reports/position_shadow.jsonl, first exit signal per position) from DAY_LO to DAY_HI, compute
RSI(14) (Wilder) on full-tape SIP 1-minute bars that CLOSED before the entry, and group trades by:
  trend      RSI now minus RSI 10 bars earlier: up >= +10, down <= -10, else flat
  divergence bullish = the low of the last 10 bars is below the low of bars 11-30 back, while the RSI low of the last 10 is
             ABOVE the RSI low of bars 11-30 back, and that earlier RSI low was < 40
  oversold   minutes since RSI was last < 30 (within 120 bars): none / < 15 / 15-60 / > 60
Outcomes (gross): the trade's own result (exit / entry) and the SIP close 15 minutes after entry vs the entry price.
USAGE (mini, repo root; outside 09:00-16:30 ET on a weekday): .venv/bin/python tools/studies/rsi_history_fills.py 2026-09-26 2026-10-09
"""
from __future__ import annotations

import json
import os
import statistics as st
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.getcwd()
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]
ET = ZoneInfo("America/New_York")


def wilder_rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, n + 1)]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, n + 1)]
    ag, al = sum(gains) / n, sum(losses) / n
    for i in range(n, len(closes)):
        if i > n:
            d = closes[i] - closes[i - 1]
            ag = (ag * (n - 1) + max(0.0, d)) / n
            al = (al * (n - 1) + max(0.0, -d)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def features(bars, t_entry):
    done = [b for b in bars if b["t"] + 60 <= t_entry]
    if len(done) < 45:
        return None
    rsi = wilder_rsi([b["c"] for b in done])
    if rsi[-1] is None or rsi[-11] is None or rsi[-31] is None:
        return None
    d10 = rsi[-1] - rsi[-11]
    trend = "up" if d10 >= 10 else ("down" if d10 <= -10 else "flat")
    lo_recent, lo_prior = min(b["l"] for b in done[-10:]), min(b["l"] for b in done[-30:-10])
    r_recent, r_prior = min(rsi[-10:]), min(rsi[-30:-10])
    div = lo_recent < lo_prior and r_recent > r_prior and r_prior < 40
    since = None
    for k in range(1, min(120, len(rsi)) + 1):
        if rsi[-k] is not None and rsi[-k] < 30:
            since = k - 1
            break
    os_b = "none" if since is None else ("<15" if since < 15 else ("15-60" if since <= 60 else ">60"))
    return {"trend": trend, "div": "bull_div" if div else "no_div", "os": os_b, "rsi": rsi[-1]}


def main():
    lo, hi = sys.argv[1], sys.argv[2]
    pos = {}
    for l in open(os.path.join(ROOT, "ai_reports", "position_shadow.jsonl")):
        r = json.loads(l)
        if not r.get("entry_price"):
            continue
        d = datetime.fromtimestamp(r["ts"], ET).strftime("%Y-%m-%d")
        if not (lo <= d <= hi):
            continue
        k = (d, r["symbol"], r["entry_price"])
        p = pos.setdefault(k, {"t0": r["ts"] - (r.get("hold_sec") or 0), "exit": None})
        if r.get("exit_why") not in (None, "hold") and p["exit"] is None and r.get("price"):
            p["exit"] = r["price"]
    trades = [(d, s, e, v) for (d, s, e), v in pos.items() if v["exit"]]
    import bars as B
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    cl = B.client()
    by_day = defaultdict(set)
    for d, s, _, _ in trades:
        by_day[d].add(s)
    bars = {}
    for d, syms in sorted(by_day.items()):
        d0 = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET)
        syms = sorted(syms)
        for i in range(0, len(syms), 50):
            r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms[i:i + 50], timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                                                   start=d0.replace(hour=9, minute=30), end=d0.replace(hour=16), feed=DataFeed.SIP))
            for s, rows in (r.data or {}).items():
                bars[(d, s)] = [{"t": b.timestamp.timestamp(), "l": float(b.low), "c": float(b.close)} for b in rows]
            time.sleep(0.5)
    rows = []
    for d, s, e, v in trades:
        b = bars.get((d, s), [])
        f = features(b, v["t0"])
        if f is None:
            continue
        after = [x for x in b if x["t"] + 60 <= v["t0"] + 900]
        f15 = (after[-1]["c"] / e - 1) * 1e4 if after and after[-1]["t"] > v["t0"] else None
        rows.append({"d": d, **f, "trade": (v["exit"] / e - 1) * 1e4, "f15": f15})
    print(f"{len(rows)} desk trades {lo}..{hi} with RSI(14) history from SIP bars (of {len(trades)} closed); gross bp, in sample\n")
    for key, order in (("trend", ("up", "flat", "down")), ("div", ("bull_div", "no_div")), ("os", ("none", "<15", "15-60", ">60"))):
        for g in order:
            x = [r for r in rows if r[key] == g]
            if not x:
                continue
            f = [r["f15"] for r in x if r["f15"] is not None]
            print(f"  {key:5s} {g:8s} n {len(x):4d} | trade {st.fmean(r['trade'] for r in x):+6.1f} win {sum(r['trade'] > 0 for r in x) / len(x):.0%} "
                  f"| +15 min {st.fmean(f) if f else float('nan'):+6.1f} (up {sum(v > 0 for v in f)}/{len(f)})")
        print()
    byd = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["f15"] is not None:
            byd[r["d"]][r["trend"]].append(r["f15"])
    print("  trend up minus flat, +15 min, by day:", " ".join(f"{d[5:]}:{st.fmean(v['up']) - st.fmean(v['flat']):+.0f}"
                                                           for d, v in sorted(byd.items()) if v["up"] and v["flat"]))


if __name__ == "__main__":
    main()
