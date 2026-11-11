#!/usr/bin/env python3
"""daily_horizon.py - does a days-to-weeks horizon carry an edge? (docs/studies/daily_horizon_prereg.json, operator 11/11)

Data: ai_reports/gapdown/daily.pkl - fully adjusted (open, close) for every name that was ever liquid 2016-2026
(close >= $10 and 20-day mean dollar volume >= $20M on the prior session), delisted names included, plus SPY.
A name is tradeable on day d only if d is in its liquid list, which is computed from sessions before d.

Every rule decides at the CLOSE of day t, buys the OPEN of t+1 and sells the OPEN of t+1+H. A name that stops trading
before the exit is sold at its last close. Two kinds of test:

  COHORT rules (1-5): every H sessions form the basket, hold H sessions, no overlap. Judged on basket return minus the
  equal-weight return of every tradeable name over the same window (lift), on alpha against SPY, and after cost.

  RIDE rule (6): buy a new 60-day closing high above the 200-day average, hold until the first close under the 50-day
  average. Judged per trade against the equal-weight universe over the same days, and as a 10-position account.

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
FAMILY = 6
T_CRIT = 2.64                             # two-sided 5% over 6 hypotheses
SPLIT = "2021-04-01"
WARM = 260


def load(path=None):
    d = pickle.load(open(path or ROOT / "ai_reports" / "gapdown" / "daily.pkl", "rb"))
    days = sorted(d["adj"]["SPY"])
    ix = {x: i for i, x in enumerate(days)}
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
    return days, syms, O, C, L, spy[:, 0], spy[:, 1]


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


def window_ret(O, Cf, t, h):
    """Return in bp for buying the open of t+1 and selling the open of t+1+h; a name gone by then is sold at its last close."""
    e = O[t + 1]
    x = O[t + 1 + h] if t + 1 + h < O.shape[0] else np.full(O.shape[1], np.nan)
    x = np.where(np.isnan(x), Cf[min(t + h, O.shape[0] - 1)], x)
    with np.errstate(all="ignore"):
        return (x / e - 1.0) * 1e4


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


def cohort_test(name, pick, h, D):
    """pick(t) -> bool mask of names chosen at the close of t (subset of tradeable). Non-overlapping cohorts every h sessions."""
    days, O, Cf, L, so = D["days"], D["O"], D["Cf"], D["L"], D["spy_o"]
    rows = []
    for t in range(WARM, len(days) - h - 2, h):
        uni = L[t + 1] & ~np.isnan(O[t + 1]) & ~np.isnan(D["C"][t])
        sel = pick(t) & uni
        r = window_ret(O, Cf, t, h)
        if sel.sum() < 5 or uni.sum() < 100:
            continue
        rows.append((days[t + 1], float(np.nanmean(r[sel])), float(np.nanmean(r[uni])), float((so[t + 1 + h] / so[t + 1] - 1) * 1e4), int(sel.sum())))
    if len(rows) < 20:
        return {"name": name, "h": h, "n": len(rows), "verdict": "TOO FEW COHORTS"}
    d = np.array([x[0] for x in rows])
    b, u, s = (np.array([x[k] for x in rows]) for k in (1, 2, 3))
    lift = b - u
    h1, h2 = d < SPLIT, d >= SPLIT
    a, at, beta = alpha_t(b - COST, s)
    per_year = 252.0 / h
    eq = np.cumprod(1 + (b - COST) / 1e4)
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    res = {"name": name, "h": h, "n": len(rows), "names": float(np.mean([x[4] for x in rows])),
           "lift": float(lift.mean()), "lift_t": tstat(lift), "lift_h": (float(lift[h1].mean()), float(lift[h2].mean())),
           "net_lift_h": (float(lift[h1].mean() - COST), float(lift[h2].mean() - COST)),
           "alpha": a, "alpha_t": at, "beta": beta,
           "cagr": float(eq[-1] ** (per_year / len(eq)) - 1), "cagr_stress": float(np.prod(1 + (b - STRESS) / 1e4) ** (per_year / len(b)) - 1),
           "spy_cagr": float(np.prod(1 + s / 1e4) ** (per_year / len(s)) - 1), "uni_cagr": float(np.prod(1 + u / 1e4) ** (per_year / len(u)) - 1),
           "maxdd": dd}
    pos = lambda v: v == v and v > 0
    edge = pos(res["lift_h"][0]) and pos(res["lift_h"][1]) and res["lift_t"] >= T_CRIT
    money = edge and pos(res["net_lift_h"][0]) and pos(res["net_lift_h"][1]) and res["alpha_t"] >= 2.0 and pos(res["alpha"])
    res["verdict"] = "PASS" if money else "EDGE only (lift, not money)" if edge else "FAIL"
    return res


def ride_test(D, seed=7):
    days, O, C, Cf, L, so = D["days"], D["O"], D["C"], D["Cf"], D["L"], D["spy_o"]
    T, N = C.shape
    hi60, ma50, ma200 = roll0(Cf, 60, np.nanmax), roll0(Cf, 50, np.nanmean), roll0(Cf, 200, np.nanmean)
    with np.errstate(all="ignore"):
        at_high = (C >= hi60 - 1e-9) & (C > ma200)
        below50 = C < ma50
    recent = roll0(at_high.astype(float), 20, np.nanmax)                 # a new high within the last 20 sessions, incl. today
    fresh = at_high.copy()
    fresh[1:] &= ~(recent[:-1] >= 1)                                     # first new high in 20 sessions
    # equal-weight universe open-to-open index (the control)
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        oo = np.full((T, N), np.nan)
        oo[:-1] = O[1:] / O[:-1] - 1
        ew = np.array([np.nanmean(np.where(L[t] & ~np.isnan(oo[t]), np.clip(oo[t], -0.5, 0.5), np.nan)) if L[t].sum() > 100 else 0.0 for t in range(T)])
    ewi = np.concatenate([[1.0], np.cumprod(1 + np.nan_to_num(ew))])     # ewi[t] = index at the open of day t
    trades = []
    for j in range(N):
        t = WARM
        col_f, col_b = fresh[:, j], below50[:, j]
        while t < T - 2:
            if col_f[t] and L[t + 1, j] and not np.isnan(O[t + 1, j]):
                s = t + 1
                while s < T - 2 and not col_b[s] and not np.isnan(C[s, j]) and s - t < 250:
                    s += 1
                x = O[s + 1, j] if not np.isnan(O[s + 1, j]) else Cf[s, j]
                r = (x / O[t + 1, j] - 1) * 1e4
                c = (ewi[s + 1] / ewi[t + 1] - 1) * 1e4
                sp = (so[s + 1] / so[t + 1] - 1) * 1e4
                trades.append((t + 1, s + 1, j, float(r), float(c), float(sp)))
                t = s + 1
            else:
                t += 1
    tr = np.array([(a, b, r, c, sp) for a, b, _j, r, c, sp in trades])
    ent = np.array([days[int(a)] for a in tr[:, 0]])
    lift = np.clip(tr[:, 2], -9000, 30000) - tr[:, 3]
    qtr = np.array([x[:4] + "Q" + str((int(x[5:7]) - 1) // 3 + 1) for x in ent])
    qmeans = np.array([lift[qtr == q].mean() for q in sorted(set(qtr))])
    h1, h2 = ent < SPLIT, ent >= SPLIT
    hold = tr[:, 1] - tr[:, 0]
    out = {"trades": len(tr), "hold_med": float(np.median(hold)), "gross": float(tr[:, 2].mean()), "median": float(np.median(tr[:, 2])),
           "win": float(np.mean(tr[:, 2] > COST)), "lift": float(lift.mean()), "lift_t_quarters": tstat(qmeans),
           "lift_h": (float(lift[h1].mean()), float(lift[h2].mean())), "vs_spy": float((tr[:, 2] - tr[:, 4]).mean()),
           "top1pct_share": float(np.sort(tr[:, 2])[-max(1, len(tr) // 100):].sum() / tr[:, 2].sum())}
    # 10-position account: equal 10% slots, signals in date order (same-day ties shuffled with a fixed seed), cost per side COST/2
    rng = np.random.default_rng(seed)
    by_entry: dict[int, list] = {}
    for a, b, j, r, _c, _sp in trades:
        by_entry.setdefault(a, []).append((b, j))
    eq, cash, open_pos, curve = 1.0, 1.0, {}, []
    for t in range(WARM, T - 1):
        for j in [j for j, (b, _v) in open_pos.items() if b == t]:        # exits at this open
            b, v = open_pos.pop(j)
            cash += v * (1 - COST / 2e4)
        cands = by_entry.get(t, [])
        rng.shuffle(cands)
        for b, j in cands:
            if len(open_pos) >= 10 or j in open_pos:
                continue
            stake = min(cash, eq / 10)
            if stake <= 0:
                break
            cash -= stake
            open_pos[j] = (b, stake * (1 - COST / 2e4))
        for j, (b, v) in list(open_pos.items()):                          # mark open-to-open
            g = O[t + 1, j] / O[t, j] if not (np.isnan(O[t + 1, j]) or np.isnan(O[t, j])) else 1.0
            open_pos[j] = (b, v * g)
        eq = cash + sum(v for _b, v in open_pos.values())
        curve.append((days[t], eq, len(open_pos)))
    e = np.array([x[1] for x in curve])
    yrs = len(e) / 252.0
    sp = so[WARM + 1:WARM + 1 + len(e)] / so[WARM]
    ewc = ewi[WARM + 1:WARM + 1 + len(e)] / ewi[WARM]
    dr = np.diff(np.log(e))
    mid = next(i for i, x in enumerate(curve) if x[0] >= SPLIT)
    out["account"] = {"cagr": float(e[-1] ** (1 / yrs) - 1), "maxdd": float((e / np.maximum.accumulate(e) - 1).min()),
                      "sharpe": float(dr.mean() / dr.std() * math.sqrt(252)), "avg_open": float(np.mean([x[2] for x in curve])),
                      "spy_cagr": float(sp[-1] ** (1 / yrs) - 1), "spy_maxdd": float((sp / np.maximum.accumulate(sp) - 1).min()),
                      "ew_cagr": float(ewc[-1] ** (1 / yrs) - 1),
                      "cagr_h": (float((e[mid] / 1.0) ** (252 / mid) - 1), float((e[-1] / e[mid]) ** (252 / (len(e) - mid)) - 1)),
                      "spy_cagr_h": (float(sp[mid] ** (252 / mid) - 1), float((sp[-1] / sp[mid]) ** (252 / (len(e) - mid)) - 1))}
    pos = lambda v: v == v and v > 0
    edge = pos(out["lift_h"][0]) and pos(out["lift_h"][1]) and out["lift_t_quarters"] >= T_CRIT
    out["verdict"] = "PASS" if edge and out["lift"] > COST else "EDGE only" if edge else "FAIL"
    return out


def rules(D):
    C, Cf = D["C"], D["Cf"]
    hi252 = roll0(Cf, 252, np.nanmax)
    hi60 = roll0(Cf, 60, np.nanmax)
    ma200 = roll0(Cf, 200, np.nanmean)

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
        return D["L"][t + 1] & ~np.isnan(C[t])
    return [
        ("1 momentum 6-1 months, top tenth", lambda t: decile(past(t, 126, 21), uni(t)), 21),
        ("2 nearest the 52-week high, top tenth", lambda t: decile(Cf[t] / hi252[t], uni(t)), 21),
        ("3 new 60-day high above 200-day avg", lambda t: uni(t) & (C[t] >= hi60[t] - 1e-9) & (C[t] > ma200[t]), 10),
        ("4 one-week losers, bottom tenth", lambda t: decile(past(t, 5, 0), uni(t), top=False), 5),
        ("5 one-month momentum, top tenth", lambda t: decile(past(t, 21, 0), uni(t)), 21),
    ]


def main() -> int:
    days, syms, O, C, L, so, sc = load()
    D = {"days": days, "O": O, "C": C, "Cf": ffill0(C), "L": L, "spy_o": so}
    print(f"{len(syms)} names ever liquid, {days[0]}..{days[-1]} ({len(days)} sessions); tradeable names per day: median {int(np.median(L.sum(1)[WARM:]))}")
    print(f"cost {COST:g} bp per round trip (stress {STRESS:g}); halves split at {SPLIT}; t needed {T_CRIT} ({FAMILY} hypotheses)\n")
    print("COHORT RULES (basket formed every H sessions, held H sessions, no overlap)")
    for name, pick, h in rules(D):
        r = cohort_test(name, pick, h, D)
        if "lift" not in r:
            print(f"  {name}: {r['verdict']}")
            continue
        print(f"  {name}  (hold {h} sessions, {r['n']} baskets, ~{r['names']:.0f} names)\n"
              f"      lift vs all tradeable names {r['lift']:+7.1f} bp per basket  t {r['lift_t']:+.2f}  halves {r['lift_h'][0]:+.1f} / {r['lift_h'][1]:+.1f}\n"
              f"      alpha vs SPY after cost {r['alpha']:+7.1f} bp per basket  t {r['alpha_t']:+.2f}  beta {r['beta']:.2f}\n"
              f"      yearly return after cost {100 * r['cagr']:+.1f}% (stress {100 * r['cagr_stress']:+.1f}%)  all names {100 * r['uni_cagr']:+.1f}%  SPY {100 * r['spy_cagr']:+.1f}%  "
              f"worst drawdown {100 * r['maxdd']:.0f}%\n      VERDICT: {r['verdict']}")
    print("\nRIDE RULE 6: buy a new 60-day closing high above the 200-day average (first in 20 sessions); sell on the first close under the 50-day average")
    r = ride_test(D)
    a = r["account"]
    print(f"  {r['trades']} trades, median hold {r['hold_med']:.0f} sessions; per trade: mean {r['gross']:+.0f} bp, median {r['median']:+.0f} bp, win {100 * r['win']:.0f}%; "
          f"top 1% of trades = {100 * r['top1pct_share']:.0f}% of the total")
    print(f"  per trade vs all tradeable names over the same days: {r['lift']:+.1f} bp  t (by entry quarter) {r['lift_t_quarters']:+.2f}  halves {r['lift_h'][0]:+.1f} / {r['lift_h'][1]:+.1f};  vs SPY {r['vs_spy']:+.1f} bp")
    print(f"  10-position account after cost: {100 * a['cagr']:+.1f}%/yr (halves {100 * a['cagr_h'][0]:+.1f}% / {100 * a['cagr_h'][1]:+.1f}%), worst drawdown {100 * a['maxdd']:.0f}%, "
          f"Sharpe {a['sharpe']:.2f}, average {a['avg_open']:.1f} positions open")
    print(f"  same period: SPY {100 * a['spy_cagr']:+.1f}%/yr (halves {100 * a['spy_cagr_h'][0]:+.1f}% / {100 * a['spy_cagr_h'][1]:+.1f}%), worst drawdown {100 * a['spy_maxdd']:.0f}%; all tradeable names equal-weight {100 * a['ew_cagr']:+.1f}%/yr")
    print(f"  VERDICT: {r['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
