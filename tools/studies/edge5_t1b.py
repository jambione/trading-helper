#!/usr/bin/env python3
"""edge5_t1b.py — overlap-free robustness for the T1 leaders + T6 rule on T1 picks + Mark file for T4 variant 2.
Non-overlapping: for hold h, rebalance every h sessions (h separate offsets); per-offset series of basket returns,
t on the series of rebalance periods (no overlap). Reports year-by-year excess and top-symbol concentration."""
import json
import os
import pickle
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

ROOT = os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import edge_common as ec  # noqa: E402
import edge2_fetch as e2  # noqa: E402

D = pickle.load(open("ai_reports/edge5/daily_long.pkl", "rb"))
U = sorted(e2.U2)
mk = lambda j: pd.DataFrame({s: pd.Series({str(r[0])[:10]: r[j] for r in D[s]}) for s in U}).sort_index()
O, C, H = mk(1), mk(2), mk(4)
dates = list(C.index)
sm = ec.SpreadModel(set(U))
cost = pd.Series({s: sm.sym.get(s, 8.0) + 2.0 for s in U})
spy = C["SPY"]
reg = {"none": pd.Series(True, index=C.index), "SPY>SMA200": spy > spy.rolling(200).mean(), "SPY>SMA50": spy > spy.rolling(50).mean()}
sig = {"RS12_1": C.shift(21) / C.shift(252) - 1, "RS60": C / C.shift(60) - 1, "RS20": C / C.shift(20) - 1}
# T6 rule: prior-close breadth >= 50% above SMA50 and SPY rv20 <= trailing 250d median
a50 = (C > C.rolling(50).mean()).where(C.rolling(50).mean().notna()).mean(axis=1)
rv = np.log(spy).diff().rolling(20).std()
t6 = (a50 >= 0.5) & (rv <= rv.rolling(250, min_periods=60).median().shift(1))
sig_dates = dates[253:-1]
n = len(sig_dates)
part = {x: ("tune" if i < int(n * .6) else "validate" if i < int(n * .8) else "holdout") for i, x in enumerate(sig_dates)}
di = {x: i for i, x in enumerate(dates)}


def top(s, t, N):
    v = sig[s].loc[t].dropna()
    v = v[v >= v.quantile(.9)]
    return list(v.sort_values(ascending=False).index[:N])


def run(s, N, rg, ent, h, extra=None, label=""):
    per = {p: [] for p in part.values()}
    symc = defaultdict(lambda: defaultdict(float)); yr = defaultdict(list)
    for off in range(h):
        for k in range(off, n, h):
            t = sig_dates[k]; i = di[t]
            if i + h >= len(dates) or not reg[rg].loc[t] or (extra is not None and not extra.loc[t]):
                continue
            px0 = O.iloc[i + 1] if ent == "open" else C.iloc[i]
            g = (C.iloc[i + h] / px0 - 1) * 1e4
            g = g.dropna()
            pk = [x for x in top(s, t, N) if x in g.index]
            if not pk:
                continue
            net = g[pk] - cost[pk]
            base = (g - cost[g.index]).mean()
            ex = net.mean() - base
            per[part[t]].append((off, net.mean(), ex, g[pk].mean() - g["SPY"]))
            yr[t[:4]].append(ex)
            for x in pk:
                symc[part[t]][x] += (net[x] - base) / len(pk)
    print(f"\n{label or f'{s} top{N} {rg} {ent}-entry {h}d'}  (non-overlapping rebalance every {h} sessions, all {h} offsets)")
    for p in ("tune", "validate", "holdout"):
        A = np.array(per[p])
        if len(A) < 5:
            print(f"  {p:8} n={len(A)}"); continue
        # t per offset (independent periods), then average t and mean across offsets
        ts = [A[A[:, 0] == o, 2].mean() / (A[A[:, 0] == o, 2].std(ddof=1) / np.sqrt((A[:, 0] == o).sum())) for o in range(h) if (A[:, 0] == o).sum() > 3]
        tot = sum(symc[p].values()) or 1
        topc = sorted(symc[p].items(), key=lambda kv: -kv[1])[:3]
        print(f"  {p:8} periods/offset ~{len(A)//h:4d}  basket net {A[:,1].mean():+7.1f} bp  excess vs random {A[:,2].mean():+7.1f}  "
              f"minus-SPY {A[:,3].mean():+7.1f}  overlap-free t(excess) {np.mean(ts):+.2f} [min {np.min(ts):+.2f}, max {np.max(ts):+.2f}]  "
              f"top contributors: " + ", ".join(f"{k} {v/tot:.0%}" for k, v in topc))
    print("  excess by year: " + "  ".join(f"{y} {np.mean(v):+.0f}" for y, v in sorted(yr.items())))


if __name__ == "__main__":
    for args in [("RS12_1", 5, "none", "close", 20), ("RS12_1", 5, "none", "open", 20), ("RS60", 5, "SPY>SMA200", "open", 20),
                 ("RS12_1", 10, "none", "open", 20), ("RS12_1", 5, "none", "open", 10), ("RS12_1", 5, "none", "open", 5),
                 ("RS12_1", 5, "none", "open", 1), ("RS20", 10, "none", "open", 20)]:
        run(*args)
    print("\nT6 rule applied to T1 leader picks (ON = breadth>=50% & SPY rv20 <= 250d median):")
    run("RS12_1", 5, "none", "open", 20, extra=t6, label="RS12_1 top5 open 20d, T6 ON only")
    run("RS12_1", 5, "none", "open", 20, extra=~t6, label="RS12_1 top5 open 20d, T6 OFF only")
    run("RS12_1", 5, "none", "open", 5, extra=t6, label="RS12_1 top5 open 5d, T6 ON only")
    # Mark file for T4 variant 2: top-5 RS12_1 as of the prior close, no regime filter
    split = json.load(open("ai_reports/edge2_split.json"))
    mf = {}
    for d in split["tune"] + split["validate"] + split["holdout"]:
        i = dates.index(str(d)[:10])
        mf[d] = top("RS12_1", dates[i - 1], 5)
    json.dump(mf, open("ai_reports/edge5/mark_rs12_1_top5.json", "w"))
    print(f"\nwrote mark file for T4 variant 2: {len(mf)} days, e.g. {list(mf.items())[-1]}")
