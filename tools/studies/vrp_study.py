#!/usr/bin/env python3
"""vrp_study.py — volatility risk premium: is selling SPX option insurance a return separate from equity beta?
Pre-registered in docs/studies/vrp_prereg.json. Q1-Q3 here (CBOE + Yahoo, free); Q4 (SPY option spreads) is
vrp_spy_cost.py on the mini.

usage: vrp_study.py CACHE_DIR      (downloads CBOE CSVs and Yahoo series into CACHE_DIR on first run)
"""
import csv
import math
import os
import sys
import urllib.request
from datetime import date, datetime

import numpy as np

CBOE = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{}_History.csv"
INDEXES = ("PUT", "WPUT", "BXM", "CNDR", "PPUT")
PERIODS = {
    "PUT": [("2007-07", "2012-12"), ("2013-01", "2019-12"), ("2020-01", "2026-09")],
    "WPUT": [("2006-02", "2012-12"), ("2013-01", "2019-12"), ("2020-01", "2026-09")],
    "BXM": [("2002-04", "2006-12"), ("2007-01", "2012-12"), ("2013-01", "2019-12"), ("2020-01", "2026-09")],
    "CNDR": [("1987-01", "2006-12"), ("2007-01", "2016-12"), ("2017-01", "2026-09")],
    "PPUT": [("1987-01", "2006-12"), ("2007-01", "2016-12"), ("2017-01", "2026-09")],
}


def cboe(cache, sym):
    p = os.path.join(cache, f"{sym}.csv")
    if not os.path.exists(p):
        req = urllib.request.Request(CBOE.format(sym), headers={"User-Agent": "Mozilla/5.0"})
        open(p, "wb").write(urllib.request.urlopen(req, timeout=60).read())
    out = {}
    with open(p) as f:
        rd = csv.reader(f)
        head = next(rd)
        col = head.index("CLOSE") if "CLOSE" in head else 1
        for r in rd:
            if len(r) > col and r[col]:
                out[datetime.strptime(r[0], "%m/%d/%Y").date()] = float(r[col])
    return out


def yahoo(cache, tick):
    p = os.path.join(cache, tick.strip("^") + ".csv")
    if not os.path.exists(p):
        import yfinance as yf
        s = yf.download(tick, start="1985-01-01", end="2026-10-02", auto_adjust=True, progress=False)["Close"].dropna()
        s = s.iloc[:, 0] if getattr(s, "ndim", 1) > 1 else s
        with open(p, "w") as f:
            for d, v in s.items():
                f.write(f"{d.date()},{float(v)}\n")
    return {date.fromisoformat(a): float(b) for a, b in (ln.strip().split(",") for ln in open(p))}


def month_ends(series):
    """{YYYY-MM: (date, value)} last observation per calendar month."""
    out = {}
    for d in sorted(series):
        out[d.strftime("%Y-%m")] = (d, series[d])
    return out


def monthly_returns(series):
    me = month_ends(series)
    ks = sorted(me)
    return {k: me[k][1] / me[p][1] - 1 for p, k in zip(ks, ks[1:])}


def nw_ols(y, x, lags=3):
    """OLS y = a + b x with Newey-West SEs. Returns (a, b, t_a, t_b)."""
    y, x = np.asarray(y, float), np.asarray(x, float)
    X = np.column_stack([np.ones_like(x), x])
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    e = y - X @ beta
    n = len(y)
    S = (X * e[:, None]).T @ (X * e[:, None])
    for L in range(1, lags + 1):
        w = 1 - L / (lags + 1)
        G = (X[L:] * e[L:, None]).T @ (X[:-L] * e[:-L, None])
        S += w * (G + G.T)
    XtXi = np.linalg.inv(X.T @ X)
    V = XtXi @ S @ XtXi * n / (n - 2)
    se = np.sqrt(np.diag(V))
    return beta[0], beta[1], beta[0] / se[0], beta[1] / se[1]


def max_dd(series, lo, hi):
    v = [series[d] for d in sorted(series) if lo <= d.strftime("%Y-%m") <= hi]
    peak, dd = -1e18, 0.0
    for x in v:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1)
    return dd


def stats_block(name, idx_m, spx_m, rf_m, months, idx_daily, spx_daily, lo, hi):
    y = np.array([idx_m[k] - rf_m[k] for k in months]); x = np.array([spx_m[k] - rf_m[k] for k in months])
    a, b, ta, tb = nw_ols(y, x)
    r_i = np.array([idx_m[k] for k in months]); r_s = np.array([spx_m[k] for k in months])
    ann = lambda r: (np.prod(1 + r) ** (12 / len(r)) - 1) * 100  # noqa: E731
    sh = lambda ex: ex.mean() / ex.std(ddof=1) * math.sqrt(12)  # noqa: E731
    wi = int(np.argmin(r_i))
    print(f"  {name:5s} {lo}..{hi} n={len(months):3d}  alpha {a * 1200:+5.2f}%/yr (t {ta:+.2f})  beta {b:.2f}  "
          f"ann {ann(r_i):+5.1f}% vs SPX-TR {ann(r_s):+5.1f}%  vol {r_i.std(ddof=1) * math.sqrt(12) * 100:4.1f}% vs "
          f"{r_s.std(ddof=1) * math.sqrt(12) * 100:4.1f}%  Sharpe {sh(y):+.2f} vs {sh(x):+.2f}  maxDD "
          f"{max_dd(idx_daily, lo, hi) * 100:5.1f}% vs {max_dd(spx_daily, lo, hi) * 100:5.1f}%  worst month "
          f"{r_i[wi] * 100:+.1f}% ({months[wi]}; SPX {r_s[wi] * 100:+.1f}%)")
    return y, x


def main():
    cache = sys.argv[1]
    os.makedirs(cache, exist_ok=True)
    spx = cboe(cache, "SPX"); vix = cboe(cache, "VIX"); vix3m = cboe(cache, "VIX3M")
    tr = yahoo(cache, "^SP500TR"); irx = yahoo(cache, "^IRX")
    idx = {s: cboe(cache, s) for s in INDEXES}
    for s, v in [("SPX", spx), ("VIX", vix), ("SP500TR", tr), ("IRX", irx)] + list(idx.items()):
        print(f"  data {s:8s} {min(v)}..{max(v)} n={len(v)}")

    # Q1: VIX vs next-21-session realized vol
    print("\n=== Q1: VIX at month end vs realized vol of SPX over the next 21 sessions (vol points)")
    ds = sorted(spx)
    lr = {ds[i]: math.log(spx[ds[i]] / spx[ds[i - 1]]) for i in range(1, len(ds))}
    pos = {d: i for i, d in enumerate(ds)}
    rows = []
    for k, (d, v) in month_ends(vix).items():
        if d not in pos or pos[d] + 21 >= len(ds):
            continue
        r = [lr[ds[pos[d] + j]] for j in range(1, 22)]
        rv = math.sqrt(np.mean(np.square(r)) * 252) * 100
        rows.append((k, v, rv))
    for lo, hi in [("1990-01", "2006-12"), ("2007-01", "2016-12"), ("2017-01", "2026-09"), ("1990-01", "2026-09")]:
        g = np.array([r[1] - r[2] for r in rows if lo <= r[0] <= hi])
        worst = sorted([r for r in rows if lo <= r[0] <= hi], key=lambda r: r[1] - r[2])[:3]
        print(f"  {lo}..{hi} n={len(g)}: mean VIX-RV {g.mean():+.2f}  median {np.median(g):+.2f}  positive "
              f"{(g > 0).mean():.0%}  worst " + ", ".join(f"{r[0]} {r[1] - r[2]:+.0f}" for r in worst))

    # Q2: alpha vs SPX TR
    spx_m = monthly_returns(tr)
    irx_me = month_ends(irx)
    ks = sorted(irx_me)
    rf_m = {k: irx_me[p][1] / 100 / 12 for p, k in zip(ks, ks[1:])}
    print("\n=== Q2: monthly excess return over T-bills regressed on SPX total-return excess (Newey-West 3 lags)")
    pooled = {}
    for s in INDEXES:
        im = monthly_returns(idx[s])
        allm = []
        for lo, hi in PERIODS[s]:
            months = [k for k in sorted(im) if lo <= k <= hi and k in spx_m and k in rf_m]
            stats_block(s, im, spx_m, rf_m, months, idx[s], tr, lo, hi)
            allm += months
        y, x = stats_block(s, im, spx_m, rf_m, allm, idx[s], tr, allm[0], allm[-1])
        pooled[s] = (im, allm)
        print()

    # Q3: timing rules on PUT (info only)
    print("=== Q3 (info only): PUT in-the-market months under pre-set rules vs always-on")
    im, allm = pooled["PUT"]
    vme, v3me = month_ends(vix), month_ends(vix3m)
    rvme = {r[0]: r[2] for r in rows}  # forward RV keyed by month (not used for rules)
    del rvme
    trail = {}
    for k, (d, _v) in month_ends(spx).items():
        i = pos.get(d)
        if i and i >= 21:
            trail[k] = math.sqrt(np.mean(np.square([lr[ds[i - j]] for j in range(21)])) * 252) * 100
    prev = {k: p for p, k in zip(sorted(vme), sorted(vme)[1:])}
    rules = {
        "always": lambda p: True,
        "contango VIX3M>VIX": lambda p: p in v3me and p in vme and v3me[p][1] > vme[p][1],
        "VIX > trailing 21d RV": lambda p: p in vme and p in trail and vme[p][1] > trail[p],
    }
    for name, rule in rules.items():
        on = [k for k in allm if k in prev and (k >= "2009-10" or name != "contango VIX3M>VIX") and rule(prev[k])]
        base = [k for k in allm if k in prev and (k >= "2009-10" or name != "contango VIX3M>VIX")]
        off = [k for k in base if k not in on]
        if len(on) > 12:
            y = np.array([im[k] - rf_m[k] for k in on]); x = np.array([spx_m[k] - rf_m[k] for k in on])
            a, b, ta, _ = nw_ols(y, x)
            yo = np.array([im[k] - rf_m[k] for k in off]) if off else np.array([])
            print(f"  {name:22s} on {len(on):3d}/{len(base)} months: alpha {a * 1200:+5.2f}%/yr (t {ta:+.2f}) beta {b:.2f}, "
                  f"mean excess on {y.mean() * 100:+.2f}%/mo" + (f", off {yo.mean() * 100:+.2f}%/mo (n {len(yo)})" if len(yo) else ""))


if __name__ == "__main__":
    main()
