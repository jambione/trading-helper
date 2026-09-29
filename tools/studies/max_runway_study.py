#!/usr/bin/env python3
"""max_runway_study.py — how much runway did our names offer, and what known
early predicts the names that offered the most?

A ceiling study: it asks what was there to take, not what the desk took.

Population: every name a source nominated on a recorded day (9/16-9/25), SIP
1m bars from source_study_bars.pkl. Entry minutes run from the later of the
nomination and 09:40 through 15:30. Before nomination we could not have
traded the name.

Per entry minute i, from that minute's close:
  run(i)    highest high within 60 min reached BEFORE a low 1% under the entry.
            A 1% stop would have survived to see it. This is a takeable run.
  free(i)   highest high from i to the close, no stop. The pure ceiling.
Per name-day:
  best      max run(i)          the best takeable 60-min run of the day
  ceiling   max free(i)         the best move from any entry to a later high
  m1, m2    share of entry minutes with run(i) >= 1% / >= 2%
            (was runway a lucky minute or a long window?)
Features, fixed at the name-day's first entry minute (known then):
  gap, pm_range, day_chg, first15 (if past 09:45), vol_pace, vol_1m, price,
  room_hod, range_pos, mins (minutes after the open), source
Each feature is split in terciles. It counts if the top-vs-bottom difference
in m2 has the same sign in both date halves and |z| >= 2 on name-days.

USAGE (on the mini; no new bar fetches, daily bars come from the Stage A cache)
  .venv/bin/python tools/studies/max_runway_study.py
"""
from __future__ import annotations

import math
import os
import pickle
import statistics
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import combined_score_study as cs  # noqa: E402
import morning_funnel as mf  # noqa: E402


def nominations():
    """(sym, day) -> (first nomination ts, source)."""
    import gzip
    import json
    out = {}
    base = os.path.join(ROOT, "ai_reports", "admit_ledger")
    for f in sorted(os.listdir(base)):
        day = f[:-6]
        for line in open(os.path.join(base, f)):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("stage") == "seed" and r.get("source") in cs.SOURCES:
                k = (str(r["symbol"]).upper(), day)
                t = float(r["ts"])
                if k not in out or t < out[k][0]:
                    out[k] = (t, r["source"])
    sess = os.path.join(ROOT, "ai_reports", "sessions")
    for day in os.listdir(sess):
        p = os.path.join(sess, day, "sources.jsonl.gz")
        if not os.path.exists(p):
            continue
        for line in gzip.open(p, "rt"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            src = "research" if r.get("source") in ("agy", "xai") else r.get("source")
            if r.get("event") == "enter" and src in cs.SOURCES:
                k = (str(r["symbol"]).upper(), day)
                t = float(r["ts"])
                if k not in out or t < out[k][0]:
                    out[k] = (t, src)
    return out


def name_day(sym, day, B, prev_close, daily, t_nom):
    t, o, h, l, c, v = B
    n = len(c)
    rth = [i for i in range(n) if 570 <= cs.et_min(t[i]) < 960]
    if len(rth) < 200 or not prev_close:
        return None
    i0 = rth[0]
    ents = [i for i in rth if 580 <= cs.et_min(t[i]) <= 930 and t[i] >= t_nom - 60 and i - i0 >= 15]
    if len(ents) < 10:
        return None
    runs, frees, drops = [], [], []
    # suffix max of highs for the no-stop ceiling
    suf = [0.0] * (n + 1)
    last_rth = rth[-1]
    for k in range(last_rth, -1, -1):
        suf[k] = max(h[k], suf[k + 1])
    for i in ents:
        px = c[i]
        stop = px * 0.99
        best = px
        for k in range(i + 1, min(n, i + 61)):
            if t[k] - t[i] > 60 * 60 or k > last_rth:
                break
            if l[k] <= stop:
                break
            best = max(best, h[k])
        runs.append((best / px - 1) * 100)
        # mirror: lowest low within 60 min before a high 1% over the entry
        worst = px
        for k in range(i + 1, min(n, i + 61)):
            if t[k] - t[i] > 60 * 60 or k > last_rth or h[k] >= px * 1.01:
                break
            worst = min(worst, l[k])
        drops.append((1 - worst / px) * 100)
        frees.append((suf[i + 1] / px - 1) * 100 if i + 1 <= last_rth else 0.0)
    e = ents[0]
    prior = [x for x in daily if x[0] < day][-20:]
    avg_vol = statistics.mean(x[2] for x in prior) if len(prior) >= 10 else None
    m = cs.et_min(t[e]) + 1
    pm = list(range(i0))
    hi = max(h[i0:e + 1])
    lo = min(l[i0:e + 1])
    rets = [(c[j] / c[j - 1] - 1) * 100 for j in range(e - 14, e + 1)]
    i15 = next((i for i in rth if cs.et_min(t[i]) >= 585), None)
    f = {
        "gap": (o[i0] / prev_close - 1) * 100,
        "pm_range": ((max(h[j] for j in pm) / min(l[j] for j in pm) - 1) * 100) if pm else None,
        "day_chg": (c[e] / prev_close - 1) * 100,
        "first15": (c[i15] / o[i0] - 1) * 100 if i15 is not None and i15 <= e else None,
        "vol_pace": (sum(v[i0:e + 1]) / (avg_vol * mf.expected_fraction(m - 570)))
        if avg_vol else None,
        "vol_1m": statistics.stdev(rets),
        "price": c[e],
        "room_hod": (1 - c[e] / hi) * 100,
        "range_pos": 100 * (c[e] - lo) / (hi - lo) if hi > lo else 50.0,
        "mins": m - 570,
    }
    return {
        "sym": sym, "day": day, "f": f,
        "best": max(runs), "ceiling": max(frees),
        "m1": sum(r >= 1 for r in runs) / len(runs),
        "m2": sum(r >= 2 for r in runs) / len(runs),
        "d2": sum(d >= 2 for d in drops) / len(drops),
        "n_min": len(runs),
    }


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def main():
    cache = pickle.load(open(cs.BARS, "rb"))
    days = sorted({d for _s, d in cache})
    daily = cs.daily_bars(cs.client(), sorted({s for s, _d in cache}), days[-1])
    nom = nominations()
    rows = []
    for (sym, day), rec in cache.items():
        if not rec or not rec[0] or (sym, day) not in nom:
            continue
        r = name_day(sym, day, rec[0], rec[1], daily.get(sym, []), nom[(sym, day)][0])
        if r:
            r["f"]["src"] = nom[(sym, day)][1]
            rows.append(r)
    h1 = set(days[:len(days) // 2])
    print(f"MAX RUNWAY STUDY {days[0]}..{days[-1]}: {len(rows)} nominated name-days, "
          f"{sum(r['n_min'] for r in rows)} entry minutes\n")

    print("WHAT WAS THERE (per name-day, after nomination)")
    for key, lab in (("best", "best takeable 60m run (1% stop survives)"),
                     ("ceiling", "ceiling: any entry to a later high, no stop")):
        xs = [r[key] for r in rows]
        print(f"  {lab}")
        print(f"    median {q(xs,.5):.2f}%  p25 {q(xs,.25):.2f}  p75 {q(xs,.75):.2f}  "
              f"p90 {q(xs,.9):.2f}  |  >=1% {sum(x>=1 for x in xs)/len(xs):.0%}  "
              f">=2% {sum(x>=2 for x in xs)/len(xs):.0%}  >=5% {sum(x>=5 for x in xs)/len(xs):.0%}")
    allm = sum(r["n_min"] for r in rows)
    print(f"  entry minutes with a takeable run >=1%: "
          f"{sum(r['m1']*r['n_min'] for r in rows)/allm:.1%}   >=2%: "
          f"{sum(r['m2']*r['n_min'] for r in rows)/allm:.1%}")
    top = sorted(rows, key=lambda r: -r["m2"] * r["n_min"])
    k = sum(r["m2"] * r["n_min"] for r in rows)
    acc, share10 = 0.0, None
    for j, r in enumerate(top, 1):
        acc += r["m2"] * r["n_min"]
        if j == max(1, len(top) // 10):
            share10 = acc / k if k else float("nan")
    print(f"  the top 10% of name-days hold {share10:.0%} of all >=2% runway minutes\n")

    print("BY SOURCE")
    bys = defaultdict(list)
    for r in rows:
        bys[r["f"]["src"]].append(r)
    for s, g in sorted(bys.items()):
        print(f"  {s:<10} n={len(g):<4} best med {q([r['best'] for r in g],.5):.2f}%  "
              f"best>=2% {sum(r['best']>=2 for r in g)/len(g):.0%}  "
              f"runway-min>=2% {statistics.mean(r['m2'] for r in g):.1%}")
    print()

    print("WHAT PREDICTS IT (terciles at the first entry minute; m2 = share of minutes with a >=2% run)")
    print(f"  {'feature':<10}{'n':>5}  {'m2 lo':>7}{'m2 hi':>7}{'z':>6}  {'best lo':>8}{'best hi':>8}"
          f"  {'H1 lo/hi':>13}  {'H2 lo/hi':>13}  cuts")
    for fn in ("gap", "pm_range", "day_chg", "first15", "vol_pace", "vol_1m", "price",
               "room_hod", "range_pos", "mins"):
        pts = [(r["f"][fn], r) for r in rows if r["f"].get(fn) is not None]
        if len(pts) < 60:
            continue
        xs = sorted(x for x, _ in pts)
        a, b = xs[len(xs) // 3], xs[2 * len(xs) // 3]
        lo = [r for x, r in pts if x <= a]
        hi = [r for x, r in pts if x >= b]
        ml, mh = statistics.mean(r["m2"] for r in lo), statistics.mean(r["m2"] for r in hi)
        sd = statistics.pstdev([r["m2"] for _, r in pts]) or 1e-9
        z = (mh - ml) / (sd * math.sqrt(1 / len(lo) + 1 / len(hi)))

        def half(g, inh1):
            v = [r["m2"] for r in g if (r["day"] in h1) == inh1]
            return statistics.mean(v) if v else float("nan")
        l1, hh1, l2, hh2 = half(lo, True), half(hi, True), half(lo, False), half(hi, False)
        ok = abs(z) >= 2 and (hh1 > l1) == (hh2 > l2) == (mh > ml)
        print(f"  {fn:<10}{len(pts):>5}  {ml:>7.1%}{mh:>7.1%}{z:>+6.1f}  "
              f"{q([r['best'] for r in lo],.5):>7.2f}%{q([r['best'] for r in hi],.5):>7.2f}%"
              f"  {l1:>6.1%}/{hh1:<6.1%}  {l2:>6.1%}/{hh2:<6.1%}  {a:.3g}|{b:.3g}"
              f"{'  HOLDS' if ok else ''}")

    print("\nUP vs DOWN (m2 = >=2% run before -1%; d2 = >=2% fall before +1%). Edge = m2 - d2.")
    allr = rows
    print(f"  all name-days: m2 {statistics.mean(r['m2'] for r in allr):.1%}  "
          f"d2 {statistics.mean(r['d2'] for r in allr):.1%}")
    for fn in ("gap", "pm_range", "day_chg", "first15", "vol_pace", "vol_1m", "price", "room_hod",
               "range_pos"):
        pts = [(r["f"][fn], r) for r in rows if r["f"].get(fn) is not None]
        xs = sorted(x for x, _ in pts)
        a, b = xs[len(xs) // 3], xs[2 * len(xs) // 3]
        out = []
        for lab, g in (("lo", [r for x, r in pts if x <= a]), ("hi", [r for x, r in pts if x >= b])):
            mu, md = statistics.mean(r["m2"] for r in g), statistics.mean(r["d2"] for r in g)
            out.append(f"{lab}: up {mu:5.1%} dn {md:5.1%} edge {mu - md:+5.1%}")
        print(f"  {fn:<10} " + "   ".join(out))

    print("\nINSIDE vol_1m TERCILES (does the feature find runway beyond jumpiness?) edge = m2 - d2")
    vs = sorted(r["f"]["vol_1m"] for r in rows)
    va, vb = vs[len(vs) // 3], vs[2 * len(vs) // 3]
    bands = (("calm", lambda x: x <= va), ("mid", lambda x: va < x < vb), ("jumpy", lambda x: x >= vb))
    for fn in ("gap", "pm_range", "day_chg", "first15", "vol_pace", "price", "room_hod", "range_pos"):
        cells = []
        for lab, inb in bands:
            g = [r for r in rows if inb(r["f"]["vol_1m"]) and r["f"].get(fn) is not None]
            xs = sorted(r["f"][fn] for r in g)
            a, b = xs[len(xs) // 3], xs[2 * len(xs) // 3]
            lo = [r for r in g if r["f"][fn] <= a]
            hi = [r for r in g if r["f"][fn] >= b]
            e = lambda G: statistics.mean(r["m2"] - r["d2"] for r in G)
            m = lambda G: statistics.mean(r["m2"] for r in G)
            cells.append(f"{lab} m2 {m(lo):4.1%}->{m(hi):4.1%} edge {e(lo):+5.1%}->{e(hi):+5.1%}")
        print(f"  {fn:<10} " + " | ".join(cells))

    print("\nTOP 15 NAME-DAYS BY RUNWAY MINUTES (>=2%)")
    for r in sorted(rows, key=lambda r: -r["m2"])[:15]:
        f = r["f"]
        print(f"  {r['day']} {r['sym']:<5} m2 {r['m2']:.0%}  best {r['best']:.1f}%  ceiling "
              f"{r['ceiling']:.1f}%  | gap {f['gap']:+.1f} day_chg {f['day_chg']:+.1f} "
              f"pace {f['vol_pace'] if f['vol_pace'] is None else round(f['vol_pace'],1)} "
              f"vol_1m {f['vol_1m']:.2f} ${f['price']:.0f} {f['src']}")


if __name__ == "__main__":
    main()
