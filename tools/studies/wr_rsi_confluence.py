#!/usr/bin/env python3
"""wr_rsi_confluence.py — DESCRIPTIVE, IN-SAMPLE quick look (operator 10/10: "confluence of %R and RSI both having directions"). Not a test.

Same trades as rsi_history_fills.py (position_shadow.jsonl, first exit signal per position). At entry, on SIP 1-minute bars that
CLOSED before the entry:
  RSI direction   RSI(14) Wilder on RTH bars, now minus 10 bars earlier: up >= +10, down <= -10, else flat (as rsi_history_fills)
  %R direction    fast %R (21, EMA 7; the TradingView-parity port, extended hours from 04:00), now minus 10 bars earlier:
                  up >= +15, down <= -15, else flat (15 points = the wr_trend15 rise)
Groups trades by the 3x3 cross and prints gross trade bp, win rate, +15 min move; then BOTH-UP vs the rest by day.
USAGE (mini, repo root): .venv/bin/python tools/studies/wr_rsi_confluence.py 2026-09-26 2026-10-09 [sip|iex]
(iex = the live bar feed the gate reads; the +15 min outcome then also uses IEX closes)
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
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools"), os.path.join(ROOT, "tools", "studies")]
import pandas as pd  # noqa: E402

import signals as SG  # noqa: E402
from rsi_history_fills import wilder_rsi  # noqa: E402

ET = ZoneInfo("America/New_York")


def directions(bars, t_entry):
    done = [b for b in bars if b["t"] + 60 <= t_entry]
    rth = [b for b in done if datetime.fromtimestamp(b["t"], ET).strftime("%H:%M") >= "09:30"]
    if len(rth) < 45:
        return None
    rsi = wilder_rsi([b["c"] for b in rth])
    if rsi[-1] is None or rsi[-11] is None:
        return None
    d_rsi = rsi[-1] - rsi[-11]
    df = pd.DataFrame([{"time": pd.Timestamp(b["t"], unit="s", tz="UTC"), "high": b["h"], "low": b["l"], "close": b["c"]} for b in done])
    wr = SG._minute_grid_pr(df, 21, 7)
    if wr is None or len(wr) < 11 or wr.iloc[-11] != wr.iloc[-11]:
        return None
    d_wr = float(wr.iloc[-1] - wr.iloc[-11])
    lab = lambda d, k: "up" if d >= k else ("down" if d <= -k else "flat")  # noqa: E731
    return {"rsi": lab(d_rsi, 10), "wr": lab(d_wr, 15)}


def main():
    lo, hi = sys.argv[1], sys.argv[2]
    feed_name = sys.argv[3] if len(sys.argv) > 3 else "sip"
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
                                                   start=d0.replace(hour=4), end=d0.replace(hour=16), feed=DataFeed.IEX if feed_name == "iex" else DataFeed.SIP))
            for s, rows in (r.data or {}).items():
                bars[(d, s)] = [{"t": b.timestamp.timestamp(), "h": float(b.high), "l": float(b.low), "c": float(b.close)} for b in rows]
            time.sleep(0.5)
    rows = []
    for d, s, e, v in trades:
        b = bars.get((d, s), [])
        f = directions(b, v["t0"])
        if f is None:
            continue
        after = [x for x in b if x["t"] + 60 <= v["t0"] + 900]
        f15 = (after[-1]["c"] / e - 1) * 1e4 if after and after[-1]["t"] > v["t0"] else None
        rows.append({"d": d, **f, "trade": (v["exit"] / e - 1) * 1e4, "f15": f15})
    print(f"{len(rows)} desk trades {lo}..{hi} with %R and RSI direction from {feed_name.upper()} bars (of {len(trades)} closed); gross bp, IN SAMPLE\n")

    def line(lab, x):
        f = [r["f15"] for r in x if r["f15"] is not None]
        print(f"  {lab:24s} n {len(x):4d} | trade {st.fmean(r['trade'] for r in x):+6.1f} win {sum(r['trade'] > 0 for r in x) / len(x):4.0%} "
              f"| +15 min {st.fmean(f) if f else float('nan'):+6.1f} (up {sum(q > 0 for q in f)}/{len(f)})")

    for w in ("up", "flat", "down"):
        for rs in ("up", "flat", "down"):
            x = [r for r in rows if r["wr"] == w and r["rsi"] == rs]
            if x:
                line(f"%R {w:4s} / RSI {rs:4s}", x)
        print()
    both = [r for r in rows if r["wr"] == "up" and r["rsi"] == "up"]
    rest = [r for r in rows if not (r["wr"] == "up" and r["rsi"] == "up")]
    line("BOTH UP", both)
    line("%R up only", [r for r in rows if r["wr"] == "up" and r["rsi"] != "up"])
    line("RSI up only", [r for r in rows if r["rsi"] == "up" and r["wr"] != "up"])
    line("neither up", [r for r in rows if r["rsi"] != "up" and r["wr"] != "up"])
    line("everything else", rest)
    byd = defaultdict(lambda: ([], []))
    for r in rows:
        byd[r["d"]][0 if (r["wr"] == "up" and r["rsi"] == "up") else 1].append(r["trade"])
    print("\n  BOTH UP minus the rest, trade bp, by day:",
          " ".join(f"{d[5:]}:{st.fmean(a) - st.fmean(b):+.0f}(n{len(a)})" for d, (a, b) in sorted(byd.items()) if a and b))


if __name__ == "__main__":
    main()
