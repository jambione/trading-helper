#!/usr/bin/env python3
"""resting_entry_study.py — pre-registered in docs/studies/resting_entry_prereg.json (f25755c).

For each desk entry (>= $10, 9/16-10/2) and 2 same-name random entries: market baseline (buy the SIP ask, sell the
SIP bid after the desk trade's own hold) vs a resting buy limit at the bid or the mid (rounded down) that is FILLED
only when round-lot SIP trades strictly below it total our size within W seconds, else skipped (P&L 0). A filled
resting entry sells at the SIP bid after the same hold, measured from its fill. All prices from the SIP tape.

PASS (PRIMARY = bid, W 30 s, desk signals): per-signal net minus market baseline >= +3 bp, day-clustered t >= 2,
n >= 300; and per-signal resting net > 0 in both date halves (9/16-9/25, 9/29-10/2).

USAGE (mini): .venv/bin/python tools/studies/resting_entry_study.py
"""
from __future__ import annotations

import json
import math
import os
import random
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import exec_report as er  # noqa: E402
import tape_check as tc  # noqa: E402

LO, HI, SPLIT = "2026-09-16", "2026-10-02", "2026-09-29"
WINDOWS = (10, 30, 60, 120)
LEVELS = ("bid", "mid")
CACHE = os.path.join(ROOT, "ai_reports", "resting_entry_cache.json")


def signals():
    seen, out = set(), []
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not (r.get("entry_time") and r.get("exit_time") and r.get("entry_price")) or (r.get("hold_days") or 0) > 0:
            continue
        if float(r["entry_price"]) < 10:
            continue
        et = r.get("entry_test") or {}
        t = float(et.get("t_decide") or r["entry_time"])
        day = bars.day_of(t)
        k = (r["symbol"], round(float(r["entry_time"]), 1))
        if LO <= day <= HI and k not in seen:
            seen.add(k)
            out.append({"sym": r["symbol"], "day": day, "t": t,
                        "hold": max(1.0, float(r["exit_time"]) - float(r["entry_time"]))})
    return sorted(out, key=lambda x: x["t"])


def main():
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    cl = bars.client()
    rng = random.Random(13)

    def quote(sym, t):
        k = f"Q|{sym}|{t:.1f}"
        if k not in cache:
            q = er.nbbo_at(cl, sym, datetime.fromtimestamp(t, timezone.utc))
            time.sleep(0.3)   # the live engine shares these data keys
            if not q:
                return None
            cache[k] = list(q)
        return cache[k]

    def prints(sym, t0, t1):
        k = f"P2|{sym}|{t0:.1f}|{t1:.1f}"
        if k not in cache:
            try:
                cache[k] = tc.regular_prints(cl, sym, t0, t1)
            except Exception as e:  # noqa: BLE001
                print(f"  prints fail {sym}: {str(e)[:60]}", file=sys.stderr)
                time.sleep(2.0)
                return None
            time.sleep(0.3)
        return cache[k]

    rows = []
    sig = signals()
    print(f"desk signals {len(sig)} ({LO}..{HI})", flush=True)
    for n, s in enumerate(sig):
        dd = datetime.strptime(s["day"], "%Y-%m-%d").replace(tzinfo=bars.ET)
        t945, t1530 = dd.replace(hour=9, minute=45).timestamp(), dd.replace(hour=15, minute=30).timestamp()
        close = dd.replace(hour=15, minute=55).timestamp()
        jobs = [("desk", s["t"])] + [("rand", rng.uniform(t945, t1530)) for _ in range(2)]
        for src, t in jobs:
            q0 = quote(s["sym"], t)
            if not q0 or q0[1] <= q0[0] or q0[0] <= 0:
                continue
            bid, ask = q0
            qty = 1000.0 / ask
            hold = s["hold"]
            # market baseline
            qx = quote(s["sym"], min(t + hold, close))
            if not qx:
                continue
            base = (qx[0] / ask - 1) * 1e4
            row = {"src": src, "day": s["day"], "sym": s["sym"], "base": base,
                   "spr": (ask - bid) / ((ask + bid) / 2) * 1e4}
            pr = prints(s["sym"], t, t + max(WINDOWS))
            if pr is None:
                continue
            for lev in LEVELS:
                lim = bid if lev == "bid" else math.floor((bid + ask) / 2 * 100 + 1e-9) / 100
                cum, tf = 0.0, None
                for pt, px, sz in pr:
                    if px < lim - 1e-9:
                        cum += sz
                        if cum >= qty:
                            tf = pt
                            break
                exit_q = quote(s["sym"], min(tf + hold, close)) if tf is not None else None
                for W in WINDOWS:
                    key = f"{lev}{W}"
                    if tf is not None and tf - t <= W and exit_q:
                        row[key] = (exit_q[0] / lim - 1) * 1e4
                        row[key + "_f"] = True
                    else:
                        row[key] = 0.0
                        row[key + "_f"] = False
            rows.append(row)
        if n % 20 == 0:
            json.dump(cache, open(CACHE, "w"))
            print(f"  {n}/{len(sig)}", flush=True)
    json.dump(cache, open(CACHE, "w"))
    json.dump(rows, open(os.path.join(ROOT, "ai_reports", "resting_entry_rows.json"), "w"))

    def dct(xs, f):
        by = defaultdict(list)
        for x in xs:
            by[x["day"]].append(f(x))
        dm = [statistics.mean(v) for v in by.values()]
        t = (statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm)))
             if len(dm) > 2 and statistics.stdev(dm) > 0 else float("nan"))
        return statistics.mean(f(x) for x in xs), t, len(xs)

    verdict = True
    for src in ("desk", "rand"):
        xs = [r for r in rows if r["src"] == src]
        if not xs:
            continue
        print(f"\n== {src} entries: {len(xs)} signals, median spread {statistics.median(r['spr'] for r in xs):.1f} bp; "
              f"market baseline {dct(xs, lambda r: r['base'])[0]:+.1f} bp/signal")
        print(f"  {'arm':<8}{'fill%':>7}{'net/signal':>12}{'vs market (t)':>18}{'net/fill':>10}"
              f"{'missed: base':>14}{'  halves net/signal':>22}")
        for lev in LEVELS:
            for W in WINDOWS:
                k = f"{lev}{W}"
                fills = [r for r in xs if r[k + "_f"]]
                miss = [r for r in xs if not r[k + "_f"]]
                m, _, _ = dct(xs, lambda r: r[k])
                d, dt, nn = dct(xs, lambda r: r[k] - r["base"])
                h1 = [r for r in xs if r["day"] < SPLIT]
                h2 = [r for r in xs if r["day"] >= SPLIT]
                m1 = statistics.mean(r[k] for r in h1) if h1 else float("nan")
                m2 = statistics.mean(r[k] for r in h2) if h2 else float("nan")
                print(f"  {k:<8}{100 * len(fills) / len(xs):>6.0f}%{m:>+12.1f}{d:>+10.1f} ({dt:+.2f})"
                      f"{(statistics.mean(r[k] for r in fills) if fills else float('nan')):>+10.1f}"
                      f"{(statistics.mean(r['base'] for r in miss) if miss else float('nan')):>+14.1f}"
                      f"{m1:>+11.1f}{m2:>+11.1f}")
                if src == "desk" and k == "bid30":
                    verdict = d >= 3 and dt >= 2 and nn >= 300 and m1 > 0 and m2 > 0
    print(f"\nPRE-REGISTERED VERDICT (desk, bid limit, 30 s): {'PASS' if verdict else 'FAIL'}")


if __name__ == "__main__":
    main()
