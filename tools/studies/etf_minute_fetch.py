#!/usr/bin/env python3
"""SPY / QQQ SIP 1-minute bars 04:00-15:59 ET, 2016-01 .. 2026-10-15, for fence_bar, pm_power_bar and
anchor_power_bar (preregs as amended). Data-only Alpaca account (secrets_data.json), Adjustment.ALL, paced, resumable.
The held-out window 2026-01-14 .. 2026-04-24 is never requested. Output: ai_reports/etf_minute/<SYM>/<YYYY-MM>.npz with
rows [ts_et_minute_of_day, o, h, l, c, v] and a parallel 'day' array; a fetch log lists any symbol-month that failed.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "ai_reports", "etf_minute")
ET = ZoneInfo("America/New_York")
SYMS = ("SPY", "QQQ")
FIRST, LAST = date(2016, 1, 1), date(2026, 10, 15)
HELD_LO, HELD_HI = date(2026, 1, 14), date(2026, 4, 24)


def ranges() -> list[tuple[str, date, date]]:
    """(label, start, end inclusive) per month, split around the held-out window."""
    out, d = [], FIRST
    while d <= LAST:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = min(nxt - timedelta(days=1), LAST)
        lab = d.strftime("%Y-%m")
        parts = [(d, end)]
        if not (end < HELD_LO or d > HELD_HI):
            parts = [(a, b) for a, b in ((d, min(end, HELD_LO - timedelta(days=1))),
                                         (max(d, HELD_HI + timedelta(days=1)), end)) if a <= b]
        for k, (a, b) in enumerate(parts):
            out.append((lab if len(parts) == 1 else f"{lab}_{k}", a, b))
        d = nxt
    return out


def main() -> None:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    cl = StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])
    failed = []
    for sym in SYMS:
        os.makedirs(os.path.join(OUT, sym), exist_ok=True)
        for lab, a, b in ranges():
            p = os.path.join(OUT, sym, f"{lab}.npz")
            if os.path.exists(p):
                continue
            start = datetime(a.year, a.month, a.day, 4, 0, tzinfo=ET).astimezone(timezone.utc)
            end = datetime(b.year, b.month, b.day, 16, 0, tzinfo=ET).astimezone(timezone.utc)
            for attempt in range(5):
                try:
                    bars = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute,
                                                              start=start, end=end, feed=DataFeed.SIP,
                                                              adjustment=Adjustment.ALL)).data.get(sym, [])
                    break
                except Exception as e:  # noqa: BLE001
                    print(f"  retry {sym} {lab} {attempt + 1}: {str(e)[:80]}", flush=True)
                    time.sleep(15 * (attempt + 1))
            else:
                failed.append(f"{sym} {lab}")
                continue
            rows, days = [], []
            for x in bars:
                t = x.timestamp.astimezone(ET)
                m = t.hour * 60 + t.minute
                if 240 <= m < 960 and not (HELD_LO <= t.date() <= HELD_HI):
                    rows.append([m, x.open, x.high, x.low, x.close, x.volume])
                    days.append(t.strftime("%Y-%m-%d"))
            np.savez_compressed(p + ".tmp.npz", rows=np.array(rows, dtype=float), day=np.array(days))
            os.replace(p + ".tmp.npz", p)
            print(f"{sym} {lab}: {len(rows)} bars", flush=True)
            time.sleep(0.4)
    json.dump({"failed": failed, "finished": datetime.now(ET).isoformat()},
              open(os.path.join(OUT, "fetch_log.json"), "w"), indent=1)
    print("failed:", failed)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
