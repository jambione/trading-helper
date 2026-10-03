#!/usr/bin/env python3
"""allsym_daily_fetch.py — SIP daily bars for every US common stock, active AND inactive, for a date range.

Feeds market_temperature_study.py and halt_reopen_study.py (pre-registered 5eb0044). The liquid panels
(lh_cache, edge5) hold only liquid names, so they cannot count small-cap gappers.

Writes ai_reports/allsym/daily_LO_HI.pkl: {symbol: [(date, o, h, l, c, v), ...]} (split-adjusted, raw
otherwise), plus assets.json. Read-only market data; <= 1 request per ~0.4 s.

USAGE (mini):  .venv/bin/python tools/studies/allsym_daily_fetch.py 2025-09-01 2026-10-02
"""
from __future__ import annotations

import json
import os
import pickle
import sys
import time
from datetime import datetime

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
from ticker_filters import is_common, is_levered_etp  # noqa: E402
from overnight_book import FUNDISH  # noqa: E402  (the overnight book's fund/SPAC/preferred name screen)

OUT = os.path.join(ROOT, "ai_reports", "allsym")


def assets() -> dict[str, str]:
    p = os.path.join(OUT, "assets.json")
    if os.path.exists(p):
        return json.load(open(p))
    sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest
    import ai_catalyst
    tc = ai_catalyst._trading_client()
    names = {}
    for st in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
        for a in tc.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=st)):
            exch = str(getattr(a, "exchange", "") or "")
            if "OTC" in exch.upper():
                continue
            names[a.symbol] = a.name or ""
    json.dump(names, open(p, "w"))
    return names


def main():
    lo, hi = sys.argv[1], sys.argv[2]
    os.makedirs(OUT, exist_ok=True)
    names = assets()
    syms = sorted(s for s, n in names.items()
                  if s.isalpha() and is_common(s) and not is_levered_etp(s, n) and not FUNDISH.search(n.upper()))
    print(f"assets {len(names)}, common {len(syms)}", flush=True)
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    cl = bars.client()
    data, failed = {}, []
    start = datetime.strptime(lo, "%Y-%m-%d").replace(tzinfo=bars.ET)
    end = datetime.strptime(hi, "%Y-%m-%d").replace(hour=23, tzinfo=bars.ET)
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        for attempt in range(4):
            try:
                r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                                                       start=start, end=end, feed=DataFeed.SIP,
                                                       adjustment=Adjustment.SPLIT))
                for s, rows in (r.data or {}).items():
                    data[s] = [(b.timestamp.astimezone(bars.ET).strftime("%Y-%m-%d"), float(b.open), float(b.high),
                                float(b.low), float(b.close), float(b.volume)) for b in rows]
                break
            except Exception as e:  # noqa: BLE001
                print(f"  chunk {i} try {attempt}: {str(e)[:80]}", flush=True)
                time.sleep(5 * (attempt + 1))
        else:
            failed.append(i)
        time.sleep(0.4)
        if i % 2000 == 0:
            print(f"  {i}/{len(syms)} symbols, {len(data)} with bars", flush=True)
    pickle.dump(data, open(os.path.join(OUT, f"daily_{lo}_{hi}.pkl"), "wb"))
    print(f"done: {len(data)} symbols with bars; failed chunks {failed}", flush=True)


if __name__ == "__main__":
    main()
