#!/usr/bin/env python3
"""20 vs 30 vs 40 names in the overnight book: risk, and what each needs in whole shares. For the 10/9 review.

The variants study (OVERNIGHT_VARIANTS_2026-10-02) found N30/N40 no better on return (OOS −0.7 / −3.3 bp,
t −0.4 / −1.8) and smaller worst nights. This measures the risk side, fixed before running (committed with this script):

  per book, IS / OOS / ALL:  gross and net (−4 bp) bp/night, nightly sd, annualized Sharpe (net, √252), % green,
                             worst night, 1st-percentile night, CVaR 1% (mean of the worst 1% of nights),
                             max drawdown and longest drawdown (sessions) of the compounded net equity curve,
                             worst calendar month, beta to SPY's overnight return
  paired diff vs N20:        bp/night and t over all nights
  whole shares:              at $1k / $2.5k / $5k / $10k / $25k, names held, cash deployed, and nightly correlation with
                             that book's own equal-weight version (raw close sizing, leftover cash to the most underweight)

All books use the live rules otherwise (12-1, ≥ $5, ADV ≥ $50M, equal weight, −1% intraday filter).
Run on the mini: .venv/bin/python tools/studies/overnight_book_size.py
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

LH = os.path.expanduser("~/lh_cache")
sys.path.insert(0, LH)
import lh_core as C  # noqa: E402
import lh_strat as S  # noqa: E402

COST = 4e-4
IS_END = pd.Timestamp("2021-12-31")
BOOKS = (20, 30, 40)
SIZES = (1_000, 2_500, 5_000, 10_000, 25_000)


def size_book(px: np.ndarray, cash: float) -> np.ndarray:
    n = len(px)
    target = cash / n
    sh = np.floor(target / px).astype(int)
    left = cash - float((sh * px).sum())
    while True:
        ok = np.where(px <= left)[0]
        if len(ok) == 0:
            break
        j = ok[np.argmin((sh * px)[ok])]
        sh[j] += 1
        left -= px[j]
    return sh


def main():
    P = S.returns(C.load())
    cf, ON, ID, rc, adv, dates = P["cf"], P["ON"], P["ID"], P["rc"], P["adv20"], P["dates"]
    T = cf.shape[0]
    mom = np.full_like(cf, np.nan)
    with np.errstate(all="ignore"):
        mom[252:] = cf[231:T - 21] / cf[:T - 252] - 1
    spy_on = P["spy_on"]
    nights = list(range(260, T - 1))
    d = dates[np.array(nights)]
    is_m, oos_m = np.array(d <= IS_END), np.array(d > IS_END)

    ret = {n: np.full(len(nights), np.nan) for n in BOOKS}
    held = {n: np.zeros(len(nights)) for n in BOOKS}
    shares = {(n, s): [] for n in BOOKS for s in SIZES}       # (names held, deployed, whole-share ret, eq ret)
    for k, t in enumerate(nights):
        ok = (rc[t] >= 5) & (adv[t] >= 50e6) & np.isfinite(mom[t]) & np.isfinite(cf[t])
        idx = np.where(ok)[0]
        if len(idx) < 30:
            continue
        ranked = idx[np.argsort(-mom[t, idx])]
        for n in BOOKS:
            pick = ranked[:n]
            pick = pick[np.nan_to_num(ID[t, pick], nan=0.0) >= -0.01]
            on = ON[t + 1, pick]
            g = np.isfinite(on) & np.isfinite(rc[t, pick]) & (rc[t, pick] > 0)
            pick, on = pick[g], on[g]
            if len(pick) == 0:
                ret[n][k] = 0.0
                continue
            ret[n][k] = float(on.mean())
            held[n][k] = len(pick)
            px = rc[t, pick]
            for s in SIZES:
                sh = size_book(px, float(s))
                val = sh * px
                r_ws = float((val * on).sum() / s)
                shares[(n, s)].append(((sh > 0).sum(), val.sum() / s, r_ws, float(on.mean())))
        if k % 250 == 0:
            print(f"  {d[k].date()}", file=sys.stderr, flush=True)

    def stats(x, mask, dd):
        x = x[mask]
        x = x[np.isfinite(x)]
        net = x - COST * (x != 0)
        eq = np.cumprod(1 + net)
        peak = np.maximum.accumulate(eq)
        draw = eq / peak - 1
        longest, run = 0, 0
        for v in draw:
            run = run + 1 if v < 0 else 0
            longest = max(longest, run)
        q = np.sort(x)
        k1 = max(1, int(len(q) * 0.01))
        months = pd.Series(net, index=dd[mask][np.isfinite(ret[BOOKS[0]][mask])][:len(net)]).groupby(
            lambda z: (z.year, z.month)).apply(lambda s: np.prod(1 + s) - 1)
        return dict(gross=x.mean() * 1e4, net=net.mean() * 1e4, sd=x.std(ddof=1) * 1e4,
                    sharpe=net.mean() / net.std(ddof=1) * math.sqrt(252), green=(x > 0).mean(),
                    worst=q[0] * 100, p1=np.percentile(x, 1) * 100, cvar=q[:k1].mean() * 100,
                    mdd=draw.min() * 100, longest=longest, worst_m=months.min() * 100)

    print(f"\nnights IS {is_m.sum()}, OOS {oos_m.sum()}; names held (OOS mean): "
          + ", ".join(f"N{n} {held[n][oos_m].mean():.1f}" for n in BOOKS))
    for label, mask in (("OOS 2022-26", oos_m), ("IS 2017-21", is_m), ("ALL", is_m | oos_m)):
        print(f"\n{label}")
        print(f"{'book':5} {'gross':>6} {'net':>6} {'sd':>5} {'Sharpe':>7} {'green':>6} {'worst%':>7} {'p1%':>6} "
              f"{'CVaR1%':>7} {'maxDD%':>7} {'longest':>8} {'worst mo%':>10}")
        for n in BOOKS:
            s = stats(ret[n], mask, d)
            print(f"N{n:<4} {s['gross']:>+6.1f} {s['net']:>+6.1f} {s['sd']:>5.0f} {s['sharpe']:>7.2f} {s['green']:>6.0%} "
                  f"{s['worst']:>+7.1f} {s['p1']:>+6.1f} {s['cvar']:>+7.1f} {s['mdd']:>+7.1f} {s['longest']:>8} {s['worst_m']:>+10.1f}")
    print("\npaired diff vs N20, all nights:")
    for n in (30, 40):
        x = (ret[n] - ret[20]) * 1e4
        x = x[np.isfinite(x)]
        print(f"  N{n}: {x.mean():+.1f} bp/night (t {x.mean() / (x.std(ddof=1) / math.sqrt(len(x))):+.1f})")
        b = np.isfinite(ret[20]) & np.isfinite(ret[n])
        beta = np.polyfit(spy_on[np.array(nights) + 1][b], ret[n][b], 1)[0]
        print(f"        beta to SPY overnight N{n} {beta:.2f}", end="")
    b = np.isfinite(ret[20])
    print(f"; N20 {np.polyfit(spy_on[np.array(nights) + 1][b], ret[20][b], 1)[0]:.2f}")
    print("\nwhole shares (all nights; corr = whole-share book vs its own equal-weight book):")
    print(f"{'account':>9} " + " ".join(f"{'N' + str(n) + ' names/dep/corr':>22}" for n in BOOKS))
    for s in SIZES:
        cells = []
        for n in BOOKS:
            a = np.array(shares[(n, s)])
            corr = np.corrcoef(a[:, 2], a[:, 3])[0, 1]
            cells.append(f"{a[:, 0].mean():>6.1f} / {a[:, 1].mean():>4.0%} / {corr:>4.2f}")
        print(f"{'$' + format(s, ','):>9} " + " ".join(f"{c:>22}" for c in cells))


if __name__ == "__main__":
    main()
