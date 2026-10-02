#!/usr/bin/env python3
"""momentum_fair.py — round-5 T1's 12-1 momentum (top 5/10 of ~100 mega-caps, monthly) on a point-in-time
universe vs today's survivors. Pre-registered in docs/studies/momentum_fair_prereg.json.

Runs on the mini: .venv/bin/python tools/studies/momentum_fair.py   (needs /tmp/lh/lh_core.py + panel.npz)
"""
import sys

import numpy as np

sys.path.insert(0, "/tmp/lh")
import lh_core as C  # noqa: E402

COST = 10e-4
TOP_U = 100


def tstat(a):
    a = np.asarray(a, float)
    return a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if len(a) > 2 else float("nan")


def main():
    P = C.load()
    d = [str(x)[:10] for x in P["dates"]]
    cf, adv, elig = P["cf"], P["adv20"], P["elig"]
    active = np.array([str(s) == "active" for s in P["status"]])
    spy = P["spy_c"]
    me = [i for i in range(len(d) - 1) if d[i][:7] != d[i + 1][:7]] + [len(d) - 1]
    me = [t for t in me if t >= 252]
    res = {}
    for uname, extra in (("PIT100", np.ones_like(active)), ("SURV100", active)):
        for k in (5, 10):
            rows = []
            for t, t2 in zip(me, me[1:]):
                ok = elig[t] & extra & np.isfinite(adv[t]) & np.isfinite(cf[t - 252]) & np.isfinite(cf[t - 21]) & np.isfinite(cf[t])
                idx = np.where(ok)[0]
                if len(idx) < TOP_U:
                    continue
                uni = idx[np.argsort(-adv[t, idx])][:TOP_U]
                mom = cf[t - 21, uni] / cf[t - 252, uni] - 1
                pick = uni[np.argsort(-mom)][:k]
                r = cf[t2] / cf[t] - 1
                rp = np.nanmean(r[pick]) - COST
                ru = np.nanmean(r[uni])
                rows.append((d[t2][:7], rp, ru, spy[t2] / spy[t] - 1))
            res[(uname, k)] = rows
    print(f"months {len(me) - 1}; universe = top {TOP_U} by 20d $ADV; momentum = close t-21 / close t-252; net of {COST * 1e4:.0f} bp/month\n")
    for (u, k), rows in res.items():
        for name, lo, hi in (("IS 2017-21", "2017", "2021-12"), ("OOS 2022-26", "2022", "2026-12"), ("ALL", "0", "9")):
            x = [r for r in rows if lo <= r[0] <= hi]
            ex_u = [(r[1] - r[2]) * 1e4 for r in x]; ex_s = [(r[1] - r[3]) * 1e4 for r in x]
            ann = (np.prod([1 + r[1] for r in x]) ** (12 / len(x)) - 1) * 100
            annu = (np.prod([1 + r[2] for r in x]) ** (12 / len(x)) - 1) * 100
            print(f"{u:7s} top{k:<2d} {name:11s} n={len(x):3d}  vs own universe {np.mean(ex_u):+6.0f} bp/mo (t {tstat(ex_u):+.2f})"
                  f"  vs SPY {np.mean(ex_s):+6.0f} (t {tstat(ex_s):+.2f})  CAGR {ann:+5.1f}% vs universe {annu:+5.1f}%")
        print()
    for k in (5, 10):
        a = {r[0]: r[1] for r in res[("SURV100", k)]}; b = {r[0]: r[1] for r in res[("PIT100", k)]}
        gap = [(a[m] - b[m]) * 1e4 for m in a if m in b]
        print(f"survivorship lift (SURV100 - PIT100), top{k}: {np.mean(gap):+.0f} bp/mo (t {tstat(gap):+.2f}), n={len(gap)}")


if __name__ == "__main__":
    main()
