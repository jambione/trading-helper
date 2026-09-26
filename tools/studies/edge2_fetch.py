#!/usr/bin/env python3
"""edge2_fetch.py — data for Round-2 strategy-edge study (2026-09-26).

Universe U2 = edge_fetch.LIQUID (100 large caps/ETFs) + the sector ETFs not in it
(XLV XLY XLP XLI XLU XLB XLRE XLC); SPY QQQ IWM DIA XLK XLF XLE are already in LIQUID.

  days     last 250 trading days to 2026-09-25 (from IEX SPY daily bars)
  split    pre-registered in ai_reports/edge2_split.json BEFORE any analysis:
           oldest 60% tune, next 20% validate, newest 20% holdout
  bars     IEX 1m, 09:25-16:05 ET -> ai_reports/edge_iex_bars/liq/DAY.pkl
  spreads  SIP (free, 15-min-delayed historical) quoted spread, median over 2 s,
           at 09:31, 13:30, 15:50 on every 3rd day (+ the last 20) -> ai_reports/edge_spreads.json
           (same store/model as round 1)
Throttled to <= 1 multi-symbol request per second.

USAGE  edge2_fetch.py days | bars | spreads
"""
from __future__ import annotations

import json
import os
import pickle
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import edge_fetch as ef  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = os.path.join(ROOT, "ai_reports", "edge_iex_bars", "liq")
SPLIT = os.path.join(ROOT, "ai_reports", "edge2_split.json")
SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC"]
ETFS = ["SPY", "QQQ", "IWM", "DIA"] + SECTORS
U2 = sorted(set(ef.LIQUID) | set(ETFS))
N_DAYS = 250
LAST = "2026-09-25"


def trading_days(cl):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    end = datetime.strptime(LAST, "%Y-%m-%d").replace(tzinfo=ET) + timedelta(days=1)
    bs = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
                                            start=end - timedelta(days=420), end=end, feed=DataFeed.IEX))
    days = sorted({r.timestamp.astimezone(ET).strftime("%Y-%m-%d") for r in bs.data["SPY"]})
    return [d for d in days if d <= LAST]


def main():
    what = sys.argv[1]
    cl = bars.client()
    os.makedirs(OUT, exist_ok=True)
    if what == "days":
        if os.path.exists(SPLIT):
            print("split already registered; not overwriting"); print(open(SPLIT).read()[:400]); return
        days = trading_days(cl)
        sel = days[-(N_DAYS + 1):]          # +1: the day before the first, for prior closes
        use = sel[1:]
        n = len(use)
        a, b = int(round(n * 0.6)), int(round(n * 0.8))
        reg = {"registered_at": datetime.now(ET).isoformat(timespec="seconds"),
               "prior_day": sel[0], "tune": use[:a], "validate": use[a:b], "holdout": use[b:],
               "rules": {
                   "cost": "full SIP quoted spread at entry per round trip (half in, half out); hedge leg adds SPY spread",
                   "promote_to_holdout": "net > 0 on tune AND on validate, and day-clustered t >= 2.0 on tune+validate pooled",
                   "holdout": "run once, only for promoted candidates (max 3, chosen by tune+validate t); report whatever it says",
                   "universe": "U2 names whose prior-day median SIP spread <= 3 bp (ETF list always included)",
                   "reversal_times": "decisions every 15 min, lookback window must start >= 09:45; exit by 15:50",
               }}
        json.dump(reg, open(SPLIT, "w"), indent=1)
        print(f"registered: tune {use[0]}..{use[a-1]} ({a}), validate {use[a]}..{use[b-1]} ({b-a}), "
              f"holdout {use[b]}..{use[-1]} ({n-b})")
        return
    reg = json.load(open(SPLIT))
    days = [reg["prior_day"]] + reg["tune"] + reg["validate"] + reg["holdout"]
    if what == "bars":
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        for day in days:
            p = os.path.join(OUT, f"{day}.pkl")
            if os.path.exists(p):
                continue
            d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
            out = {}
            for i in range(0, len(U2), 60):
                batch = U2[i:i + 60]
                for attempt in range(3):
                    ef.throttle()
                    try:
                        bs = cl.get_stock_bars(StockBarsRequest(
                            symbol_or_symbols=batch, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                            start=d.replace(hour=9, minute=25).astimezone(timezone.utc),
                            end=d.replace(hour=16, minute=5).astimezone(timezone.utc), feed=DataFeed.IEX))
                        for sym, rs in (bs.data or {}).items():
                            out[sym] = (np.array([int(r.timestamp.timestamp()) for r in rs], np.int64),
                                        np.array([r.open for r in rs]), np.array([r.high for r in rs]),
                                        np.array([r.low for r in rs]), np.array([r.close for r in rs]),
                                        np.array([r.volume for r in rs]))
                        break
                    except Exception as e:  # noqa: BLE001
                        print(f"  bars {day}: {e}"[:160], flush=True)
                        time.sleep(5)
            pickle.dump(out, open(p + ".tmp", "wb"))
            os.replace(p + ".tmp", p)
            print(f"{day}: {len(out)} syms", flush=True)
    elif what == "spreads":
        store = json.load(open(ef.SPR)) if os.path.exists(ef.SPR) else {}
        # every 3rd day (plus every day of the most recent 20) to keep the request count sane;
        # unsampled days fall back to the name's median across sampled days
        pick = [d for k, d in enumerate(days) if k % 3 == 0 or k >= len(days) - 20]
        for k, day in enumerate(pick):
            d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
            for hh, mm in ((9, 31), (13, 30), (15, 50)):
                ef.fetch_spreads(cl, d.replace(hour=hh, minute=mm).timestamp(), U2, 2, store)
            if k % 5 == 0 or k == len(pick) - 1:
                json.dump(store, open(ef.SPR, "w"))
            print(f"spreads {day}: {len(store)}", flush=True)
        json.dump(store, open(ef.SPR, "w"))


if __name__ == "__main__":
    main()
