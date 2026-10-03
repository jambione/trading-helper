#!/usr/bin/env python3
"""spread_wait_sim.py — would waiting a few seconds for the spread to tighten make the desk's market buys cheaper?

For every filled day-trade BUY in ai_reports/fills/DAY.jsonl, read the SIP quotes from the submit to W seconds
later. Rule: buy at the first quote whose spread is <= TIGHT x the spread at submit (or one cent), else buy at the
ask after W seconds. Unlike the passive arms this always crosses: it only picks a cheaper moment.

  saving  (ask at submit - ask actually paid by the rule) / mid at submit, bp (+ = cheaper). It includes the
          price drifting during the wait, so a rising price shows up as a negative saving.
  oracle  the lowest ask in the window (the most any waiting rule could save; not achievable)

USAGE (mini, after hours):  .venv/bin/python tools/studies/spread_wait_sim.py 2026-09-29 2026-10-02
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import timedelta

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import exec_report as er  # noqa: E402

WAITS = (2, 5, 10)
TIGHTS = (0.5, 0.75)
CACHE = os.path.join(ROOT, "ai_reports", "spread_wait_cache.json")


def quotes(cl, sym, t0, secs):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    q = cl.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=sym, start=t0, end=t0 + timedelta(seconds=secs),
                                               feed=DataFeed.SIP, limit=10000))
    return [(r.timestamp.timestamp(), float(r.bid_price), float(r.ask_price)) for r in (q.data.get(sym) or [])
            if r.bid_price and r.ask_price and r.ask_price >= r.bid_price]


def main():
    lo, hi = sys.argv[1], sys.argv[2]
    days = sorted(f[:-6] for f in os.listdir(os.path.join(ROOT, "ai_reports", "fills"))
                  if f.endswith(".jsonl") and lo <= f[:-6] <= hi)
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    cl = bars.client()
    rows = []
    for day in days:
        for o in er.orders(day):
            if o["side"] != "buy" or not o["t_sub"] or o["px"] < 10:
                continue
            k = o["oid"]
            if k not in cache:
                try:
                    q0 = er.nbbo_at(cl, o["sym"], o["t_sub"])
                    qs = quotes(cl, o["sym"], o["t_sub"], max(WAITS))
                except Exception as e:  # noqa: BLE001
                    print(f"  skip {o['sym']}: {str(e)[:60]}", file=sys.stderr)
                    continue
                cache[k] = {"q0": list(q0) if q0 else None, "qs": qs}
                time.sleep(0.3)   # the live engine shares these data keys
            c = cache[k]
            if not c["q0"] or not c["qs"]:
                continue
            b0, a0 = c["q0"]
            m0, s0 = (b0 + a0) / 2, a0 - b0
            if m0 <= 0:
                continue
            ts = o["t_sub"].timestamp()
            row = {"day": day, "sym": o["sym"], "spr": s0 / m0 * 1e4}
            for W in WAITS:
                win = [q for q in c["qs"] if q[0] - ts <= W]
                if not win:
                    continue
                row[f"oracle{W}"] = (a0 - min(q[2] for q in win)) / m0 * 1e4
                for T in TIGHTS:
                    hit = [q for q in win if (q[2] - q[1]) <= max(T * s0, 0.01 + 1e-9)]
                    paid = hit[0][2] if hit else win[-1][2]
                    row[f"w{W}_t{T}"] = (a0 - paid) / m0 * 1e4
                    row[f"w{W}_t{T}_hit"] = bool(hit)
            rows.append(row)
        json.dump(cache, open(CACHE, "w"))

    def dct(xs, key):
        by = defaultdict(list)
        for x in xs:
            if key in x:
                by[x["day"]].append(x[key])
        allv = [v for vs in by.values() for v in vs]
        dm = [statistics.mean(v) for v in by.values() if v]
        t = (statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm)))
             if len(dm) > 2 and statistics.stdev(dm) > 0 else None)
        return (statistics.mean(allv) if allv else None), t, len(allv)

    print(f"SPREAD-WAIT SIM {lo}..{hi}: {len(rows)} market buys >= $10 (saving bp of mid; + = cheaper)\n")
    for grp, xs in (("ALL", rows), ("spread at submit > 5 bp", [r for r in rows if r["spr"] > 5]),
                    ("spread at submit <= 5 bp", [r for r in rows if r["spr"] <= 5])):
        if not xs:
            continue
        print(f"  {grp}: n {len(xs)}, median spread {statistics.median(r['spr'] for r in xs):.1f} bp")
        for W in WAITS:
            o_, _, _ = dct(xs, f"oracle{W}")
            parts = []
            for T in TIGHTS:
                m_, t_, n_ = dct(xs, f"w{W}_t{T}")
                hr = sum(1 for r in xs if r.get(f"w{W}_t{T}_hit")) / max(1, n_)
                parts.append(f"tighten to {T:g}x: {m_:+5.1f} bp (t {t_:+.2f}) hit {hr:.0%}" if t_ is not None
                             else f"tighten to {T:g}x: {m_:+5.1f} bp hit {hr:.0%}")
            print(f"      wait <= {W:>2}s  oracle {o_:+5.1f}  |  " + "  |  ".join(parts))


if __name__ == "__main__":
    main()
