#!/usr/bin/env python3
"""desk_vs_random.py — does the desk's choice of entry minute still cost anything, net of the spread?

For each closed day trade in ai_reports/outcomes.jsonl (hold_days 0) between two dates:
  desk    SIP 1m close at exit / SIP 1m close at entry - 1, using the last bar COMPLETED before each
          time (bars are stamped at their start), so neither side sees the minute it acted in
  random  the same move for N random entry minutes in the SAME name and day, held for the same
          number of seconds, entries 09:45..(15:50 - hold)
  excess  desk - mean(random) for that trade

Bar-to-bar moves carry no spread, so excess isolates selection and timing from cost. The t is
clustered by day (mean of daily means / its standard error). Also reported: the desk's own P&L
(exit / entry - 1) for scale. This reruns the Round-1A question (desk entries vs random,
STRATEGY_EDGE_ROUND1_2026-09-26) on the current gates, with a same-name control.

USAGE (mini, after hours):
  .venv/bin/python tools/studies/desk_vs_random.py 2026-09-29 2026-10-02 [--min-price 10] [--n 50]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from collections import defaultdict

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402


def move(stamps, closes, t0: float, t1: float) -> float | None:
    i, j = bars.index_at(stamps, t0 - 60), bars.index_at(stamps, t1 - 60)
    if i < 0 or j < 0 or j <= i:
        return None
    return closes[j] / closes[i] - 1


def dct(by_day: dict[str, list[float]]):
    allv = [x for v in by_day.values() for x in v]
    dm = [statistics.mean(v) for v in by_day.values() if v]
    t = (statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm)))
         if len(dm) > 2 and statistics.stdev(dm) > 0 else None)
    return (statistics.mean(allv) if allv else None), t, len(allv)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lo")
    ap.add_argument("hi")
    ap.add_argument("--min-price", type=float, default=0.0)
    ap.add_argument("--n", type=int, default=50, help="random entries per trade")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    trades = []
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not r.get("entry_time") or not r.get("exit_time") or not r.get("entry_price") or not r.get("exit_price"):
            continue
        if (r.get("hold_days") or 0) > 0 or float(r["entry_price"]) < args.min_price:
            continue
        day = bars.day_of(float(r["entry_time"]))
        if args.lo <= day <= args.hi:
            trades.append((day, r))

    rows, missing = [], 0
    for day, r in sorted(trades, key=lambda x: float(x[1]["entry_time"])):
        sym = r["symbol"]
        known = (str(sym).upper(), day, "sip") in bars._CACHE
        st, cl = bars.fetch(sym, day, "sip")
        if not known:
            time.sleep(0.35)   # the live engine shares these data keys
        if not st:
            missing += 1
            continue
        t0, t1 = float(r["entry_time"]), float(r["exit_time"])
        hold = max(60.0, t1 - t0)
        d = move(st, cl, t0, t1)
        if d is None:
            missing += 1
            continue
        from datetime import datetime
        open_t = datetime.strptime(day, "%Y-%m-%d").replace(hour=9, minute=30, tzinfo=bars.ET).timestamp()
        lo_t = open_t + 15 * 60                      # 09:45
        hi_t = open_t + 380 * 60 - hold              # 15:50 - hold
        if hi_t <= lo_t:
            missing += 1
            continue
        ctrl = [m for m in (move(st, cl, x, x + hold) for x in
                            (rng.uniform(lo_t, hi_t) for _ in range(args.n))) if m is not None]
        if not ctrl:
            missing += 1
            continue
        rows.append({"day": day, "sym": sym, "px": float(r["entry_price"]), "hold": hold,
                     "desk": d, "rand": statistics.mean(ctrl), "excess": d - statistics.mean(ctrl),
                     "pnl": float(r["exit_price"]) / float(r["entry_price"]) - 1})

    print(f"DESK vs SAME-NAME RANDOM {args.lo}..{args.hi}  trades {len(rows)} (skipped {missing})  "
          f"min price ${args.min_price:g}, {args.n} random entries each; bp, t clustered by day\n")
    print(f"  {'group':<20}{'n':>5}{'desk':>9}{'random':>9}{'excess':>9}{'t':>7}{'desk P&L':>10}{'med hold s':>11}")

    def line(label, xs):
        if not xs:
            return
        g = lambda k: dct({d: [x[k] for x in xs if x["day"] == d] for d in {x["day"] for x in xs}})
        de, rn, ex, pn = g("desk"), g("rand"), g("excess"), g("pnl")
        print(f"  {label:<20}{len(xs):>5}{de[0] * 1e4:>+9.1f}{rn[0] * 1e4:>+9.1f}{ex[0] * 1e4:>+9.1f}"
              f"{(f'{ex[1]:+.2f}' if ex[1] is not None else '—'):>7}{pn[0] * 1e4:>+10.1f}"
              f"{statistics.median(x['hold'] for x in xs):>11.0f}")

    line("ALL", rows)
    for lo, hi in ((0, 10), (10, 20), (20, 50), (50, 1e9)):
        line(f"${lo}-{hi if hi < 1e9 else '+'}", [x for x in rows if lo <= x["px"] < hi])
    for lo, hi in ((0, 120), (120, 600), (600, 1e9)):
        line(f"hold {lo}-{hi if hi < 1e9 else '+'}s", [x for x in rows if lo <= x["hold"] < hi])
    for d in sorted({x["day"] for x in rows}):
        line(d, [x for x in rows if x["day"] == d])


if __name__ == "__main__":
    main()
