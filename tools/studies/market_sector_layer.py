#!/usr/bin/env python3
"""market_sector_layer - the market + sector tailwind as a layer on arm_then_fire's wide fires.
Spec: docs/studies/market_sector_layer_prereg.json (amended_1).

    .venv/bin/python tools/studies/market_sector_layer.py fetch     # sector ETF SIP 1-minute RTH bars 2026-01-15..11-19 (after 16:05 ET)
"""
from __future__ import annotations

import json
import pickle
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
OUT = ROOT / "ai_reports" / "market_sector_layer"
ET = ZoneInfo("America/New_York")
ETFS = ("SPY", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC", "SMH", "XBI", "IBIT", "KRE")
START, END = "2026-01-15", "2026-11-19"


def fetch():
    """One request per ETF per ~month (RTH filtered after), resumable. -> OUT/etf_bars.pkl {sym: {day: (o,h,l,c,v) 390-arrays}}."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment
    now = datetime.now(ET)
    if now.weekday() < 5 and "0900" <= now.strftime("%H%M") < "1605":
        sys.exit("market hours - rerun after 16:05 ET (shared data key)")
    k = json.load(open(ROOT / "secrets_data.json"))
    cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    OUT.mkdir(parents=True, exist_ok=True)
    dst = OUT / "etf_bars.pkl"
    res = pickle.load(open(dst, "rb")) if dst.exists() else {}
    failed = []
    for sym in ETFS:
        if sym in res and res[sym].get("_done"):
            continue
        res[sym] = {}
        a = datetime.fromisoformat(START + "T09:30").replace(tzinfo=ET)
        end = datetime.fromisoformat(END + "T16:00").replace(tzinfo=ET)
        while a < end:
            b = min(a + timedelta(days=31), end)
            got = None
            for attempt in range(6):
                try:
                    got = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute, start=a, end=b,
                                                             feed=DataFeed.SIP, adjustment=Adjustment.RAW))
                    break
                except Exception as e:                  # noqa: BLE001
                    time.sleep(60 if "429" in str(e) else 5 * (attempt + 1))
            if got is None:
                failed.append((sym, a.date().isoformat()))
            else:
                for r in (got.data or {}).get(sym, []):
                    t = r.timestamp.astimezone(ET)
                    m = (t.hour - 9) * 60 + t.minute - 30
                    if not 0 <= m < 390:
                        continue
                    day = t.strftime("%Y-%m-%d")
                    arr = res[sym].setdefault(day, np.full((5, 390), np.nan))
                    arr[:, m] = (r.open, r.high, r.low, r.close, r.volume)
            a = b
        res[sym]["_done"] = True
        pickle.dump(res, open(dst, "wb"))
        print(sym, len(res[sym]) - 1, "days", flush=True)
    print("FETCH_DONE failed", failed, flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fetch":
        fetch()
    else:
        sys.exit(__doc__)
