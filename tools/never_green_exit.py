#!/usr/bin/env python3
"""Never-green exit: would "not +X% within N min -> exit" have helped real trades?

For every paper round trip in ai_reports/fills/<day>.jsonl (buy fill -> sell
fill), reads SIP 1m bars after the entry. If the trade's best high has not
reached +X% by the close of minute N and the real exit came later, the trade
is re-scored as exiting at that minute's close less a cost allowance (the real
fills already paid the spread on the way in; --cost covers the extra exit).
Otherwise the real result stands. Read-only apart from a bar cache.

Usage (on the mini, days older than 15 min)::

    .venv/bin/python tools/never_green_exit.py --days 2026-09-16:2026-09-25
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")


def trips(day: str) -> list[tuple[str, float, float, float, float]]:
    p = ROOT / "ai_reports" / "fills" / f"{day}.jsonl"
    if not p.exists():
        return []
    fills = []
    for line in open(p):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("event") == "fill" and r.get("filled_at") and r.get("filled_avg_price"):
            fills.append((datetime.fromisoformat(r["filled_at"]).timestamp(), r["side"],
                          r["symbol"].upper(), float(r["filled_avg_price"])))
    fills.sort()
    open_, out = {}, []
    for t, side, s, px in fills:
        if side == "buy":
            open_[s] = (t, px)
        elif s in open_:
            bt, bp = open_.pop(s)
            out.append((s, bt, bp, t, px))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", required=True, help="YYYY-MM-DD or FROM:TO")
    ap.add_argument("--cost", type=float, default=0.05, help="extra exit cost, %% of price")
    a = ap.parse_args()
    if ":" in a.days:
        d0, d1 = (datetime.strptime(x, "%Y-%m-%d") for x in a.days.split(":"))
        days = [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((d1 - d0).days + 1)]
    else:
        days = [a.days]
    import ai_entry_watch as ew
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    cl = ew._data_client()
    cache_p = Path.home() / "replay_cache" / "never_green_bars.pkl"
    cache = pickle.loads(cache_p.read_bytes()) if cache_p.exists() else {}
    rows = []
    for day in days:
        for s, bt, bp, st, sp in trips(day):
            key = (s, day)
            if key not in cache:
                o = datetime.strptime(day, "%Y-%m-%d").replace(hour=9, minute=30, tzinfo=ET)
                b = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=s, timeframe=TimeFrame.Minute,
                    start=o.astimezone(timezone.utc),
                    end=o.replace(hour=16, minute=0).astimezone(timezone.utc), feed=DataFeed.SIP)).data.get(s) or []
                cache[key] = [(x.timestamp.timestamp(), float(x.high), float(x.close)) for x in b]
            rows.append((day, s, bt, bp, st, sp, cache[key]))
    cache_p.parent.mkdir(exist_ok=True)
    cache_p.write_bytes(pickle.dumps(cache))

    real = [(sp / bp - 1) * 100 for _, _, _, bp, _, sp, _ in rows]
    print(f"{len(rows)} round trips over {len({r[0] for r in rows})} days; "
          f"real mean {sum(real) / len(real):+.3f}%  total {sum(real):+.2f}%  "
          f"winners {sum(1 for x in real if x > 0)}/{len(real)}")
    print(f"{'rule':>22s}  {'fired':>5s}  {'cut winners':>11s}  {'mean':>8s}  {'total':>8s}  {'vs real':>8s}")
    for n in (3, 5, 7, 10):
        for x in (0.1, 0.2, 0.3):
            new, fired, cut_win = [], 0, 0
            for (day, s, bt, bp, st, sp, bars), r in zip(rows, real):
                # bars that start after the entry minute; minute k closes at start+60
                after = [b for b in bars if b[0] >= bt - 30]
                if len(after) < n:
                    new.append(r)
                    continue
                best = max(b[1] for b in after[:n])
                t_n = after[n - 1][0] + 60
                if (best / bp - 1) * 100 < x and st > t_n:
                    fired += 1
                    cut_win += r > 0
                    new.append((after[n - 1][2] / bp - 1) * 100 - a.cost)
                else:
                    new.append(r)
            m = sum(new) / len(new)
            print(f"  not +{x:.1f}% by {n:2d} min   {fired:5d}  {cut_win:11d}  {m:+.3f}%  "
                  f"{sum(new):+7.2f}%  {sum(new) - sum(real):+7.2f}%")


if __name__ == "__main__":
    main()
