#!/usr/bin/env python3
"""needle_study.py — which measurable things move the needle on names and entries?

One study, one sample, one honest test for every candidate indicator, instead
of a dozen separate studies on different samples. For each name-day in
ai_reports/source_study_bars.pkl (SIP 1m bars from 04:00, 8 days, ~1,800
name-days, the desk's nominated and refused names), $20-$100, it samples every
5th minute from 09:45 to 15:15 ET and records features known at that minute's
close (no look-ahead) and what happened next:

  success   +1% before -1% within 60 min (lows first)
  fwd30     close 30 min later, bp gross; the round-trip spread is ~8-16 bp

Features
  name/day   gap (open vs prior close), day_chg (vs prior close), price,
             pm_range (premarket high-low %), pm_vol_share (premarket volume /
             prior-RTH-so-far volume proxy), first15 (09:30-09:45 move)
  location   room_hod (% under the running session high), range_pos (0-100 in
             today's range), vwap_dist (% from session VWAP)
  momentum   ret15, ret60 (%), fast_r, slow_r (live %R: 21 EWM 7 / 112 EWM 3)
  activity   vol_accel (last-15-min volume / session per-minute mean),
             vol15 (stdev of 1m returns, last 15 min), minutes since open

Test: quintile edges are fitted on the first half of the days and applied to
the second half. A feature moves the needle only if its best quintile beats
its worst in BOTH halves. The best two-feature cell (5x5 grid) is chosen on
half 1 and scored on half 2 only.
"""
from __future__ import annotations

import math
import os
import pickle
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import mid_rise_runway_study as mr  # noqa: E402

ET = ZoneInfo("America/New_York")
CACHE = os.path.join(ROOT, "ai_reports", "source_study_bars.pkl")
COST_BP = 10.0
FEATURES = ("gap", "day_chg", "price", "pm_range", "first15", "room_hod", "range_pos",
            "vwap_dist", "ret15", "ret60", "fast_r", "slow_r", "vol_accel", "vol15", "mins")


def et_min(ts):
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def rows_for(B, prev_close, day):
    t, o, h, l, c, v = B
    n = len(c)
    rth = [i for i in range(n) if 570 <= et_min(t[i]) < 960]
    if len(rth) < 200 or not prev_close:
        return []
    i0 = rth[0]
    pm = [i for i in range(i0)]
    pm_range = ((max(h[j] for j in pm) / min(l[j] for j in pm) - 1) * 100) if pm else None
    open_px = o[i0]
    gap = (open_px / prev_close - 1) * 100
    i15 = next((i for i in rth if et_min(t[i]) >= 585), None)
    first15 = (c[i15] / open_px - 1) * 100 if i15 is not None else None
    fast = mr.percent_r_series(h, l, c, 21, 7.0)
    slow = mr.percent_r_series(h, l, c, 112, 3.0)
    out = []
    hi = lo = None
    pv = vv = 0.0
    cumv = 0.0
    for k, i in enumerate(rth):
        hi = h[i] if hi is None else max(hi, h[i])
        lo = l[i] if lo is None else min(lo, l[i])
        tp = (h[i] + l[i] + c[i]) / 3
        pv += tp * v[i]
        vv += v[i]
        cumv += v[i]
        m = et_min(t[i]) + 1
        if not (585 <= m <= 915) or k % 5 or not (20 <= c[i] <= 100) or i < 60:
            continue
        px = c[i]
        r15 = [(c[j] / c[j - 1] - 1) * 100 for j in range(i - 14, i + 1) if c[j - 1] > 0]
        last15v = sum(v[i - 14:i + 1])
        per_min = cumv / (k + 1)
        f = {
            "gap": gap, "day_chg": (px / prev_close - 1) * 100, "price": px,
            "pm_range": pm_range, "first15": first15,
            "room_hod": (1 - px / hi) * 100, "range_pos": 100 * (px - lo) / (hi - lo) if hi > lo else None,
            "vwap_dist": (px / (pv / vv) - 1) * 100 if vv > 0 else None,
            "ret15": (px / c[i - 15] - 1) * 100, "ret60": (px / c[i - 60] - 1) * 100,
            "fast_r": fast[i], "slow_r": slow[i],
            "vol_accel": (last15v / 15) / per_min if per_min > 0 else None,
            "vol15": statistics.pstdev(r15) if len(r15) > 2 else None,
            "mins": m - 570,
        }
        # outcomes
        succ = 0
        for j in range(i + 1, min(n, i + 61)):
            if l[j] <= px * 0.99:
                break
            if h[j] >= px * 1.01:
                succ = 1
                break
        j30 = i + 30
        fwd30 = (c[j30] / px - 1) * 1e4 if j30 < n and t[j30] - t[i] <= 45 * 60 else None
        if fwd30 is None:
            continue
        out.append({"day": day, **f, "succ": succ, "fwd30": fwd30})
    return out


def quintile_edges(xs):
    xs = sorted(xs)
    return [xs[int(len(xs) * q / 5)] for q in (1, 2, 3, 4)]


def bucket(x, edges):
    return sum(1 for e in edges if x >= e)


def cell_stats(rs):
    if not rs:
        return None
    return (len(rs), statistics.mean(r["succ"] for r in rs), statistics.mean(r["fwd30"] for r in rs))


def main():
    cache = pickle.load(open(CACHE, "rb"))
    rows = []
    for (sym, day), rec in cache.items():
        if rec and rec[0]:
            rows += rows_for(rec[0], rec[1], day)
    days = sorted({r["day"] for r in rows})
    h1d = set(days[: len(days) // 2])
    H1 = [r for r in rows if r["day"] in h1d]
    H2 = [r for r in rows if r["day"] not in h1d]
    base = cell_stats(rows)
    print(f"{len(rows)} sampled minutes; {len(days)} days (H1 {days[0]}..{days[len(days)//2-1]}, "
          f"H2 ..{days[-1]}); baseline success {base[1]:.1%}, fwd30 {base[2]:+.1f} bp gross\n")
    print(f"{'feature':10} {'H1 best q':>9} {'H1 succ':>8} {'H2 succ':>8} {'H2 fwd30':>9} "
          f"{'H2 worst q':>10} {'H2 succ':>8} {'H2 fwd30':>9} {'holds':>6}")
    edges = {}
    ranked = []
    for f in FEATURES:
        x1 = [r[f] for r in H1 if r[f] is not None]
        if len(x1) < 500:
            continue
        e = quintile_edges(x1)
        edges[f] = e
        q1 = defaultdict(list)
        q2 = defaultdict(list)
        for r in H1:
            if r[f] is not None:
                q1[bucket(r[f], e)].append(r)
        for r in H2:
            if r[f] is not None:
                q2[bucket(r[f], e)].append(r)
        s1 = {q: cell_stats(v) for q, v in q1.items()}
        best = max(s1, key=lambda q: s1[q][2])
        worst = min(s1, key=lambda q: s1[q][2])
        b2, w2 = cell_stats(q2[best]), cell_stats(q2[worst])
        holds = b2 and w2 and b2[2] > w2[2] and s1[best][2] > s1[worst][2]
        ranked.append((b2[2] - w2[2] if holds else -99, f))
        print(f"{f:10} {best:9d} {s1[best][1]:8.1%} {b2[1]:8.1%} {b2[2]:+9.1f} "
              f"{worst:10d} {w2[1]:8.1%} {w2[2]:+9.1f} {'yes' if holds else 'no':>6}")
    # best two-feature cell, chosen on H1 (min 150 samples), scored on H2
    good = [f for _, f in sorted(ranked, reverse=True) if _ > -99][:8]
    best_cells = []
    for a in range(len(good)):
        for b in range(a + 1, len(good)):
            fa, fb = good[a], good[b]
            g1 = defaultdict(list)
            for r in H1:
                if r[fa] is not None and r[fb] is not None:
                    g1[(bucket(r[fa], edges[fa]), bucket(r[fb], edges[fb]))].append(r)
            for cell, rs in g1.items():
                if len(rs) >= 150:
                    best_cells.append((statistics.mean(x["fwd30"] for x in rs), fa, fb, cell, len(rs),
                                       statistics.mean(x["succ"] for x in rs)))
    best_cells.sort(reverse=True)
    print("\nTop two-feature cells chosen on H1, scored on H2 (q0 = lowest fifth, q4 = highest):")
    print(f"{'features':24} {'cell':>7} {'H1 n':>5} {'H1 succ':>8} {'H1 fwd30':>9} {'H2 n':>5} "
          f"{'H2 succ':>8} {'H2 fwd30':>9} {'H2 net':>7}")
    for m1, fa, fb, cell, n1, s1_ in best_cells[:8]:
        rs = [r for r in H2 if r[fa] is not None and r[fb] is not None
              and (bucket(r[fa], edges[fa]), bucket(r[fb], edges[fb])) == cell]
        st = cell_stats(rs)
        if not st:
            continue
        print(f"{fa + ' x ' + fb:24} {str(cell):>7} {n1:5d} {s1_:8.1%} {m1:+9.1f} {st[0]:5d} "
              f"{st[1]:8.1%} {st[2]:+9.1f} {st[2] - COST_BP:+7.1f}")
    print("\nquintile edges (H1):")
    for f, e in edges.items():
        print(f"  {f:10} {[round(x, 2) for x in e]}")


if __name__ == "__main__":
    main()
