#!/usr/bin/env python3
"""ETF round-trip cost from a fixed NBBO quote sample (evening_family_2026-11-07 prereg, common.cost). Read-only.

For each ETF: 10 sessions chosen by seed sha256('etf-cost'|ETF) from the warehouse days, 20 minutes per session between 09:45 and
15:30 chosen the same way; the NBBO in force at the start of each minute (last SIP quote in the 5 s before it). Spread in bp of mid.
Round trip = median spread + 1 bp; stress = 2x. Output ai_reports/warehouse/etf/cost.json.

    .venv/bin/python tools/studies/etf_cost_sample.py      # mini; Alpaca historical quotes (free SIP, > 15 min old)
"""
from __future__ import annotations

import hashlib
import json
import random
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
ETFS = ["SMH", "SOXX", "IBIT", "IGV", "XBI", "URA", "XLE", "XLF", "ARKK", "QQQ", "IWM", "SPY"]
OUT = ROOT / "ai_reports" / "warehouse" / "etf" / "cost.json"


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16)


def sample_plan(etf: str, days: list[str], n_days: int = 10, n_min: int = 20):
    """Pure: [(day, minute_of_day)] for one ETF - the n_days sessions with the lowest sha256('etf-cost'|ETF|day), and in each the
    n_min minutes (09:45-15:30) with the lowest sha256('etf-cost'|ETF|day|minute) (prereg wording, skeptic round 1)."""
    ds = sorted(sorted(days, key=lambda d: _h("etf-cost", etf, d))[:n_days])
    plan = []
    for d in ds:
        mins = sorted(range(9 * 60 + 45, 15 * 60 + 30 + 1), key=lambda m: _h("etf-cost", etf, d, m))[:n_min]
        plan += [(d, m) for m in sorted(mins)]
    return plan


def spread_bp(bid: float, ask: float) -> float | None:
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (ask - bid) / ((ask + bid) / 2) * 1e4


def make_client():
    import alpaca_api as aa
    from config import load_config_ro
    return aa.connect_data_client(dict(load_config_ro() or {}))


def spread_at(client, etf: str, day: str, minute: int):
    """NBBO spread in bp of mid in force at the start of `minute` (minute of day, ET): the last SIP quote in the 5 s before it. None if absent."""
    from alpaca.data.requests import StockQuotesRequest
    from alpaca.data.enums import DataFeed
    t1 = datetime.fromisoformat(day).replace(hour=minute // 60, minute=minute % 60, tzinfo=ET)
    try:
        q = client.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=etf, start=t1 - timedelta(seconds=5), end=t1, feed=DataFeed.SIP))
        rows = (q.data or {}).get(etf, [])
    except Exception as e:  # noqa: BLE001
        print("ERR", etf, day, minute, str(e)[:80])
        return None
    if not rows:
        return None
    last = rows[-1]
    return spread_bp(float(last.bid_price), float(last.ask_price))


def main():
    client = make_client()
    days = sorted(p.stem for p in (ROOT / "ai_reports" / "warehouse" / "etf").glob("20??-??-??.npz"))
    res = {}
    for etf in ETFS:
        vals = []
        for d, m in sample_plan(etf, days):
            s_ = spread_at(client, etf, d, m)
            if s_ is not None:
                vals.append(s_)
            time.sleep(0.05)
        med = statistics.median(vals) if vals else None
        res[etf] = {"n": len(vals), "median_spread_bp": med, "rt_bp": None if med is None else med + 1.0,
                    "stress_bp": None if med is None else 2 * (med + 1.0)}
        print(etf, res[etf])
    OUT.write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
