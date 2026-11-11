#!/usr/bin/env python3
"""daily_horizon.py - does a days-to-weeks horizon carry an edge? (docs/studies/daily_horizon_prereg.json, operator 11/11)

Data: ai_reports/gapdown/daily.pkl - fully adjusted (open, close) for every name that was ever liquid 2016-2026
(close >= $10 and 20-day mean dollar volume >= $20M on the prior session), plus SPY. A name is tradeable on day d only
if d is in its liquid list, which is computed from sessions before d. Delisted names are in the file for 2018-2022 but
nearly absent for 2023-2025, so the VERDICT window is 2017-2022 (split at 2020-01-01) and 2023+ is shown as information.

Every rule decides at the CLOSE of day t, buys the OPEN of t+1 and sells a later OPEN. A name that stops trading before
the exit is sold at its last close. Indicators need a full window of real bars (no short-history names).

  COHORT rules (1-5): every H sessions form the basket, hold H sessions, no overlap. Verdict: the intercept of basket
  return regressed on the equal-weight return of all tradeable names over the same windows (beta-adjusted lift), per half
  and pooled, and that intercept after cost.

  RIDE rule (6): buy a new 60-day closing high above the 200-day average, hold until the first close under the 50-day
  average. Per trade, the control is buying every tradeable name at the same open and selling at the same open
  (same clip, same delisting rule). Lift is the RATIO (1+r)/(1+c)-1, because an arithmetic difference over a hold the exit
  rule picks is positive on pure drift (skeptic 11/11: 3 of 16 no-edge simulations read PASS). The t is cluster-robust by
  entry quarter on the trade-weighted mean. A 10-position account over 200 tie-break seeds is reported, not a verdict.

    .venv/bin/python tools/studies/daily_horizon.py            # mini (the table lives there)
"""
from __future__ import annotations

import math
import os
import pickle
import sys
import warnings
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

ROOT = Path(os.environ.get("REPO") or Path(__file__).resolve().parents[2])
COST, STRESS = 10.0, 30.0                 # bp per round trip
T_CRIT = 2.64                             # two-sided 5% over 6 hypotheses
V0, V1, VSPLIT = "2017-01-01", "2023-01-01", "2020-01-01"       # verdict window and its halves
WARM = 260
CLIP_LO, CLIP_HI = -0.9, 3.0


def load(path=None):
    d = pickle.load(open(path or ROOT / "ai_reports" / "gapdown" / "daily.pkl", "rb"))
    days = sorted(d["adj"]["SPY"])
    ix = {x: i for i, x in enumerate(days)}
    missing = sorted(s for s in d["liquid"] if s not in d["adj"])
    syms = sorted(s for s in d["liquid"] if s in d["adj"] and s != "SPY")
    T, N = len(days), len(syms)
    O, C, L = np.full((T, N), np.nan), np.full((T, N), np.nan), np.zeros((T, N), bool)
    for j, s in enumerate(syms):
        for day, (o, c) in d["adj"][s].items():
            i = ix.get(day)
            if i is not None and o > 0 and c > 0:
                O[i, j], C[i, j] = o, c
        for day in d["liquid"][s]:
            i = ix.get(day)
            if i is not None:
                L[i, j] = True
    spy = np.array([d["adj"]["SPY"][x] for x in days])
    failed = [x for x in d.get("log", []) if str(x).startswith("FAILED")]
    return {"days": days, "syms": syms, "O": O, "C": C, "L": L, "spy_o": spy[:, 0], "missing": missing, "failed": failed}


def ffill0(a):
    """Forward-fill NaN down the rows."""
    idx = np.where(~np.isnan(a), np.arange(a.shape[0])[:, None], 0)
    np.maximum.accumulate(idx, axis=0, out=idx)
    out = a[idx, np.arange(a.shape[1])[None, :]]
    out[(idx == 0) & np.isnan(a[0])[None, :]] = np.nan
    return out


def roll0(a, n, fn):
    out = np.full(a.shape, np.nan)
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        out[n - 1:] = fn(sliding_window_view(a, n, axis=0), axis=-1)
    return out


def full_window(C, n):
    """True where at least 95% of the last n sessions have a real bar (no indicator on a short history)."""
    have = roll0((~np.isnan(C)).astype(float), n, np.nansum)
    return have >= 0.95 * n


def span_ret(O, Cf, a, b):
    """Simple return for buying the open of day a and selling the open of day b; a name gone by b is sold at its last close.
    Clipped to [-90%, +300%], the same for a trade and for its control."""
    e = O[a]
    x = O[b] if b < O.shape[0] else np.full(O.shape[1], np.nan)
    x = np.where(np.isnan(x), Cf[min(b - 1, O.shape[0] - 1)], x)
    with np.errstate(all="ignore"):
        return np.clip(x / e - 1.0, CLIP_LO, CLIP_HI)


def window_ret(O, Cf, t, h):
    """bp: decide at the close of t, buy the open of t+1, sell the open of t+1+h."""
    return span_ret(O, Cf, t + 1, t + 1 + h) * 1e4


def tstat(v):
    v = np.asarray(v, float)
    v = v[~np.isnan(v)]
    if len(v) < 3 or v.std(ddof=1) == 0:
        return float("nan")
    return float(v.mean() / (v.std(ddof=1) / math.sqrt(len(v))))


def alpha_t(y, x):
    """OLS y = a + b x; returns (a, t of a, b)."""
    y, x = np.asarray(y, float), np.asarray(x, float)
    ok = ~np.isnan(y) & ~np.isnan(x)
    y, x = y[ok], x[ok]
    if len(y) < 10:
        return float("nan"), float("nan"), float("nan")
    X = np.column_stack([np.ones(len(x)), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    res = y - X @ beta
    cov = (res @ res) / (len(y) - 2) * np.linalg.inv(X.T @ X)
    return float(beta[0]), float(beta[0] / math.sqrt(cov[0, 0])), float(beta[1])


def cluster_t(x, groups):
    """Cluster-robust t of the TRADE-WEIGHTED mean of x, clusters = groups (entry quarters)."""
    x, groups = np.asarray(x, float), np.asarray(groups)
    m, n = x.mean(), len(x)
    labs = sorted(set(groups))
    g = len(labs)
    if g < 5:
        return float("nan")
    s = np.array([(x[groups == q] - m).sum() for q in labs])
    var = (s @ s) / n ** 2 * g / (g - 1)
    return float(m / math.sqrt(var)) if var > 0 else float("nan")


def cohort_test(name, pick, h, D):
    """pick(t) -> bool mask of names chosen at the close of t. Non-overlapping cohorts every h sessions."""
    days, O, Cf, L, so = D["days"], D["O"], D["Cf"], D["L"], D["spy_o"]
    rows = []
    for t in range(WARM, len(days) - h - 2, h):
        uni = L[t + 1] & ~np.isnan(O[t + 1]) & ~np.isnan(D["C"][t])
        sel = pick(t) & uni
        r = window_ret(O, Cf, t, h)
        if sel.sum() < 5 or uni.sum() < 100:
            continue
        rows.append((days[t + 1], float(np.nanmean(r[sel])), float(np.nanmean(r[uni])), float((so[t + 1 + h] / so[t + 1] - 1) * 1e4), int(sel.sum())))
    d = np.array([x[0] for x in rows])
    b, u, s, k = (np.array([x[i] for x in rows], float) for i in (1, 2, 3, 4))
    win = (d >= V0) & (d < V1)
    h1, h2, late = win & (d < VSPLIT), win & (d >= VSPLIT), d >= V1
    if win.sum() < 20:
        return {"name": name, "h": h, "n": int(win.sum()), "verdict": "TOO FEW COHORTS"}
    a, at, beta = alpha_t(b[win], u[win])                       # beta-adjusted lift against all tradeable names
    a1, a2 = alpha_t(b[h1], u[h1])[0], alpha_t(b[h2], u[h2])[0]
    sa, sat, sbeta = alpha_t(b[win] - COST, s[win])
    per_year = 252.0 / h
    eq = np.cumprod(1 + (b[win] - COST) / 1e4)
    res = {"name": name, "h": h, "n": int(win.sum()), "names": float(k[win].mean()),
           "raw_lift": float((b - u)[win].mean()), "name_weighted_lift": float(((b - u) * k)[win].sum() / k[win].sum()),
           "alpha": a, "alpha_t": at, "beta": beta, "alpha_h": (a1, a2),
           "late_raw_lift": float((b - u)[late].mean()) if late.sum() > 3 else float("nan"), "late_n": int(late.sum()),
           "spy_alpha": sa, "spy_alpha_t": sat, "spy_beta": sbeta,
           "cagr": float(eq[-1] ** (per_year / len(eq)) - 1), "cagr_stress": float(np.prod(1 + (b[win] - STRESS) / 1e4) ** (per_year / win.sum()) - 1),
           "spy_cagr": float(np.prod(1 + s[win] / 1e4) ** (per_year / win.sum()) - 1), "uni_cagr": float(np.prod(1 + u[win] / 1e4) ** (per_year / win.sum()) - 1),
           "maxdd": float((eq / np.maximum.accumulate(eq) - 1).min())}
    pos = lambda v: v == v and v > 0
    edge = pos(a1) and pos(a2) and at >= T_CRIT
    money = edge and pos(a1 - STRESS) and pos(a2 - STRESS)
    res["verdict"] = "PASS" if money else "EDGE only (beats the universe, not its cost)" if edge else "FAIL"
    return res


def ride_trades(D):
    """-> list of (entry day index, exit day index, name index, r, c): simple returns of the trade and of its control."""
    O, C, Cf, L = D["O"], D["C"], D["Cf"], D["L"]
    T, N = C.shape
    ok = full_window(C, 200)
    hi60, ma50, ma200 = roll0(Cf, 60, np.nanmax), roll0(Cf, 50, np.nanmean), roll0(Cf, 200, np.nanmean)
    with np.errstate(all="ignore"):
        at_high = (C >= hi60 - 1e-9) & (C > ma200) & ok
        below50 = C < ma50
    recent = roll0(at_high.astype(float), 20, np.nanmax)
    fresh = at_high.copy()
    fresh[1:] &= ~(recent[:-1] >= 1)                                     # the first new high in 20 sessions
    raw = []
    for j in range(N):
        t = WARM
        col_f, col_b = fresh[:, j], below50[:, j]
        while t < T - 2:
            if col_f[t] and L[t + 1, j] and not np.isnan(O[t + 1, j]):
                s = t + 1
                while s < T - 2 and not col_b[s] and not np.isnan(C[s, j]) and s - t < 250:
                    s += 1
                raw.append((t + 1, s + 1, j))
                t = s + 1
            else:
                t += 1
    cache: dict[tuple[int, int], float] = {}
    out = []
    for a, b, j in raw:
        r = float(span_ret(O[:, [j]], Cf[:, [j]], a, b)[0])
        if (a, b) not in cache:                                          # buy every tradeable name at the same open, sell at the same open
            uni = L[a] & ~np.isnan(O[a])
            assert uni.sum() > 100, f"thin universe on day index {a}"
            cache[(a, b)] = float(np.nanmean(span_ret(O, Cf, a, b)[uni]))
        if not math.isnan(r):
            out.append((a, b, j, r, cache[(a, b)]))
    return out


def ride_stats(trades, days):
    tr = np.array([(a, b, r, c) for a, b, _j, r, c in trades], float)
    ent = np.array([days[int(a)] for a in tr[:, 0]])
    r, c = tr[:, 2], tr[:, 3]
    lift = ((1 + r) / (1 + c) - 1) * 1e4                                 # ratio lift, bp
    qtr = np.array([x[:4] + "Q" + str((int(x[5:7]) - 1) // 3 + 1) for x in ent])
    win = (ent >= V0) & (ent < V1)
    h1, h2, late = win & (ent < VSPLIT), win & (ent >= VSPLIT), ent >= V1
    _a, _t, beta = alpha_t(r[win] * 1e4, c[win] * 1e4)
    resid = r * 1e4 - beta * c * 1e4                                     # beta-adjusted, bp
    hold = tr[:, 1] - tr[:, 0]
    tot = (r[win] * 1e4).sum()
    out = {"trades": int(win.sum()), "hold_med": float(np.median(hold[win])), "gross": float(r[win].mean() * 1e4),
           "median": float(np.median(r[win]) * 1e4), "win": float(np.mean(r[win] * 1e4 > COST)),
           "lift": float(lift[win].mean()), "lift_t": cluster_t(lift[win], qtr[win]), "lift_h": (float(lift[h1].mean()), float(lift[h2].mean())),
           "beta": beta, "resid": float(resid[win].mean()), "resid_t": cluster_t(resid[win], qtr[win]),
           "late_lift": float(lift[late].mean()) if late.sum() else float("nan"), "late_n": int(late.sum()),
           "top1pct_share": float(np.sort(r[win] * 1e4)[-max(1, int(win.sum()) // 100):].sum() / tot) if tot else float("nan")}
    pos = lambda v: v == v and v > 0
    edge = pos(out["lift_h"][0]) and pos(out["lift_h"][1]) and out["lift_t"] >= T_CRIT and out["resid_t"] >= T_CRIT
    out["verdict"] = "PASS" if edge and out["lift"] - STRESS > 0 else "EDGE only (beats the universe, not its cost)" if edge else "FAIL"
    return out


def account(trades, D, seed, slots=10):
    """Equal 1/slots stakes, signals in date order (same-day ties shuffled), half the cost on each side, marked on filled opens."""
    days, Of = D["days"], D["Of"]
    T = len(days)
    rng = np.random.default_rng(seed)
    by_entry: dict[int, list] = {}
    for a, b, j, _r, _c in trades:
        by_entry.setdefault(a, []).append((b, j))
    eq, cash, pos, curve = 1.0, 1.0, {}, []
    for t in range(WARM, T - 1):
        for j in [j for j, (b, _v) in pos.items() if b == t]:
            _b, v = pos.pop(j)
            cash += v * (1 - COST / 2e4)
        cands = list(by_entry.get(t, []))
        rng.shuffle(cands)
        for b, j in cands:
            if len(pos) >= slots or j in pos:
                continue
            stake = min(cash, eq / slots)
            if stake <= 1e-9:
                break
            cash -= stake
            pos[j] = (b, stake * (1 - COST / 2e4))
        for j, (b, v) in list(pos.items()):
            pos[j] = (b, v * float(np.clip(Of[t + 1, j] / Of[t, j], 1 + CLIP_LO, 1 + CLIP_HI)))
        eq = cash + sum(v for _b, v in pos.values())
        curve.append(eq)
    return np.array(curve)


def curve_stats(e, days, lo, hi):
    d = np.array(days[WARM + 1:WARM + 1 + len(e)])
    m = (d >= lo) & (d < hi)
    x = e[m] / e[m][0]
    yrs = len(x) / 252.0
    dr = np.diff(np.log(x))
    return float(x[-1] ** (1 / yrs) - 1), float((x / np.maximum.accumulate(x) - 1).min()), float(dr.mean() / dr.std() * math.sqrt(252))


def rules(D):
    C, Cf, L = D["C"], D["Cf"], D["L"]
    hi252, hi60, ma200 = roll0(Cf, 252, np.nanmax), roll0(Cf, 60, np.nanmax), roll0(Cf, 200, np.nanmean)
    f252, f200, f126 = full_window(C, 252), full_window(C, 200), full_window(C, 126)

    def past(t, a, b):
        with np.errstate(all="ignore"):
            return Cf[t - b] / Cf[t - a] - 1

    def decile(score, uni, top=True):
        s = np.where(uni & ~np.isnan(score), score, np.nan)
        if np.sum(~np.isnan(s)) < 50:
            return np.zeros(len(s), bool)
        cut = np.nanpercentile(s, 90 if top else 10)
        return (s >= cut) if top else (s <= cut)

    def uni(t):
        return L[t + 1] & ~np.isnan(C[t])
    return [
        ("1 momentum 6-1 months, top tenth", lambda t: decile(past(t, 126, 21), uni(t) & f126[t]), 21),
        ("2 nearest the 52-week high, top tenth", lambda t: decile(Cf[t] / hi252[t], uni(t) & f252[t]), 21),
        ("3 new 60-day high above 200-day avg", lambda t: uni(t) & f200[t] & (C[t] >= hi60[t] - 1e-9) & (C[t] > ma200[t]), 10),
        ("4 one-week losers, bottom tenth", lambda t: decile(past(t, 5, 0), uni(t)), 5),
        ("5 one-month momentum, top tenth", lambda t: decile(past(t, 21, 0), uni(t)), 21),
    ]


def main() -> int:
    D = load()
    D["Cf"], D["Of"] = ffill0(D["C"]), ffill0(D["O"])
    days, L = D["days"], D["L"]
    print(f"{len(D['syms'])} names ever liquid, {days[0]}..{days[-1]} ({len(days)} sessions); tradeable per day: median {int(np.median(L.sum(1)[WARM:]))}; "
          f"liquid names with no adjusted series {len(D['missing'])}; FAILED fetch lines {len(D['failed'])}")
    print(f"VERDICT window {V0[:4]}-2022 (halves split {VSPLIT}); 2023+ shown as information (delisted names mostly missing there). "
          f"Cost {COST:g} bp, money line at {STRESS:g} bp; t needed {T_CRIT}\n")
    print("COHORT RULES (basket every H sessions, held H sessions, no overlap)")
    for name, pick, h in rules(D):
        r = cohort_test(name, pick, h, D)
        if "alpha" not in r:
            print(f"  {name}: {r['verdict']}")
            continue
        print(f"  {name}  (hold {h}, {r['n']} baskets, ~{r['names']:.0f} names)\n"
              f"      beats all tradeable names, beta-adjusted: {r['alpha']:+7.1f} bp per basket  t {r['alpha_t']:+.2f}  halves {r['alpha_h'][0]:+.1f} / {r['alpha_h'][1]:+.1f}  (beta {r['beta']:.2f})\n"
              f"      raw difference {r['raw_lift']:+.1f} bp (name-weighted {r['name_weighted_lift']:+.1f});  2023+ raw difference {r['late_raw_lift']:+.1f} bp on {r['late_n']} baskets\n"
              f"      alpha vs SPY after cost {r['spy_alpha']:+.1f} bp  t {r['spy_alpha_t']:+.2f}  (beta {r['spy_beta']:.2f})\n"
              f"      yearly after cost {100 * r['cagr']:+.1f}% (at 30 bp {100 * r['cagr_stress']:+.1f}%)  all names {100 * r['uni_cagr']:+.1f}%  SPY {100 * r['spy_cagr']:+.1f}%  worst drawdown {100 * r['maxdd']:.0f}%\n"
              f"      VERDICT: {r['verdict']}", flush=True)
    print("\nRIDE RULE 6: buy the first new 60-day closing high in 20 sessions, above the 200-day average; sell on the first close under the 50-day average")
    trades = ride_trades(D)
    r = ride_stats(trades, days)
    print(f"  {r['trades']} trades entered 2017-2022, median hold {r['hold_med']:.0f} sessions; per trade mean {r['gross']:+.0f} bp, median {r['median']:+.0f} bp, win {100 * r['win']:.0f}%; "
          f"top 1% of trades = {100 * r['top1pct_share']:.0f}% of the total")
    print(f"  per trade vs buying every tradeable name over the same days: {r['lift']:+.1f} bp  t {r['lift_t']:+.2f}  halves {r['lift_h'][0]:+.1f} / {r['lift_h'][1]:+.1f}; "
          f"beta-adjusted {r['resid']:+.1f} bp  t {r['resid_t']:+.2f}  (beta {r['beta']:.2f});  2023+ {r['late_lift']:+.1f} bp on {r['late_n']} trades")
    print(f"  VERDICT: {r['verdict']}")
    curves = [account(trades, D, s) for s in range(200)]
    so = D["spy_o"]
    spy = so[WARM + 1:WARM + 1 + len(curves[0])] / so[WARM]
    for lab, lo, hi in (("2017-2022", V0, V1), ("2023 to 10/2026", V1, "2099")):
        st = np.array([curve_stats(e, days, lo, hi) for e in curves])
        sp = curve_stats(spy, days, lo, hi)
        print(f"  10-position account, {lab}, 200 tie-break seeds: yearly median {100 * np.median(st[:, 0]):+.1f}% (range {100 * st[:, 0].min():+.1f}% to {100 * st[:, 0].max():+.1f}%), "
              f"worst drawdown median {100 * np.median(st[:, 1]):.0f}%, Sharpe median {np.median(st[:, 2]):.2f}  |  SPY {100 * sp[0]:+.1f}%/yr, drawdown {100 * sp[1]:.0f}%, Sharpe {sp[2]:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
