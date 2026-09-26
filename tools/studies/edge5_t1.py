#!/usr/bin/env python3
"""edge5_t1.py — Round-5 T1 horizon ladder on daily bars (pre-registered in docs/studies/edge5b_prereg.json).
U2 108 liquid names (SURVIVORSHIP: today's list), SIP daily adjusted bars 2016-01..2026-09 (edge5_fetch daily_long).
Signals RS20 / RS60 / RS12_1 / NEAR_HIGH / PULLBACK x regime (none, SPY>SMA50, SPY>SMA200) x entry (next open, close)
x hold (1,2,5,10,20 d) x N (5, 10). Cost = symbol median modeled spread + 2 bp. Baseline = equal-weight universe
(expected value of random picks, same dates, same cost). Split by signal date: 60/20/20; holdout printed only for
promoted cells. Also writes the tune+validate-best pre-open selection for T4 variant 2 when one is promoted."""
from __future__ import annotations

import json
import os
import pickle
import sys

import numpy as np
import pandas as pd

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import edge_common as ec  # noqa: E402
import edge2_fetch as e2  # noqa: E402

HS = (1, 2, 5, 10, 20)


def tstat(x, dates):
    s = ec.stats(np.asarray(x), np.asarray(dates), boot=200)
    return s


def main():
    D = pickle.load(open(os.path.join(ROOT, "ai_reports", "edge5", "daily_long.pkl"), "rb"))
    U = sorted(e2.U2)
    mk = lambda j: pd.DataFrame({s: pd.Series({r[0]: r[j] for r in D[s]}) for s in U}).sort_index()
    O, C, H = mk(1), mk(2), mk(4)
    dates = list(C.index)
    sm = ec.SpreadModel(set(U))
    cost = pd.Series({s: sm.sym.get(s, 8.0) + 2.0 for s in U})
    spy = C["SPY"]
    sma50, sma200 = spy.rolling(50).mean(), spy.rolling(200).mean()
    r20, r60 = C / C.shift(20) - 1, C / C.shift(60) - 1
    r12_1 = C.shift(21) / C.shift(252) - 1
    hi252 = H.rolling(252, min_periods=240).max()
    near = C / hi252 - 1
    s50 = C.rolling(50).mean()
    d = C.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    rsi2 = 100 - 100 / (1 + up.ewm(alpha=0.5, adjust=False).mean() / dn.ewm(alpha=0.5, adjust=False).mean())
    down3 = (d < 0) & (d.shift(1) < 0) & (d.shift(2) < 0)
    first = 253
    sig_dates = dates[first:len(dates) - 1]
    n = len(sig_dates)
    part = {x: ("tune" if i < int(n * 0.6) else "validate" if i < int(n * 0.8) else "holdout") for i, x in enumerate(sig_dates)}
    di = {x: i for i, x in enumerate(dates)}
    fwd = {}
    for h in HS:
        fwd[("open", h)] = (C.shift(-h) / O.shift(-1) - 1) * 1e4      # enter next open, exit close t+h
        fwd[("close", h)] = (C.shift(-h) / C - 1) * 1e4               # enter close t, exit close t+h
    rows = {}
    regimes = {"none": pd.Series(True, index=C.index), "SPY>SMA50": spy > sma50, "SPY>SMA200": spy > sma200}
    def picks(sig, t, N):
        if sig in ("RS20", "RS60", "RS12_1"):
            v = {"RS20": r20, "RS60": r60, "RS12_1": r12_1}[sig].loc[t].dropna()
            v = v[v >= v.quantile(0.9)]
            return list(v.sort_values(ascending=False).index[:N])
        if sig == "NEAR_HIGH":
            v = near.loc[t].dropna(); v = v[v >= -0.02]
            return list(v.sort_values(ascending=False).index[:N])
        v = rsi2.loc[t][(C.loc[t] > s50.loc[t]) & ((rsi2.loc[t] < 10) | down3.loc[t])].dropna()
        return list(v.sort_values().index[:N])
    P = {}
    for sig in ("RS20", "RS60", "RS12_1", "NEAR_HIGH", "PULLBACK"):
        for N in (5, 10):
            P[(sig, N)] = {t: picks(sig, t, N) for t in sig_dates}
    print("picks built", flush=True)
    out = []
    for (sig, N), pk in P.items():
        for rg, rs in regimes.items():
            for ent in ("open", "close"):
                for h in HS:
                    F = fwd[(ent, h)]
                    rec = {"tune": [], "validate": [], "holdout": []}
                    for t in sig_dates:
                        if not rs.loc[t] or not pk[t]:
                            continue
                        row = F.loc[t]
                        if not np.isfinite(row["SPY"]):
                            continue
                        uni = row.dropna()
                        base = float((uni - cost[uni.index]).mean())
                        for s in pk[t]:
                            if np.isfinite(row[s]):
                                g = row[s]
                                rec[part[t]].append((di[t], g - cost[s], g - cost[s] - base, g - row["SPY"]))
                    for p, v in rec.items():
                        if len(v) < 20:
                            continue
                        A = np.array(v)
                        s1 = tstat(A[:, 1], A[:, 0]); s2 = tstat(A[:, 2], A[:, 0]); s3 = tstat(A[:, 3], A[:, 0])
                        # non-overlapping check: only signal dates spaced >= h apart
                        u = np.unique(A[:, 0]); keep = set(u[::h])
                        m = np.array([x in keep for x in A[:, 0]])
                        s4 = tstat(A[m, 2], A[m, 0]) if m.sum() > 10 else {"t": np.nan}
                        out.append({"sig": sig, "N": N, "regime": rg, "entry": ent, "h": h, "part": p, "n": len(v),
                                    "dates": s1["days"], "net": s1["net"], "t": s1["t"], "excess": s2["net"], "t_ex": s2["t"],
                                    "t_ex_nonoverlap": s4["t"], "mspy_gross": s3["net"], "t_mspy": s3["t"], "per_day": s1["net"] / h})
        print(f"  {sig} N{N} done", flush=True)
    json.dump(out, open(os.path.join(ROOT, "ai_reports", "edge5", "t1.json"), "w"))
    idx = {(c["sig"], c["N"], c["regime"], c["entry"], c["h"], c["part"]): c for c in out}
    print(f"\nT1 horizon ladder: {sig_dates[0]}..{sig_dates[-1]}; tune ..{[x for x in sig_dates if part[x]=='tune'][-1]}, "
          f"validate ..{[x for x in sig_dates if part[x]=='validate'][-1]}, holdout ..{sig_dates[-1]}")
    # combined tune+validate stats for promotion
    promos = []
    for key in {k[:5] for k in idx}:
        tu, va = idx.get(key + ("tune",)), idx.get(key + ("validate",))
        if not tu or not va:
            continue
        tv_t = (tu["t_ex"] * np.sqrt(tu["dates"]) + va["t_ex"] * np.sqrt(va["dates"])) / np.sqrt(tu["dates"] + va["dates"])
        if tu["excess"] > 0 and va["excess"] > 0 and tv_t >= 2:
            promos.append((tv_t, key))
    print(f"cells: {len(idx)//3}; promoted (excess>0 tune & validate, combined t_ex>=2): {len(promos)}")
    print(f"{'signal':10} {'N':>2} {'regime':10} {'entry':5} {'h':>2} | {'tune n':>6} {'net':>6} {'excess':>6} {'t':>5} {'t_no':>5} | "
          f"{'val n':>5} {'net':>6} {'excess':>6} {'t':>5} | {'comb t':>6} | holdout n / net / excess / t / t_nonoverlap / -SPY")
    for tv_t, key in sorted(promos, reverse=True)[:12]:
        tu, va, ho = (idx.get(key + (p,)) for p in ("tune", "validate", "holdout"))
        hs = f"{ho['n']} / {ho['net']:+.1f} / {ho['excess']:+.1f} / {ho['t_ex']:+.2f} / {ho['t_ex_nonoverlap']:+.2f} / {ho['mspy_gross']:+.1f}" if ho else "-"
        flag = "  <- holdout run (top 5)" if (tv_t, key) in sorted(promos, reverse=True)[:5] else ""
        if not flag:
            hs = "(not run)"
        print(f"{key[0]:10} {key[1]:2d} {key[2]:10} {key[3]:5} {key[4]:2d} | {tu['n']:6d} {tu['net']:+6.1f} {tu['excess']:+6.1f} {tu['t_ex']:+5.2f} {tu['t_ex_nonoverlap']:+5.2f} | "
              f"{va['n']:5d} {va['net']:+6.1f} {va['excess']:+6.1f} {va['t_ex']:+5.2f} | {tv_t:+6.2f} | {hs}{flag}")
    print("\nSummary (regime none, next-open entry, N=10): net bp/trade [excess vs random] by hold, tune | validate")
    for sig in ("RS20", "RS60", "RS12_1", "NEAR_HIGH", "PULLBACK"):
        cells = []
        for h in HS:
            a, b = idx.get((sig, 10, "none", "open", h, "tune")), idx.get((sig, 10, "none", "open", h, "validate"))
            cells.append(f"{h}d {a['net']:+.0f}[{a['excess']:+.0f}] | {b['net']:+.0f}[{b['excess']:+.0f}]" if a and b else f"{h}d -")
        print(f"  {sig:10} " + "   ".join(cells))


if __name__ == "__main__":
    main()
