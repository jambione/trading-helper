#!/usr/bin/env python3
"""Two questions on the live overnight book (top-20 12-1 momentum, >= $5, ADV20 >= $50M, equal weight, the
-1% filter OFF as live since 2026-10-03), on the 10-year panel (same machinery as overnight_variants.py).

(9) SPY HEDGE: short h x SPY overnight (close -> next open, auction legs) against the book. The book's beta
    to SPY overnight is ~1.44 (OVERNIGHT_BOOK_SIZE). h is estimated on IS only (2017-2021) and applied to
    OOS unchanged; fixed h = 0.5 / 1.0 / 1.4 are shown too. SPY legs cost SPY_COST per unit of hedge.
    Does it cut the tail (worst night/month, CVaR, drawdown) and what does it cost in mean?

(12) INTRADAY LEG: the picks' open -> close return on the day AFTER the overnight hold (when the desk could
    trade them), and on the day OF the buy, vs the average eligible name on the same day. Do the overnight
    names give back during the session?

Run on the mini: .venv/bin/python tools/studies/overnight_hedge_intraday.py
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

COST = 4e-4          # book round trip at the auctions
SPY_COST = 2.26e-4   # SPY auction round trip per unit of hedge (month-end study)
IS_END = pd.Timestamp("2021-12-31")


def tail(x, d):
    """mean, t, sd, worst night, CVaR 1%, worst month (sum), max drawdown (sum of bp), in bp."""
    x = x * 1e4
    ok = np.isfinite(x)
    x, d = x[ok], d[ok]
    t = x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))
    k = max(1, int(len(x) * 0.01))
    cvar = np.sort(x)[:k].mean()
    mo = pd.Series(x, index=d).groupby(pd.Index(d).to_period("M")).sum().min()
    cum = np.cumsum(x)
    dd = (cum - np.maximum.accumulate(cum)).min()
    return x.mean(), t, x.std(ddof=1), x.min(), cvar, mo, dd


def main():
    P = S.returns(C.load())
    cf, ON, ID = P["cf"], P["ON"], P["ID"]
    rc, adv, dates = P["rc"], P["adv20"], P["dates"]
    T, N = cf.shape
    spy_on = np.full(T, np.nan)
    spy_on[1:] = P["spy_o"][1:] / P["spy_c"][:-1] - 1      # close t-1 -> open t
    m = np.full_like(cf, np.nan)
    with np.errstate(all="ignore"):
        m[252:] = cf[252 - 21:T - 21] / cf[:T - 252] - 1

    nights = np.arange(260, T - 2)
    book, spy, id_next, id_same, id_next_univ, id_same_univ = (np.full(len(nights), np.nan) for _ in range(6))
    for k, t in enumerate(nights):
        ok = (rc[t] >= 5) & (adv[t] >= 50e6) & np.isfinite(m[t]) & np.isfinite(cf[t])
        idx = np.where(ok)[0]
        if len(idx) < 30:
            continue
        pick = idx[np.argsort(-m[t, idx])][:20]
        on = ON[t + 1, pick]
        g = np.isfinite(on)
        if g.any():
            book[k] = on[g].mean() - COST
        spy[k] = spy_on[t + 1]
        id_same[k] = np.nanmean(ID[t, pick])
        id_same_univ[k] = np.nanmean(ID[t, idx])
        id_next[k] = np.nanmean(ID[t + 1, pick])
        id_next_univ[k] = np.nanmean(ID[t + 1, idx])

    d = pd.DatetimeIndex(dates[nights])
    is_m, oos_m = np.asarray(d <= IS_END), np.asarray(d > IS_END)
    ok = np.isfinite(book) & np.isfinite(spy)
    b_is = np.polyfit(spy[ok & is_m], book[ok & is_m], 1)[0]
    b_oos = np.polyfit(spy[ok & oos_m], book[ok & oos_m], 1)[0]
    print(f"nights IS {is_m.sum()} OOS {oos_m.sum()}; book beta to SPY overnight: IS {b_is:.2f}, OOS {b_oos:.2f}\n")

    print("(9) SPY OVERNIGHT HEDGE (book net of 4 bp; hedge legs cost 2.26 bp x h; bp per night)")
    print(f"  {'book':<16}{'period':<5}{'mean':>8}{'t':>7}{'sd':>7}{'worst':>8}{'CVaR1%':>8}{'worst mo':>10}{'max DD':>9}")
    for label, h in (("unhedged", 0.0), ("h 0.5", 0.5), ("h 1.0", 1.0), ("h 1.4", 1.4), (f"h IS {b_is:.2f}", b_is)):
        x = book - h * spy - (SPY_COST * h if h else 0.0)
        for pname, msk in (("IS", is_m), ("OOS", oos_m)):
            mm = ok & msk
            r = tail(x[mm], d[mm])
            print(f"  {label:<16}{pname:<5}{r[0]:>+8.1f}{r[1]:>+7.2f}{r[2]:>7.0f}{r[3]:>+8.0f}{r[4]:>+8.0f}{r[5]:>+10.0f}{r[6]:>+9.0f}")

    print("\n(12) INTRADAY LEG of the top-20 (open -> close, bp; excess = picks - all eligible names that day)")
    print(f"  {'leg':<28}{'period':<5}{'picks':>8}{'all':>8}{'excess':>8}{'t':>7}")
    for label, a, u in (("day OF the buy (t)", id_same, id_same_univ), ("day AFTER the hold (t+1)", id_next, id_next_univ)):
        for pname, msk in (("IS", is_m), ("OOS", oos_m)):
            g = msk & np.isfinite(a) & np.isfinite(u)
            ex = (a[g] - u[g]) * 1e4
            tt = ex.mean() / (ex.std(ddof=1) / math.sqrt(len(ex)))
            print(f"  {label:<28}{pname:<5}{np.mean(a[g]) * 1e4:>+8.1f}{np.mean(u[g]) * 1e4:>+8.1f}{ex.mean():>+8.1f}{tt:>+7.2f}")


if __name__ == "__main__":
    main()
