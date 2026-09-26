#!/usr/bin/env python3
"""edge5_t3.py — Round-5 T3 calendar effects on SPY/QQQ, last 5 years (pre-registered in edge5b_prereg.json).
Daily SIP adjusted bars (edge5_fetch daily_long). Effects: turn of month (enter close before last trading day, exit
close of +3 = 4 sessions), pre-holiday session, FOMC statement day (hardcoded public dates), day of week, overnight
(close->open) vs intraday (open->close). Cost per trade = modeled round-trip spread + 2 bp (overnight/open leg).
Baseline = all sessions (random days) same instrument. Split by time 60/20/20."""
import math
import os
import pickle
import sys
from datetime import date

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import edge_common as ec  # noqa: E402

FOMC = """2021-09-22 2021-11-03 2021-12-15 2022-01-26 2022-03-16 2022-05-04 2022-06-15 2022-07-27 2022-09-21 2022-11-02
2022-12-14 2023-02-01 2023-03-22 2023-05-03 2023-06-14 2023-07-26 2023-09-20 2023-11-01 2023-12-13 2024-01-31 2024-03-20
2024-05-01 2024-06-12 2024-07-31 2024-09-18 2024-11-07 2024-12-18 2025-01-29 2025-03-19 2025-05-07 2025-06-18 2025-07-30
2025-09-17 2025-10-29 2025-12-10 2026-01-28 2026-03-18 2026-04-29 2026-06-17 2026-07-29 2026-09-16""".split()


def welch(a, b):
    a, b = np.asarray(a), np.asarray(b)
    if len(a) < 3:
        return float("nan")
    return (a.mean() - b.mean()) / math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))


def one(a):
    a = np.asarray(a)
    return a.mean() / (a.std(ddof=1) / math.sqrt(len(a))) if len(a) > 2 else float("nan")


def main():
    D = pickle.load(open(os.path.join(ROOT, "ai_reports", "edge5", "daily_long.pkl"), "rb"))
    sm = ec.SpreadModel({"SPY", "QQQ"})
    for sym in ("SPY", "QQQ"):
        rows = sorted(D[sym])
        ds = [str(r[0])[:10] for r in rows]
        o = np.array([r[1] for r in rows], float); c = np.array([r[2] for r in rows], float)
        start = next(i for i, x in enumerate(ds) if x >= "2021-09-27")
        cc = np.r_[np.nan, np.diff(c) / c[:-1]] * 1e4
        on = np.r_[np.nan, o[1:] / c[:-1] - 1] * 1e4
        intra = (c / o - 1) * 1e4
        cost = sm.sym.get(sym, 1.0) + 2.0
        idx = list(range(start, len(ds)))
        n = len(idx)
        part = {i: ("tune" if k < int(n * .6) else "validate" if k < int(n * .8) else "holdout") for k, i in enumerate(idx)}
        dd = [date.fromisoformat(x) for x in ds]
        # flags
        last_of_month = {i for i in range(len(ds) - 1) if ds[i][:7] != ds[i + 1][:7]}
        pre_hol = set()
        for i in range(len(ds) - 1):
            gap = (dd[i + 1] - dd[i]).days
            if (dd[i].weekday() == 4 and gap > 3) or (dd[i].weekday() < 4 and gap > 1):
                pre_hol.add(i)
        fomc = {i for i, x in enumerate(ds) if x in FOMC}
        print(f"\n=== {sym}  {ds[start]}..{ds[-1]}  sessions {n}  cost/trade {cost:.1f} bp (spread {cost-2:.1f} + 2)")
        print(f"  split: tune ..{ds[idx[int(n*.6)-1]]}, validate ..{ds[idx[int(n*.8)-1]]}, holdout ..{ds[-1]}")
        allcc = {p: [cc[i] for i in idx if part[i] == p] for p in ("tune", "validate", "holdout")}
        print(f"  baseline all sessions mean c2c: " + " | ".join(f"{p} {np.mean(v):+.1f} bp" for p, v in allcc.items()))
        def rep(name, sel, ret_fn, sessions=1):
            cells = []
            for p in ("tune", "validate", "holdout"):
                g = [ret_fn(i) for i in sel if i in part and part[i] == p]
                g = [x for x in g if np.isfinite(x)]
                if len(g) < 3:
                    cells.append(f"{p} n{len(g)}"); continue
                base = np.mean(allcc[p]) * sessions
                net = np.array(g) - cost
                cells.append(f"{p} n{len(g):3d} gross {np.mean(g):+6.1f} net {net.mean():+6.1f} (t {one(net):+.2f}) "
                             f"excess-vs-random {np.mean(g)-base:+6.1f} (t {welch(g, [base]*1 and [x*sessions for x in allcc[p]]) if sessions==1 else one(np.array(g)-base):+.2f})")
            print(f"  {name:22} " + " | ".join(cells))
        # TOM: enter close of day before last trading day (i-1), exit close i+3
        rep("turn-of-month (4 ses)", [i for i in last_of_month if i + 3 < len(ds)], lambda i: (c[i + 3] / c[i - 1] - 1) * 1e4, 4)
        rep("pre-holiday session", pre_hol, lambda i: cc[i])
        rep("FOMC day", fomc, lambda i: cc[i])
        rep("FOMC day intraday", fomc, lambda i: intra[i])
        for wd, nm in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri")):
            rep(f"{nm} c2c", [i for i in idx if dd[i].weekday() == wd], lambda i: cc[i])
        # overnight vs intraday (every session; cost applies per leg)
        for p in ("tune", "validate", "holdout"):
            a = np.array([on[i] for i in idx if part[i] == p]); b = np.array([intra[i] for i in idx if part[i] == p])
            print(f"  overnight vs intraday {p:8}: overnight gross {a.mean():+.2f} (t {one(a):+.2f}) net {a.mean()-cost:+.2f} | "
                  f"intraday gross {b.mean():+.2f} (t {one(b):+.2f}) net {b.mean()-(cost-2):+.2f} (spread only, no open print... uses open print: {b.mean()-cost:+.2f})")
    # QQQ minus SPY on TOM
    print("\n(minus-SPY for QQQ = QQQ gross - SPY gross on the same windows; see QQQ vs SPY lines above)")


if __name__ == "__main__":
    main()
