#!/usr/bin/env python3
"""Minute-bar warehouse for the liquid universe (bottleneck plan item 1, operator 10/16).

Every US common stock passing the gap-down study's liquidity rule on the prior day (RAW close >= $10, 20-day mean
dollar volume >= $20M; ETFs/funds excluded; ACTIVE + INACTIVE assets) gets its RTH 1-minute bars (09:30-15:59 ET)
for the last N sessions, on IEX (what the live desk sees) and SIP (fills/markouts). Data comes ONLY from the
data-only Alpaca account in secrets_data.json; never the desk's key.

  .venv/bin/python tools/studies/warehouse_fetch.py --sessions 120 --feeds iex,sip

Writes ai_reports/warehouse/<feed>/<day>.npz: syms (str), sym_idx, minute (ET minute of day), o, h, l, c, v.
A day is written only when every chunk succeeded, so a rerun refetches incomplete days. Split-adjusted.
Needs ai_reports/gapdown/daily.pkl (the gap-down study's liquidity table).
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
OUT = os.path.join(ROOT, "ai_reports", "warehouse")
ET = ZoneInfo("America/New_York")
CHUNK = 25                 # 25 names x 390 bars < the 10k-bar page: one request each, so pacing controls the rate
REQ_GAP_S = 0.6
WORKERS = 3


def _client():
    from alpaca.data.historical import StockHistoricalDataClient
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    return StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])


def sessions_and_universe(n: int, last: str | None = None) -> tuple[list[str], dict[str, list[str]]]:
    d = pickle.load(open(os.path.join(ROOT, "ai_reports", "gapdown", "daily.pkl"), "rb"))
    cal = sorted(d["adj"]["SPY"])
    if last:
        cal = [x for x in cal if x <= last]
    days = cal[-n:]
    want = set(days)
    uni: dict[str, list[str]] = defaultdict(list)
    for sym, ds in d["liquid"].items():
        for x in ds:
            if x in want:
                uni[x].append(sym)
    return days, {k: sorted(v) for k, v in uni.items()}


def pack(day: str, rows: dict[str, list]) -> dict:
    syms = sorted(rows)
    idx, mins, o, h, l_, c, v = [], [], [], [], [], [], []
    for i, s in enumerate(syms):
        for b in rows[s]:
            t = b.timestamp.astimezone(ET)
            m = t.hour * 60 + t.minute
            if not (570 <= m < 960):
                continue
            idx.append(i); mins.append(m)
            o.append(b.open); h.append(b.high); l_.append(b.low); c.append(b.close); v.append(b.volume)
    return {"syms": np.array(syms), "sym_idx": np.array(idx, np.int32), "minute": np.array(mins, np.int16),
            "o": np.array(o, np.float32), "h": np.array(h, np.float32), "l": np.array(l_, np.float32),
            "c": np.array(c, np.float32), "v": np.array(v, np.float32)}


def fetch_day(cl, day: str, syms: list[str], feed: str) -> tuple[dict | None, list[str]]:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    y, m, dd = map(int, day.split("-"))
    t0, t1 = datetime(y, m, dd, 9, 30, tzinfo=ET), datetime(y, m, dd, 15, 59, 59, tzinfo=ET)
    fd = DataFeed.IEX if feed == "iex" else DataFeed.SIP
    rows: dict[str, list] = {}
    log: list[str] = []
    for i in range(0, len(syms), CHUNK):
        chunk = syms[i:i + CHUNK]
        for attempt in range(8):
            try:
                time.sleep(REQ_GAP_S)
                r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Minute,
                                                       start=t0, end=t1, feed=fd, adjustment=Adjustment.SPLIT))
                rows.update(r.data or {})
                break
            except Exception as e:  # noqa: BLE001
                log.append(f"{day} {feed} {chunk[0]}.. try {attempt}: {type(e).__name__} {str(e)[:60]}")
                time.sleep(10 * (attempt + 1))
        else:
            log.append(f"FAILED {day} {feed} {chunk[0]}..{chunk[-1]}")
            return None, log
    return pack(day, rows), log


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=120)
    ap.add_argument("--feeds", default="iex,sip")
    ap.add_argument("--last", help="last session (default: the newest in the liquidity table)")
    a = ap.parse_args()
    days, uni = sessions_and_universe(a.sessions, a.last)
    feeds = [f.strip() for f in a.feeds.split(",") if f.strip()]
    jobs = []
    for f in feeds:
        os.makedirs(os.path.join(OUT, f), exist_ok=True)
        for d in days:
            if not os.path.exists(os.path.join(OUT, f, f"{d}.npz")):
                jobs.append((d, f))
    print(f"warehouse: {len(days)} sessions {days[0]}..{days[-1]}, feeds {feeds}, {len(jobs)} day-files to fetch, "
          f"~{sum(len(uni.get(d, [])) for d in days) // max(1, len(days))} names/day", flush=True)
    cl = _client()

    def run(job):
        d, f = job
        pk, log = fetch_day(cl, d, uni.get(d, []), f)
        if pk is not None:
            path = os.path.join(OUT, f, f"{d}.npz")
            np.savez_compressed(path + ".tmp.npz", **pk)
            os.replace(path + ".tmp.npz", path)
        return d, f, pk, log

    done = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        for d, f, pk, log in ex.map(run, jobs):
            done += 1
            fails = [x for x in log if x.startswith("FAILED")]
            if fails or done % 10 == 0 or done == len(jobs):
                n = 0 if pk is None else len(pk["syms"])
                print(f"  {d} {f}: {'INCOMPLETE ' + fails[0] if fails else f'{n} names'} ({done}/{len(jobs)})",
                      flush=True)
    print("warehouse done", flush=True)


if __name__ == "__main__":
    sys.exit(main())
