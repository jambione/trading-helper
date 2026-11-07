#!/usr/bin/env python3
"""Fetch 1-minute SIP bars for thematic ETFs into ai_reports/warehouse/etf/DAY.npz (same arrays as warehouse/sip:
syms, sym_idx, minute (minutes since midnight ET, 570-959), o h l c v). Free historical SIP (older than 15 min). Read-only on the account;
idempotent (skips days already written). For the sector-ETF lead-lag study (Argon idea 3, 11/07).

    .venv/bin/python tools/studies/etf_bars_fetch.py            # every session in warehouse/sip
    .venv/bin/python tools/studies/etf_bars_fetch.py 2026-10-14 # one day
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
ETFS = ["SMH", "SOXX", "IBIT", "IGV", "XBI", "URA", "XLE", "XLF", "ARKK", "QQQ", "IWM", "SPY"]
OUT = ROOT / "ai_reports" / "warehouse" / "etf"


def day_arrays(df, day):
    """Pure: bars DataFrame (MultiIndex symbol,timestamp) -> the npz arrays for one session (regular hours 570-959 only)."""
    syms, sym_idx, minute, o, h, l, c, v = [], [], [], [], [], [], [], []
    for s in sorted(set(df.index.get_level_values(0))):
        x = df.xs(s, level=0)
        k = len(syms)
        syms.append(s)
        for t, a, b, d, e, f in zip(x.index, x["open"], x["high"], x["low"], x["close"], x["volume"]):
            tt = t.astimezone(ET)
            m = tt.hour * 60 + tt.minute
            if 570 <= m <= 959 and tt.strftime("%Y-%m-%d") == day:
                sym_idx.append(k); minute.append(m); o.append(a); h.append(b); l.append(d); c.append(e); v.append(f)
    return {"syms": np.array(syms), "sym_idx": np.array(sym_idx, dtype=np.int32), "minute": np.array(minute, dtype=np.int16),
            "o": np.array(o, dtype=np.float32), "h": np.array(h, dtype=np.float32), "l": np.array(l, dtype=np.float32),
            "c": np.array(c, dtype=np.float32), "v": np.array(v, dtype=np.float64)}


def main(argv=None):
    import alpaca_api as aa
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from config import load_config_ro
    a = argv if argv is not None else sys.argv[1:]
    days = a or sorted(p.stem for p in (ROOT / "ai_reports" / "warehouse" / "sip").glob("*.npz"))
    OUT.mkdir(parents=True, exist_ok=True)
    client = aa.connect_data_client(dict(load_config_ro() or {}))
    n = 0
    for day in days:
        f = OUT / f"{day}.npz"
        if f.exists():
            continue
        s0 = datetime.fromisoformat(day + "T09:30:00").replace(tzinfo=ET)
        s1 = datetime.fromisoformat(day + "T16:00:00").replace(tzinfo=ET)
        try:
            df = client.get_stock_bars(StockBarsRequest(symbol_or_symbols=ETFS, timeframe=TimeFrame.Minute, start=s0, end=s1, feed=DataFeed.SIP)).df
        except Exception as e:  # noqa: BLE001
            print("ERR", day, str(e)[:120]); time.sleep(2); continue
        if df is None or df.empty:
            print("EMPTY", day); continue
        np.savez_compressed(f, **day_arrays(df, day))
        n += 1
        time.sleep(0.4)
    print(f"wrote {n} days to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
