#!/usr/bin/env python3
"""Can volatility-scaled exits turn findable runway into profit?

runway_target_study.py: volume pace vs a stock's own normal (rvol_pace) finds
+2%-before--1% runway 4x more often, but a flat 0.35% trail keeps none of it.
Here the exit scales with the name's own 1-minute volatility (vol_1m, stdev
of the last 15 one-minute returns, %).

Entry sets (book names >= $20, after admission, 09:40-15:30, 09-14..23):
  A  one-arm events, all                              (tomorrow)
  B  one-arm events with rvol_pace >= 1.64            (high-runway names)
  C  every 5th minute with rvol_pace >= 1.64          (the names, any moment)
Exits (arm at +0.3% unless noted; flat at the horizon):
  live        seed 1%, trail 0.35% from peak, 60m
  trail kV    trail max(0.35%, k * vol_1m), k = 1..4
  seed+trail  seed max(1%, 4 * vol_1m) and trail max(0.35%, 2 * vol_1m)
  ... 120m    the best shapes with a 120-minute horizon
Net = exit return - the name-day's median SIP spread.
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
import entry_screen as es  # noqa: E402
import name_rank_study as nr  # noqa: E402
import runway_target_study as rt  # noqa: E402

DAYS = rt.DAYS
H1 = rt.H1


def walk(B, i0, px, seed, trail, arm=0.3, horizon=60):
    t, o, h, l, c, _v = B
    stop = px * (1 - seed / 100)
    hi, armed = px, False
    end = t[i0] + horizon * 60
    last = i0
    for k in range(i0 + 1, len(t)):
        if t[k] > end:
            break
        last = k
        if l[k] <= stop:
            return (min(stop, o[k]) / px - 1) * 100
        hi = max(hi, h[k])
        if not armed and hi >= px * (1 + arm / 100):
            armed = True
        if armed:
            stop = max(stop, hi * (1 - trail / 100))
    return (c[last] / px - 1) * 100


def main():
    from config import load_config
    import morning_funnel as mf
    cfg = load_config()
    first: dict = {}
    want = es.admitted(DAYS, first)
    syms_all = sorted({s for v in want.values() for s in v})
    ext = es.fetch_ext(want)
    av = rt.avg_volume(set(syms_all))
    cl = bars.client()
    sets = {"A": [], "B": [], "C": []}
    for d in DAYS:
        spy = ext.get(("SPY", d)) if ("SPY", d) in ext else None
        for s in sorted(want[d]):
            df = ext.get((s, d))
            if df is None or len(df) < 150:
                continue
            sp = nr.name_day_spread(cl, s, d)
            time.sleep(0.2)
            if sp is None:
                continue
            indi = es.indicators(df, cfg, spy)
            B = es.to_B(df)
            t, o, h, l, c, v = B
            t_on = first.get((s, d))
            f0 = indi["first"]
            avgv = av.get((s, d))
            last_mr = -1e18
            for i in range(f0 + 16, len(t) - 1):
                m = indi["mins"][i]
                if m < 9 * 60 + 40 or m > 15 * 60 + 30 or c[i] < 20:
                    continue
                if t_on is None or t[i] < t_on:
                    continue
                k = i - 1
                rets = [(c[j] / c[j - 1] - 1) * 100 for j in range(k - 14, k + 1)]
                vol = statistics.stdev(rets)
                rp = None
                if avgv:
                    frac = mf.expected_fraction(m - 570)
                    if frac > 0:
                        rp = sum(v[f0:k + 1]) / (avgv * frac)
                e = {"B": B, "i": i, "px": c[i], "vol": vol, "sp": sp, "day": d}
                fired = "mid_rise" in es.fire(indi, i) and t[i] - last_mr >= es.COOL
                if fired:
                    last_mr = t[i]
                    sets["A"].append(e)
                    if rp is not None and rp >= 1.64:
                        sets["B"].append(e)
                if i % 5 == 0 and rp is not None and rp >= 1.64:
                    sets["C"].append(e)
        print(f"  {d}: A {len(sets['A'])} B {len(sets['B'])} C {len(sets['C'])}", file=sys.stderr)

    shapes = [("live: seed 1%, trail 0.35%", lambda e: (1.0, 0.35, 60))]
    for k in (1, 2, 3, 4):
        shapes.append((f"trail max(.35, {k}xvol)", lambda e, k=k: (1.0, max(0.35, k * e["vol"]), 60)))
    shapes.append(("seed max(1,4xvol) + trail 2xvol", lambda e: (max(1.0, 4 * e["vol"]), max(0.35, 2 * e["vol"]), 60)))
    shapes.append(("seed max(1,5xvol) + trail 3xvol", lambda e: (max(1.0, 5 * e["vol"]), max(0.35, 3 * e["vol"]), 60)))
    shapes.append(("trail 2xvol, 120m", lambda e: (1.0, max(0.35, 2 * e["vol"]), 120)))
    shapes.append(("seed 4xvol + trail 2xvol, 120m", lambda e: (max(1.0, 4 * e["vol"]), max(0.35, 2 * e["vol"]), 120)))

    names = {"A": "A  one-arm, all (tomorrow)", "B": "B  one-arm, rvol_pace >= 1.64",
             "C": "C  any moment, rvol_pace >= 1.64"}
    print(f"VOL-SCALED EXIT STUDY {DAYS[0]}..{DAYS[-1]}  (net = exit - name-day SIP spread)\n")
    for key in ("A", "B", "C"):
        es_ = sets[key]
        if not es_:
            continue
        vols = sorted(e["vol"] for e in es_)
        print(f"== {names[key]}: {len(es_)} entries, median vol_1m {vols[len(vols) // 2]:.3f}%, "
              f"median spread {sorted(e['sp'] for e in es_)[len(es_) // 2]:.3f}% ==")
        print(f"  {'exit':<34}{'gross':>9}{'net':>9}{'±':>7}{'win':>6}{'avg win':>9}{'avg loss':>9}"
              f"{'h1 net':>9}{'h2 net':>9}")
        for lab, fn in shapes:
            g, n1, n2 = [], [], []
            for e in es_:
                seed, trail, hz = fn(e)
                r = walk(e["B"], e["i"], e["px"], seed, trail, horizon=hz)
                g.append(r)
                (n1 if e["day"] in H1 else n2).append(r - e["sp"])
            net = [x - e["sp"] for x, e in zip(g, es_)]
            w = [x for x in net if x > 0]
            lo = [x for x in net if x <= 0]
            print(f"  {lab:<34}{statistics.mean(g):>+8.3f}%{statistics.mean(net):>+8.3f}%"
                  f"{statistics.stdev(net) / len(net) ** .5:>7.3f}{len(w) / len(net):>6.0%}"
                  f"{(statistics.mean(w) if w else 0):>+8.2f}%{(statistics.mean(lo) if lo else 0):>+8.2f}%"
                  f"{(statistics.mean(n1) if n1 else float('nan')):>+9.3f}"
                  f"{(statistics.mean(n2) if n2 else float('nan')):>+9.3f}")
        print()


if __name__ == "__main__":
    main()
