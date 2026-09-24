#!/usr/bin/env python3
"""Vol-scaled trail under the book: does findable runway become profit?

Gates as deployed (name-day SIP spread <= 0.20%, gap-down > 1% blocked), book
rules as live (5 slots, one per symbol, 120s cooldown, 3R brake, flat 15:50),
cost = the name-day median SIP spread, 2026-09-14..23, halves 14-17 | 18-23.

  S1        one arm, all names, trail 0.35%              (deployed)
  S1-vK     one arm, all names, trail max(0.35, K x vol_1m)
  S7        one arm, rvol_pace >= 1.64 only, trail 0.35%
  S7-vK     same with the vol-scaled trail
vol_1m = stdev of the 15 one-minute returns before entry, %.
"""
from __future__ import annotations

import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import counterfactual_today as cf  # noqa: E402
import daily_filter_screen as dfs  # noqa: E402
import entry_screen as es  # noqa: E402
import name_rank_study as nr  # noqa: E402
import runway_target_study as rt  # noqa: E402

DAYS = rt.DAYS
H1 = rt.H1
KS = (4, 5, 6)


def walk_eod(B, i0, px, give, arm=0.3, seed=1.0):
    t, o, h, l, c, _v = B
    stop = px * (1 - seed / 100)
    hi, armed, last = px, False, i0
    for k in range(i0 + 1, len(t)):
        if bars.et_minutes(t[k]) >= cf.EOD_MIN:
            return (o[k] / px - 1) * 100, k
        last = k
        if l[k] <= stop:
            return (min(stop, o[k]) / px - 1) * 100, k
        hi = max(hi, h[k])
        if not armed and hi >= px * (1 + arm / 100):
            armed = True
        if armed:
            stop = max(stop, hi * (1 - give / 100))
    return (c[last] / px - 1) * 100, last


def book(events, cache, cost):
    """events: sorted (ts, sym, i, give)."""
    open_until, cool_until, trades, realized = {}, {}, [], 0.0
    for ts, sym, i, give in events:
        if realized <= -cf.BRAKE_USD:
            break
        if open_until.get(sym, 0) > ts or cool_until.get(sym, 0) > ts:
            continue
        if sum(1 for v in open_until.values() if v > ts) >= 5:
            continue
        B = cache[sym]
        ret, j = walk_eod(B, i, B[4][i], give)
        net = ret - cost[(sym, i)]
        usd = cf.NOTIONAL * net / 100
        realized += usd
        open_until[sym], cool_until[sym] = B[0][j], B[0][j] + 120
        trades.append({"ts": ts, "ret": ret, "net": net, "usd": usd, "cost": cost[(sym, i)]})
    return trades


def main():
    from config import load_config
    import morning_funnel as mf
    cfg = load_config()
    first: dict = {}
    want = es.admitted(DAYS, first)
    syms_all = sorted({s for v in want.values() for s in v})
    ext = es.fetch_ext(want)
    dfeat = dfs.daily_features(set(syms_all))
    av = rt.avg_volume(set(syms_all))
    cl = bars.client()
    strat = ["S1"] + [f"S1-v{k}" for k in KS] + ["S7"] + [f"S7-v{k}" for k in KS]
    per_day = {}
    for d in DAYS:
        spy = ext.get(("SPY", d))
        cache, cost = {}, {}
        ev = {k: [] for k in strat}
        for s in sorted(want[d]):
            df = ext.get((s, d))
            f0d = dfeat.get((s, d)) or {}
            if df is None or len(df) < 150 or f0d.get("gap") is None or f0d["gap"] < -1.0:
                continue
            sp = nr.name_day_spread(cl, s, d)
            time.sleep(0.2)
            if sp is None or sp > 0.20:
                continue
            indi = es.indicators(df, cfg, spy)
            B = es.to_B(df)
            t, o, h, l, c, v = B
            cache[s] = B
            t_on = first.get((s, d))
            f0 = indi["first"]
            avgv = av.get((s, d))
            for i in range(f0 + 16, len(t) - 1):
                m = indi["mins"][i]
                if m < 9 * 60 + 40 or m > 15 * 60 + 30 or c[i] < 20:
                    continue
                if t_on is None or t[i] < t_on:
                    continue
                if "mid_rise" not in es.fire(indi, i):
                    continue
                k = i - 1
                vol = statistics.stdev([(c[j] / c[j - 1] - 1) * 100 for j in range(k - 14, k + 1)])
                rp = None
                if avgv:
                    frac = mf.expected_fraction(m - 570)
                    if frac > 0:
                        rp = sum(v[f0:k + 1]) / (avgv * frac)
                cost[(s, i)] = sp
                gives = {"": 0.35, **{f"-v{kk}": max(0.35, kk * vol) for kk in KS}}
                for suf, g in gives.items():
                    ev["S1" + suf].append((t[i], s, i, g))
                    if rp is not None and rp >= 1.64:
                        ev["S7" + suf].append((t[i], s, i, g))
        per_day[d] = (cache, {k: sorted(x) for k, x in ev.items()}, cost)
        print(f"  {d}: {len(cache)} names", file=sys.stderr)

    print(f"VOL-TRAIL BOOK STUDY {DAYS[0]}..{DAYS[-1]}  (gates on, true name-day spreads, "
          f"book rules, flat 15:50; halves {DAYS[0]}..{DAYS[3]} | {DAYS[4]}..{DAYS[-1]})\n")
    print(f"  {'strategy':<10}{'trades':>7}{'win':>6}{'gross':>9}{'cost':>8}{'net':>9}{'±':>7}"
          f"{'P/L':>10}{'h1 net':>9}{'h2 net':>9}  per-day P/L")
    for k in strat:
        trades, daypl, h1, h2 = [], [], [], []
        for d in DAYS:
            cache, ev, cost = per_day[d]
            tr = book(ev[k], cache, cost)
            trades += tr
            daypl.append(sum(x["usd"] for x in tr))
            (h1 if d in H1 else h2).extend(x["net"] for x in tr)
        if not trades:
            print(f"  {k:<10} no trades")
            continue
        net = [x["net"] for x in trades]
        print(f"  {k:<10}{len(trades):>7}{sum(x > 0 for x in net) / len(net):>6.0%}"
              f"{statistics.mean(x['ret'] for x in trades):>+8.3f}%"
              f"{statistics.mean(x['cost'] for x in trades):>7.3f}%{statistics.mean(net):>+8.3f}%"
              f"{statistics.stdev(net) / len(net) ** .5:>7.3f}{sum(daypl):>+10.2f}"
              f"{(statistics.mean(h1) if h1 else float('nan')):>+9.3f}"
              f"{(statistics.mean(h2) if h2 else float('nan')):>+9.3f}  "
              + " ".join(f"{x:+.0f}" for x in daypl))


if __name__ == "__main__":
    main()
