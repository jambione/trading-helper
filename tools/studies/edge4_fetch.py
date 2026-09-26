#!/usr/bin/env python3
"""edge4_fetch.py — data for Round-4 (volume and spread signals on free historical SIP), 2026-09-26.

Pre-registration: docs/studies/edge4_prereg.json (written before any fetch or result).

  days    20 warm-up trading days before each universe (L: before edge2 prior_day; C: before 2026-06-18)
          -> ai_reports/edge4/days.json
  bars    SIP 1m RTH bars (09:30-16:00) for L (U2, 108 names) on L warm-up + prior + 250 days and for
          C (327 Stage-B names) on C warm-up + 2026-06-18 + 60 days -> ai_reports/edge4/sip/DAY.pkl
          IEX 1m bars for warm-up days not already cached -> ai_reports/edge4/iex/DAY.pkl
  quotes  S1: SIP NBBO snapshot (last quote in (t-1s, t]) at every minute 09:45..15:50 for the first
          30 LIQUID names on tune[::6] + validate[::3] + holdout[::3] -> ai_reports/edge4/quotes/DAY.pkl
Free data only: historical SIP (request end > 15 min old). Throttled to <= 1 multi-symbol request/s;
bar batches of 25 names keep each call to ~1 page.
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
import edge2_fetch as e2  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = os.path.join(ROOT, "ai_reports", "edge4")
DAYS = os.path.join(OUT, "days.json")
SPLIT = os.path.join(ROOT, "ai_reports", "edge2_split.json")
C_PRIOR = "2026-06-18"
S1_NAMES = ef.LIQUID[:30]


def c_days():
    rows = pickle.load(open(os.path.join(ROOT, "ai_reports", "combined_stage_b_rows.pkl"), "rb"))
    days = sorted(rows)
    return days, sorted({r["sym"] for d in days for r in rows[d]})


def spy_days(cl, last):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    end = datetime.strptime(last, "%Y-%m-%d").replace(tzinfo=ET) + timedelta(days=1)
    bs = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
                                            start=end - timedelta(days=60), end=end, feed=DataFeed.IEX))
    return sorted({r.timestamp.astimezone(ET).strftime("%Y-%m-%d") for r in bs.data["SPY"]})


def get_bars(cl, day, syms, feed):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    out = {}
    for i in range(0, len(syms), 25):
        batch = syms[i:i + 25]
        for attempt in range(4):
            ef.throttle()
            try:
                bs = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=batch, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=d.replace(hour=9, minute=30).astimezone(timezone.utc),
                    end=d.replace(hour=16, minute=0).astimezone(timezone.utc),
                    feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX))
                for sym, rs in (bs.data or {}).items():
                    out[sym] = (np.array([int(r.timestamp.timestamp()) for r in rs], np.int64),
                                np.array([r.open for r in rs], np.float64),
                                np.array([r.high for r in rs], np.float64),
                                np.array([r.low for r in rs], np.float64),
                                np.array([r.close for r in rs], np.float64),
                                np.array([r.volume for r in rs], np.float64))
                break
            except Exception as e:  # noqa: BLE001
                print(f"  {feed} bars {day} {i}: {e}"[:200], flush=True)
                if "subscription" in str(e):
                    raise SystemExit("SIP refused -> stop")
                time.sleep(5 * (attempt + 1))
    return out


def save(p, obj):
    pickle.dump(obj, open(p + ".tmp", "wb"))
    os.replace(p + ".tmp", p)


def main():
    what = sys.argv[1]
    cl = bars.client()
    for sub in ("sip", "iex", "quotes"):
        os.makedirs(os.path.join(OUT, sub), exist_ok=True)
    split = json.load(open(SPLIT))
    cd, cnames = c_days()
    if what == "days":
        if os.path.exists(DAYS):
            print(open(DAYS).read()); return
        lw = [d for d in spy_days(cl, split["prior_day"]) if d < split["prior_day"]][-20:]
        cw = [d for d in spy_days(cl, C_PRIOR) if d < C_PRIOR][-20:]
        json.dump({"L_warm": lw, "C_warm": cw, "C_prior": C_PRIOR, "C_days": cd, "C_names": cnames}, open(DAYS, "w"))
        print("L warm", lw[0], lw[-1], "C warm", cw[0], cw[-1], "C names", len(cnames))
        return
    D = json.load(open(DAYS))
    L_all = D["L_warm"] + [split["prior_day"]] + split["tune"] + split["validate"] + split["holdout"]
    C_all = D["C_warm"] + [C_PRIOR] + cd
    if what == "bars":
        need = {}
        for d in L_all:
            need.setdefault(d, set()).update(e2.U2)
        for d in C_all:
            need.setdefault(d, set()).update(cnames)
        for d in sorted(need):
            p = os.path.join(OUT, "sip", f"{d}.pkl")
            if os.path.exists(p):
                continue
            t0 = time.time()
            b = get_bars(cl, d, sorted(need[d]), "sip")
            save(p, b)
            print(f"sip {d}: {len(b)}/{len(need[d])} syms, {sum(len(x[0]) for x in b.values())} bars, {time.time()-t0:.0f}s", flush=True)
        iex_need = {d: set(e2.U2) for d in D["L_warm"]}
        for d in D["C_warm"]:
            iex_need.setdefault(d, set()).update(cnames)
        for d in sorted(iex_need):
            p = os.path.join(OUT, "iex", f"{d}.pkl")
            if os.path.exists(p):
                continue
            b = get_bars(cl, d, sorted(iex_need[d]), "iex")
            save(p, b)
            print(f"iex {d}: {len(b)} syms", flush=True)
    elif what == "quotes":
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockQuotesRequest
        qd = split["tune"][::6] + split["validate"][::3] + split["holdout"][::3]
        # fetch order interleaved across parts so that any prefix is balanced (same 59-day set)
        a, b, c = split["tune"][::6], split["validate"][::3], split["holdout"][::3]
        qd = [x for i in range(max(len(a), len(b), len(c))) for x in (a[i:i + 1] + b[i:i + 1] + c[i:i + 1])]
        print(f"quote days: {len(qd)}", flush=True)
        for day in qd:
            p = os.path.join(OUT, "quotes", f"{day}.pkl")
            if os.path.exists(p):
                continue
            t0 = time.time()
            arr = {s: np.full((390, 3), np.nan) for s in S1_NAMES}   # spread_bp, bid_size, ask_size
            d = datetime.strptime(day, "%Y-%m-%d").replace(hour=9, minute=31, tzinfo=ET)
            nerr = 0
            for k in range(14, 380):   # snapshot at the close of bar k = 09:31 + k
                end = (d + timedelta(minutes=k)).astimezone(timezone.utc)
                for attempt in range(3):
                    ef.throttle()
                    try:
                        q = cl.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=S1_NAMES,
                                                                   start=end - timedelta(seconds=1), end=end,
                                                                   feed=DataFeed.SIP))
                        for sym, rows in (q.data or {}).items():
                            rows = [r for r in rows if r.bid_price and r.ask_price and r.ask_price >= r.bid_price]
                            if not rows:
                                continue
                            r = max(rows, key=lambda x: x.timestamp)
                            mid = (float(r.ask_price) + float(r.bid_price)) / 2
                            arr[sym][k] = ((float(r.ask_price) - float(r.bid_price)) / mid * 1e4,
                                           float(r.bid_size), float(r.ask_size))
                        break
                    except Exception as e:  # noqa: BLE001
                        nerr += 1
                        print(f"  quotes {day} k{k}: {e}"[:200], flush=True)
                        if "subscription" in str(e):
                            raise SystemExit("SIP refused -> stop")
                        time.sleep(5 * (attempt + 1))
            save(p, arr)
            cov = np.mean([np.isfinite(a[14:380, 0]).mean() for a in arr.values()])
            print(f"quotes {day}: coverage {cov:.0%}, errors {nerr}, {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
