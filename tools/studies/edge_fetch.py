#!/usr/bin/env python3
"""edge_fetch.py — data for the Round-1 strategy-edge study (2026-09-26).

1. IEX 1-minute bars (free feed), 04:00-16:05 ET, for every trading day of the
   combined-score Stage B window (2026-06-22..2026-09-15, 60 days) plus the day
   before, for (a) every symbol in Claude's Stage B universe
   (combined_daily.pkl keys, ~759 names the desk nominated in Sept) and (b) a
   fixed, pre-registered liquid large-cap/ETF list (LIQUID below).
   -> ai_reports/edge_iex_bars/DAY.pkl  {sym: (t int64 s, o, h, l, c, v) numpy}
2. Spread samples with Claude's spread model (combined_score_study.Spreads:
   SIP (ask-bid)/mid, median over a short window before the moment): liquid
   list at 8 moments/day; Stage B names at 09:36 and 15:45 (the cached spreads
   only cover 09:45-15:15). -> ai_reports/edge_spreads.json  "SYM|ts" -> bp
Throttled to <= 1 multi-symbol request per second.
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
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = os.path.join(ROOT, "ai_reports", "edge_iex_bars")
SPR = os.path.join(ROOT, "ai_reports", "edge_spreads.json")
LIQUID = """SPY QQQ IWM DIA SMH XLF XLE XLK TLT GLD AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO JPM V MA UNH
XOM LLY JNJ PG HD COST ABBV MRK CVX KO PEP WMT BAC ORCL CRM AMD NFLX ADBE CSCO TMO ACN MCD ABT LIN DHR WFC
TXN QCOM INTU AMGN PM IBM CAT GE HON UNP LOW SPGI GS MS BLK AMAT MU ISRG NOW BKNG PLTR UBER SBUX DE AXP C
SCHW MDT ADP GILD LRCX PANW KLAC MRVL ADI SNPS CDNS REGN VRTX TJX CMCSA T VZ PFE DIS NKE BA COP MO SO DUK CVS""".split()

_last = [0.0]


def throttle():
    dt = time.time() - _last[0]
    if dt < 1.05:
        time.sleep(1.05 - dt)
    _last[0] = time.time()


def trading_days():
    rows = pickle.load(open(os.path.join(ROOT, "ai_reports", "combined_stage_b_rows.pkl"), "rb"))
    days = sorted(rows)
    return days, rows


def fetch_bars(cl, day, syms):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    out = {}
    for i in range(0, len(syms), 100):
        batch = syms[i:i + 100]
        for attempt in range(3):
            throttle()
            try:
                bs = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=batch, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=d.replace(hour=4).astimezone(timezone.utc),
                    end=d.replace(hour=16, minute=5).astimezone(timezone.utc), feed=DataFeed.IEX))
                for sym, rs in (bs.data or {}).items():
                    out[sym] = (np.array([int(r.timestamp.timestamp()) for r in rs], np.int64),
                                np.array([r.open for r in rs], np.float64),
                                np.array([r.high for r in rs], np.float64),
                                np.array([r.low for r in rs], np.float64),
                                np.array([r.close for r in rs], np.float64),
                                np.array([r.volume for r in rs], np.float64))
                break
            except Exception as e:  # noqa: BLE001
                print(f"  bars {day} {i}: {e}"[:160], flush=True)
                time.sleep(5)
    return out


def fetch_spreads(cl, t_end, syms, window_s, store):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    need = [s for s in syms if f"{s}|{int(t_end)}" not in store]
    end = datetime.fromtimestamp(t_end, timezone.utc)
    for i in range(0, len(need), 100):
        batch = need[i:i + 100]
        got = {}
        throttle()
        try:
            q = cl.get_stock_quotes(StockQuotesRequest(
                symbol_or_symbols=batch, start=end - timedelta(seconds=window_s), end=end,
                feed=DataFeed.SIP))
            for sym, rows in (q.data or {}).items():
                sp = sorted((float(r.ask_price) - float(r.bid_price)) /
                            ((float(r.ask_price) + float(r.bid_price)) / 2) * 1e4
                            for r in rows if r.bid_price and r.ask_price and r.ask_price >= r.bid_price)
                if sp:
                    got[sym] = sp[len(sp) // 2]
        except Exception as e:  # noqa: BLE001
            print(f"  quotes: {e}"[:160], flush=True)
            continue
        for s in batch:
            store[f"{s}|{int(t_end)}"] = got.get(s)


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "bars"
    os.makedirs(OUT, exist_ok=True)
    cl = bars.client()
    days, rows = trading_days()
    daily = pickle.load(open(os.path.join(ROOT, "ai_reports", "combined_daily.pkl"), "rb"))
    src_syms = sorted({s for s, _d in daily})
    allsyms = sorted(set(src_syms) | set(LIQUID))
    if what == "bars":
        spy = [x[0] for x in daily[("SPY", "2026-09-16")]] if ("SPY", "2026-09-16") in daily else []
        prev = max([d for d in spy if d < days[0]] or ["2026-06-18"])
        for day in [prev] + days:
            p = os.path.join(OUT, f"{day}.pkl")
            if os.path.exists(p):
                continue
            t0 = time.time()
            b = fetch_bars(cl, day, allsyms)
            pickle.dump(b, open(p + ".tmp", "wb"))
            os.replace(p + ".tmp", p)
            print(f"{day}: {len(b)} syms, {sum(len(x[0]) for x in b.values())} bars, {time.time()-t0:.0f}s", flush=True)
    else:
        store = json.load(open(SPR)) if os.path.exists(SPR) else {}
        for k, day in enumerate(days):
            d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
            cur = sorted({r["sym"] for r in rows[day]})
            for hh, mm in ((9, 36), (10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0), (15, 45)):
                te = d.replace(hour=hh, minute=mm).timestamp()
                fetch_spreads(cl, te, LIQUID, 2, store)
                if (hh, mm) in ((9, 36), (15, 45)):
                    fetch_spreads(cl, te, cur, 3, store)
            json.dump(store, open(SPR, "w"))
            print(f"spreads {day}: {len(store)} total", flush=True)


if __name__ == "__main__":
    main()
