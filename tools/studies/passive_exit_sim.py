#!/usr/bin/env python3
"""passive_exit_sim.py — what would resting the desk's sells as limits have saved, before building a live arm?

For every filled day-trade SELL in ai_reports/fills/DAY.jsonl (market sells today):
  actual     the real fill vs the SIP mid at submit (exec_report's yardstick), bp
  arm X/R    rest a sell limit at X for R seconds from the submit, X in {mid (rounded UP to the cent), ask};
             it FILLS if a SIP trade prints strictly ABOVE the limit within R s (strictly, so queue
             position is not assumed; prints AT the limit are counted separately as 'touch');
             otherwise cross at the SIP bid R s later (the cost of waiting while price falls)
  saving     arm fill - actual fill, bp of the submit mid (+ = the arm earned more)

The sell is tagged with the outcome's close_reason (matched by symbol and exit_time within 15 s), so stop
exits — which must stay market orders — are reported apart. t is clustered by day.

USAGE (mini, after hours):  .venv/bin/python tools/studies/passive_exit_sim.py 2026-09-29 2026-10-02
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

RESTS = (5, 10, 30)
CACHE = os.path.join(ROOT, "ai_reports", "passive_exit_cache.json")


def trades_in(cl, sym, t0, secs):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest
    q = cl.get_stock_trades(StockTradesRequest(symbol_or_symbols=sym, start=t0,
                                               end=t0 + timedelta(seconds=secs), feed=DataFeed.SIP, limit=10000))
    return [(r.timestamp.timestamp(), float(r.price)) for r in (q.data.get(sym) or [])]


def reasons(lo, hi):
    out = defaultdict(list)
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if r.get("exit_time") and r.get("symbol"):
            d = bars.day_of(float(r["exit_time"]))
            if lo <= d <= hi:
                out[r["symbol"]].append((float(r["exit_time"]), str(r.get("close_reason") or "?")))
    return out


def kind(reason: str) -> str:
    r = reason.lower()
    if "stop" in r and "trail" not in r:
        return "stop"
    if "trail" in r or "ratchet" in r:
        return "trail"
    return "other"


def main():
    lo, hi = sys.argv[1], sys.argv[2]
    days = sorted(f[:-6] for f in os.listdir(os.path.join(ROOT, "ai_reports", "fills"))
                  if f.endswith(".jsonl") and lo <= f[:-6] <= hi)
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    cl = bars.client()
    why = reasons(lo, hi)
    rows = []
    for day in days:
        for o in er.orders(day):
            if o["side"] != "sell" or not o["t_sub"]:
                continue
            k = o["oid"]
            if k not in cache:
                try:
                    q0 = er.nbbo_at(cl, o["sym"], o["t_sub"])
                    pr = trades_in(cl, o["sym"], o["t_sub"], max(RESTS))
                    later = {R: er.nbbo_at(cl, o["sym"], o["t_sub"] + timedelta(seconds=R)) for R in RESTS}
                except Exception as e:  # noqa: BLE001
                    print(f"  skip {o['sym']}: {str(e)[:60]}", file=sys.stderr)
                    continue
                cache[k] = {"q0": list(q0) if q0 else None, "prints": pr,
                            "later": {str(R): (list(v) if v else None) for R, v in later.items()}}
                time.sleep(0.3)   # the live engine shares these data keys
            c = cache[k]
            if not c["q0"]:
                continue
            bid, ask = c["q0"]
            mid = (bid + ask) / 2
            if mid <= 0 or ask <= bid:
                continue
            ts = o["t_sub"].timestamp()
            m = [rs for te, rs in why.get(o["sym"], []) if abs(te - ts) <= 15]
            row = {"day": day, "sym": o["sym"], "kind": kind(m[0]) if m else "unmatched",
                   "spr": (ask - bid) / mid * 1e4, "actual": (o["px"] / mid - 1) * 1e4}
            for name, lim in (("mid", math.ceil(mid * 100 - 1e-9) / 100), ("ask", ask)):
                for R in RESTS:
                    hit = [p for t, p in c["prints"] if t - ts <= R and p > lim]
                    touch = [p for t, p in c["prints"] if t - ts <= R and p >= lim]
                    lb = c["later"].get(str(R))
                    fill = lim if hit else (lb[0] if lb else None)
                    if fill is None:
                        continue
                    row[f"{name}{R}"] = (fill / mid - 1) * 1e4 - row["actual"]
                    row[f"{name}{R}_fill"] = bool(hit)
                    row[f"{name}{R}_touch"] = bool(touch)
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

    print(f"PASSIVE EXIT SIM {lo}..{hi}: {len(rows)} sells (bp of SIP mid at submit; saving + = arm better)\n")
    for grp in ("ALL non-stop", "trail", "other", "stop", "unmatched"):
        xs = ([r for r in rows if r["kind"] != "stop"] if grp == "ALL non-stop"
              else [r for r in rows if r["kind"] == grp])
        if not xs:
            continue
        a = statistics.mean(r["actual"] for r in xs)
        print(f"  {grp:<13} n {len(xs):>4}  actual {a:+.1f} bp vs mid  med spread {statistics.median(r['spr'] for r in xs):.1f} bp")
        for name in ("mid", "ask"):
            for R in RESTS:
                k = f"{name}{R}"
                m_, t_, n_ = dct(xs, k)
                if m_ is None:
                    continue
                fr = sum(1 for r in xs if r.get(f"{k}_fill")) / max(1, n_)
                tr = sum(1 for r in xs if r.get(f"{k}_touch")) / max(1, n_)
                print(f"      limit @{name:<3} rest {R:>2}s: saving {m_:+6.1f} bp (t {t_:+.2f}, n {n_})  "
                      f"filled {fr:.0%}  touched {tr:.0%}" if t_ is not None else
                      f"      limit @{name:<3} rest {R:>2}s: saving {m_:+6.1f} bp (n {n_})  filled {fr:.0%}")


if __name__ == "__main__":
    main()
