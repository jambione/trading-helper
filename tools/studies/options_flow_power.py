#!/usr/bin/env python3
"""Options-flow study, stock-only power check (skeptic round 1 item 4), BEFORE any option data is fetched.

Universe per signal day t: the top N names by median SIP dollar volume over the 20 trading days before t (prior close
>= $10 RAW), from the gap-down study's ever-liquid list (active + inactive). Next-day return approximated by the t+1
daily open -> close (SIP, RAW); the registered trade is 09:45 -> close, whose dispersion is slightly smaller, so this
MDE is conservative. Placebo per C1: a seeded random set of the long-set size minus the all-name mean, per day.

For each config (N, fraction) prints MDE = (2.0 + 0.84) x SD(day placebo LIFT) / sqrt(days) per half. No option
data; no signal; the held-out window 2026-01-14 .. 04-24 is never fetched.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
import random
import statistics
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "ai_reports", "options_flow")
ET = ZoneInfo("America/New_York")
RANGES = (("2023-12-01", "2026-01-13"), ("2026-04-27", "2026-10-15"))
HALVES = {"A": [("2024-03-01", "2025-06-30")], "B": [("2025-07-01", "2026-01-12"), ("2026-05-26", "2026-10-14")]}
CONFIGS = ((100, 0.10), (100, 0.20), (200, 0.10), (200, 0.20))


def fetch_daily() -> dict:
    p = os.path.join(OUT, "stock_daily_raw.pkl")
    if os.path.exists(p):
        return pickle.load(open(p, "rb"))
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    cl = StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])
    syms = sorted(pickle.load(open(os.path.join(ROOT, "ai_reports", "gapdown", "daily.pkl"), "rb"))["liquid"])
    out, failed = {}, []
    for a0, b0 in RANGES:
        a = datetime.fromisoformat(a0).replace(tzinfo=ET)
        b = datetime.fromisoformat(b0).replace(hour=16, tzinfo=ET)
        for i in range(0, len(syms), 100):
            for attempt in range(6):
                try:
                    got = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms[i:i + 100], timeframe=TimeFrame.Day,
                                                             start=a, end=b, feed=DataFeed.SIP,
                                                             adjustment=Adjustment.RAW)).data
                    break
                except Exception as e:  # noqa: BLE001
                    print(f"  retry {a0} {i}: {str(e)[:80]}", flush=True)
                    time.sleep(15 * (attempt + 1))
            else:
                failed.append(f"{a0} {i}")
                continue
            for sym, bars in got.items():
                for x in bars:
                    d = x.timestamp.astimezone(ET).strftime("%Y-%m-%d")
                    if not ("2026-01-14" <= d <= "2026-04-24"):
                        out.setdefault(sym, {})[d] = (x.open, x.close, x.volume)
            time.sleep(0.4)
    if failed:
        raise SystemExit(f"fetch failures: {failed}")
    os.makedirs(OUT, exist_ok=True)
    pickle.dump(out, open(p, "wb"))
    return out


def in_half(d: str, h: str) -> bool:
    return any(a <= d <= b for a, b in HALVES[h])


def main() -> None:
    daily = fetch_daily()
    days = sorted({d for v in daily.values() for d in v})
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    res = {}
    for n_univ, frac in CONFIGS:
        per = {"A": [], "B": []}
        for i, t in enumerate(days):
            h = "A" if in_half(t, "A") else "B" if in_half(t, "B") else None
            if h is None or t not in nxt or i < 20:
                continue
            prior = days[i - 20:i]
            if "2026-01-13" < prior[0] < "2026-04-27" or any("2026-01-14" <= p <= "2026-04-24" for p in prior):
                continue
            ranks = []
            for s, v in daily.items():
                if t not in v or prior[-1] not in v or v[prior[-1]][1] < 10:
                    continue
                dv = [v[p][1] * v[p][2] for p in prior if p in v]
                if len(dv) >= 15:
                    ranks.append((statistics.median(dv), s))
            univ = [s for _, s in sorted(ranks, reverse=True)[:n_univ]]
            t1 = nxt[t]
            rets = {s: daily[s][t1][1] / daily[s][t1][0] - 1 for s in univ if t1 in daily[s] and daily[s][t1][0] > 0}
            if len(rets) < 30:
                continue
            k = max(1, int(len(rets) * frac))
            rng = random.Random(int(hashlib.sha256(f"options-flow-placebo|{t}|{n_univ}|{frac}".encode())
                                    .hexdigest()[:16], 16))
            pick = rng.sample(sorted(rets), k)
            per[h].append(statistics.mean(rets[s] for s in pick) - statistics.mean(rets.values()))
        res[f"top{n_univ}_{'decile' if frac == 0.10 else 'quintile'}"] = {
            h: {"days": len(v), "mde_bp": round((2.0 + 0.84) * statistics.stdev(v) / math.sqrt(len(v)) * 1e4, 2)}
            for h, v in per.items() if len(v) >= 2}
    print(json.dumps(res, indent=1))
    json.dump(res, open(os.path.join(OUT, "power_stock_only.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
