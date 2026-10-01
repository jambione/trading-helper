#!/usr/bin/env python3
"""month_end_rebal.py — month-end pension rebalancing flow on SPY/TLT (pre-registered in
docs/studies/month_end_rebal_prereg.json).

Signal S = SPY minus TLT return from the prior month's last close to close(d_{T-k}); trade -sign(S) over the
last k sessions of the month (close to close, auction fills). Placebo = the same rule on every non-month-end
window with a 15-session signal. Primary data: Alpaca SIP adjusted daily bars (edge5 daily_long.pkl), split
60/20/20 by month. Second OOS: Yahoo adjusted closes 2002-08..2015-12 (yfinance, cached next to the pickle).

usage: month_end_rebal.py PATH/daily_long.pkl
"""
import math
import os
import pickle
import sys

import numpy as np

COST = {"A": 0.26 + 2.0, "B": 0.26 + 1.17 + 4.0, "C": 0.26 + 2.0}
KS = (3, 1, 5)
PLACEBO_LOOKBACK = 15


def tstat(a):
    a = np.asarray(a, float)
    return a.mean() / (a.std(ddof=1) / math.sqrt(len(a))) if len(a) > 2 else float("nan")


def welch(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 3 or len(b) < 3:
        return float("nan")
    return (a.mean() - b.mean()) / math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))


def spearman(x, y):
    rx = np.argsort(np.argsort(x)); ry = np.argsort(np.argsort(y))
    return float(np.corrcoef(rx, ry)[0, 1]) if len(x) > 2 else float("nan")


def month_events(ds, s, t, k):
    """One row per month: (month, S, spy_ret, tlt_ret, next3_spy, next3_tlt)."""
    ends = [i for i in range(len(ds) - 1) if ds[i][:7] != ds[i + 1][:7]] + [len(ds) - 1]
    rows = []
    for a, b in zip(ends, ends[1:]):  # a = last session of prior month, b = last session of this month
        if b - k <= a:
            continue
        e = b - k
        S = (s[e] / s[a] - 1) - (t[e] / t[a] - 1)
        n3 = (s[b + 3] / s[b] - 1, t[b + 3] / t[b] - 1) if b + 3 < len(ds) and ds[b + 3][:7] != ds[b][:7] else (np.nan, np.nan)
        rows.append((ds[b][:7], S, s[b] / s[e] - 1, t[b] / t[e] - 1, n3[0], n3[1]))
    return rows


def placebo(ds, s, t, k, lo, hi):
    """Non-month-end windows with dates in [lo, hi]: (S, spy_ret, tlt_ret)."""
    n = len(ds)
    month_end = [i for i in range(n - 1) if ds[i][:7] != ds[i + 1][:7]]
    excl = set()
    for b in month_end:
        excl.update(range(b - 4, b + 1)); excl.update(range(b + 1, b + 4))
    out = []
    for i in range(PLACEBO_LOOKBACK, n - k):
        if i in excl or not (lo <= ds[i][:7] <= hi):
            continue
        a = i - PLACEBO_LOOKBACK
        S = (s[i] / s[a] - 1) - (t[i] / t[a] - 1)
        out.append((S, s[i + k] / s[i] - 1, t[i + k] / t[i] - 1))
    return out


def pnl(rows, strat):
    """rows of (S, rs, rt) -> net bp per trade (C: only S<0 trades)."""
    out = []
    for S, rs, rt in rows:
        d = -np.sign(S)
        if strat == "A":
            out.append(d * rs * 1e4 - COST["A"])
        elif strat == "B":
            out.append(d * (rs - rt) * 1e4 - COST["B"])
        elif strat == "C" and S < 0:
            out.append(rs * 1e4 - COST["C"])
    return out


def fmt(a, base=None):
    if len(a) < 3:
        return f"n={len(a)}"
    s = f"n={len(a):3d} net {np.mean(a):+7.1f} (t {tstat(a):+.2f})"
    if base is not None:
        s += f"  excess {np.mean(a) - np.mean(base):+7.1f} (t {welch(a, base):+.2f})"
    return s


def report(label, ds, s, t, parts, thresholds=None):
    """parts: list of (name, lo_month, hi_month). Returns tune median |S| per k for the strong-half cut."""
    med = {}
    print(f"\n######## {label}")
    for k in KS:
        ev = month_events(ds, s, t, k)
        print(f"\n=== k={k} (last {k} sessions){'  PRIMARY' if k == 3 else ''}")
        for name, lo, hi in parts:
            e = [r for r in ev if lo <= r[0] <= hi]
            pl = placebo(ds, s, t, k, lo, hi)
            rows = [(r[1], r[2], r[3]) for r in e]
            if name == parts[0][0] and thresholds is None:
                med[k] = float(np.median([abs(r[0]) for r in rows]))
            thr = (thresholds or med)[k]
            allwin = [rs * 1e4 - COST["C"] for _S, rs, _rt in pl]
            print(f"  [{name}] {lo}..{hi}  months={len(e)}  placebo windows={len(pl)}")
            for st in ("A", "B"):
                print(f"    {st}  {fmt(pnl(rows, st), pnl(pl, st))}")
            print(f"    C  {fmt(pnl(rows, 'C'), allwin)}   (C excess vs every SPY {k}-session window)")
            strong = [r for r in rows if abs(r[0]) > thr]
            pls = [r for r in pl if abs(r[0]) > thr]
            for st in ("A", "B"):
                print(f"    {st} strong |S|>{thr * 1e4:.0f}bp  {fmt(pnl(strong, st), pnl(pls, st))}")
            x = np.array([r[0] for r in rows]); y = np.array([r[1] - r[2] for r in rows])
            print(f"    info: spearman(S, SPY-TLT window) {spearman(x, y):+.3f}  spearman(S, SPY window) "
                  f"{spearman(x, np.array([r[1] for r in rows])):+.3f}")
            if k == 3:
                nx = [(r[1], r[4], r[5]) for r in e if not np.isnan(r[4])]
                print(f"    info: next-month first 3 sessions, same -sign(S): A {fmt(pnl(nx, 'A'))} | B {fmt(pnl(nx, 'B'))}")
                q = [(r[1], r[2], r[3]) for r in e if r[0][5:] in ("03", "06", "09", "12")]
                nq = [(r[1], r[2], r[3]) for r in e if r[0][5:] not in ("03", "06", "09", "12")]
                print(f"    info: quarter-end A {fmt(pnl(q, 'A'))} | B {fmt(pnl(q, 'B'))}")
                print(f"    info: other months A {fmt(pnl(nq, 'A'))} | B {fmt(pnl(nq, 'B'))}")
    return med


def alpaca_series(pkl):
    D = pickle.load(open(pkl, "rb"))
    sp = {r[0]: r[2] for r in D["SPY"]}; tl = {r[0]: r[2] for r in D["TLT"]}
    ds = sorted(set(sp) & set(tl))
    return ds, np.array([sp[d] for d in ds]), np.array([tl[d] for d in ds])


def yahoo_series(cache):
    if not os.path.exists(cache):
        import yfinance as yf
        df = yf.download(["SPY", "TLT"], start="2002-07-01", end="2026-09-26", auto_adjust=True, progress=False)["Close"]
        df = df.dropna()
        pickle.dump([(d.strftime("%Y-%m-%d"), float(r["SPY"]), float(r["TLT"])) for d, r in df.iterrows()], open(cache, "wb"))
    rows = pickle.load(open(cache, "rb"))
    return [r[0] for r in rows], np.array([r[1] for r in rows]), np.array([r[2] for r in rows])


def main():
    pkl = sys.argv[1]
    ds, s, t = alpaca_series(pkl)
    months = sorted({d[:7] for d in ds})[1:]  # first month has no prior-month close
    m = len(months)
    a, b = int(m * 0.6), int(m * 0.8)
    parts = [("tune", months[0], months[a - 1]), ("validate", months[a], months[b - 1]), ("holdout", months[b], months[-1])]
    print(f"Alpaca SPY/TLT {ds[0]}..{ds[-1]}, {len(ds)} sessions, {m} months; costs A/C {COST['A']:.2f} bp, B {COST['B']:.2f} bp")
    med = report("ALPACA (primary)", ds, s, t, parts)

    yd, ys, yt = yahoo_series(os.path.join(os.path.dirname(os.path.abspath(pkl)), "yahoo_spy_tlt.pkl"))
    ov = {d: i for i, d in enumerate(yd)}
    common = [d for d in ds if d in ov]
    ia = np.array([ds.index(d) for d in common]); iy = np.array([ov[d] for d in common])
    ra = np.diff(s[ia]) / s[ia][:-1]; ry = np.diff(ys[iy]) / ys[iy][:-1]
    rta = np.diff(t[ia]) / t[ia][:-1]; rty = np.diff(yt[iy]) / yt[iy][:-1]
    print(f"\nYahoo vs Alpaca daily returns on {len(common)} common sessions: corr SPY {np.corrcoef(ra, ry)[0, 1]:.4f}, "
          f"TLT {np.corrcoef(rta, rty)[0, 1]:.4f}; median |diff| SPY {np.median(abs(ra - ry)) * 1e4:.2f} bp, "
          f"TLT {np.median(abs(rta - rty)) * 1e4:.2f} bp")
    cut = [i for i, d in enumerate(yd) if d < "2016-01-01"]
    yd2, ys2, yt2 = yd[:cut[-1] + 1], ys[:cut[-1] + 1], yt[:cut[-1] + 1]
    ym = sorted({d[:7] for d in yd2})[1:]
    report("YAHOO pre-2016 (second OOS; strong-half threshold from Alpaca tune)", yd2, ys2, yt2,
           [("pre2016", ym[0], ym[-1])], thresholds=med)


if __name__ == "__main__":
    main()
