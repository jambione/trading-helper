#!/usr/bin/env python3
"""edge5_fetch.py — data for Round-5 addendum T5/T6/T7 (2026-09-26). Pre-registration: docs/studies/edge5_prereg.json.

  noms    desk seed nominations from ai_reports/tapes/*/{shadow,rejects}.jsonl + sessions/*/{sources,discord}.jsonl.gz
          -> ai_reports/edge5/noms.json  [(day, source, symbol, first_ts, price)]
  daily   SIP daily bars: U2 2024-06-01..2026-09-25, nominated symbols 2026-07-01..2026-09-25 -> ai_reports/edge5/daily.pkl
  news    Alpaca historical news (Benzinga) for U2, 2025-08-15..2026-09-25, paged by hand at <= 1 request/s
          -> ai_reports/edge5/news.pkl  {id: (created_ts, [symbols])}
Free data only; read-only elsewhere.
"""
from __future__ import annotations

import glob
import gzip
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
OUT = os.path.join(ROOT, "ai_reports", "edge5")
AR = os.path.join(ROOT, "ai_reports")


def noms():
    first = {}
    def add(ts, src, sym, px):
        if not sym or not src or not ts:
            return
        day = datetime.fromtimestamp(float(ts), ET).strftime("%Y-%m-%d")
        k = (day, str(src), str(sym).upper())
        if k not in first or float(ts) < first[k][0]:
            first[k] = (float(ts), px)
    for f in glob.glob(os.path.join(AR, "tapes", "*", "shadow.jsonl")) + glob.glob(os.path.join(AR, "tapes", "*", "rejects.jsonl")):
        for line in open(f):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            add(r.get("ts"), r.get("source"), r.get("symbol"), r.get("price"))
    for f in glob.glob(os.path.join(AR, "sessions", "*", "sources.jsonl.gz")):
        for line in gzip.open(f, "rt"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") == "enter":
                add(r.get("ts"), r.get("source"), r.get("symbol"), None)
    for f in glob.glob(os.path.join(AR, "sessions", "*", "discord.jsonl.gz")):
        for line in gzip.open(f, "rt"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            for sym in (r.get("tickers") or r.get("symbols") or ([r["symbol"]] if r.get("symbol") else [])):
                add(r.get("ts"), "discord", sym, None)
    rows = sorted((d, s, y, t, p) for (d, s, y), (t, p) in first.items())
    json.dump(rows, open(os.path.join(OUT, "noms.json"), "w"))
    from collections import Counter
    print(len(rows), Counter(r[1] for r in rows).most_common(), rows[0][0], rows[-1][0])


def daily(cl):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    rows = json.load(open(os.path.join(OUT, "noms.json")))
    nsyms = sorted({r[2] for r in rows if r[2].isalpha() and len(r[2]) <= 5} - set(e2.U2))
    out = {}
    for syms, start in ((sorted(e2.U2), datetime(2024, 6, 1, tzinfo=ET)), (nsyms, datetime(2026, 7, 1, tzinfo=ET))):
        n_per = max(1, 9000 // (560 if start.year == 2024 else 60))
        for i in range(0, len(syms), n_per):
            ef.throttle()
            try:
                bs = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms[i:i + n_per], timeframe=TimeFrame.Day,
                                                        start=start, end=datetime(2026, 9, 26, tzinfo=ET), feed=DataFeed.SIP,
                                                        adjustment="all"))
                for s, rs in (bs.data or {}).items():
                    out[s] = [(r.timestamp.astimezone(ET).strftime("%Y-%m-%d"), float(r.open), float(r.close), float(r.volume)) for r in rs]
            except Exception as e:  # noqa: BLE001
                print("daily err", i, str(e)[:160], flush=True)
        print(f"daily: {len(out)} symbols", flush=True)
    pickle.dump(out, open(os.path.join(OUT, "daily.pkl"), "wb"))


def news(cl):
    import requests
    hdr = {"APCA-API-KEY-ID": cl._api_key, "APCA-API-SECRET-KEY": cl._secret_key}
    p = os.path.join(OUT, "news.pkl")
    store = pickle.load(open(p, "rb")) if os.path.exists(p) else {"items": {}, "done_months": []}
    d = datetime(2025, 8, 1, tzinfo=timezone.utc)
    while d < datetime(2026, 9, 26, tzinfo=timezone.utc):
        nxt = (d + timedelta(days=32)).replace(day=1)
        mk = d.strftime("%Y-%m")
        if mk in store["done_months"]:
            d = nxt; continue
        tok, n0, npages = None, len(store["items"]), 0
        while True:
            params = {"symbols": ",".join(sorted(e2.U2)), "start": d.isoformat().replace("+00:00", "Z"),
                      "end": min(nxt, datetime(2026, 9, 26, tzinfo=timezone.utc)).isoformat().replace("+00:00", "Z"),
                      "limit": 50, "sort": "asc", "include_content": "false", "exclude_contentless": "false"}
            if tok:
                params["page_token"] = tok
            ef.throttle()
            try:
                r = requests.get("https://data.alpaca.markets/v1beta1/news", params=params, headers=hdr, timeout=30)
                r.raise_for_status()
                j = r.json()
            except Exception as e:  # noqa: BLE001
                print("news err", mk, str(e)[:160], flush=True); time.sleep(5); continue
            npages += 1
            for it in j.get("news", []):
                ts = datetime.fromisoformat(it["created_at"].replace("Z", "+00:00")).timestamp()
                store["items"][it["id"]] = (ts, it.get("symbols") or [], it.get("source"))
            tok = j.get("next_page_token")
            if not tok:
                break
        store["done_months"].append(mk)
        pickle.dump(store, open(p, "wb"))
        print(f"news {mk}: +{len(store['items']) - n0} items, {npages} pages", flush=True)
        d = nxt


def daily_long(cl):
    """U2 SIP daily bars (split+dividend adjusted) from 2016-01 -> ai_reports/edge5/daily_long.pkl (T1/T3)."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    syms = sorted(e2.U2)
    out = {}
    for i in range(0, len(syms), 3):
        ef.throttle()
        bs = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms[i:i + 3], timeframe=TimeFrame.Day,
                                                start=datetime(2016, 1, 1, tzinfo=ET), end=datetime(2026, 9, 26, tzinfo=ET),
                                                feed=DataFeed.SIP, adjustment="all"))
        for s, rs in (bs.data or {}).items():
            out[s] = [(r.timestamp.astimezone(ET).strftime("%Y-%m-%d"), float(r.open), float(r.close), float(r.volume),
                       float(r.high), float(r.low)) for r in rs]
    pickle.dump(out, open(os.path.join(OUT, "daily_long.pkl"), "wb"))
    print(f"daily_long: {len(out)} symbols, SPY {out['SPY'][0][0]}..{out['SPY'][-1][0]}, min start {min(v[0][0] for v in out.values())}")


def main():
    os.makedirs(OUT, exist_ok=True)
    what = sys.argv[1]
    if what == "noms":
        return noms()
    cl = bars.client()
    {"daily": daily, "news": news, "daily_long": daily_long}[what](cl)


if __name__ == "__main__":
    main()
