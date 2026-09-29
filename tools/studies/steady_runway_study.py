#!/usr/bin/env python3
"""steady_runway_study.py — the operator's runway: from the entry, a steady
climb to +5% ($10 -> $10.50), not a spike and not a round trip.

"Steady" is scored on a grid, all measured from the entry price until the
target is touched (or the close, 15:59):
  dip   the price never trades more than dip% under the entry
  pull  the price never gives back more than pull% from its running high
A path counts only if it touches the target with both rules intact. Targets
are +5% (the ask), with +2% and +3% for scale. The mirror, a steady FALL of
the same size under the same rules, is scored too. A feature that raises
both only finds movement.

Populations, 9/16-9/25 (source_study_bars.pkl, SIP 1m):
  desk fills   outcomes.jsonl entries on those days, from the fill price
  every minute every nominated name, every minute from nomination (or
               09:40) to 15:30, from that minute's close
Features at the entry: day_chg, pm_range, vol_1m, vol_pace, price, room_hod,
range_pos, ret15, minutes after the open, source. Terciles, both halves.

USAGE (on the mini; no new bar fetches)
  .venv/bin/python tools/studies/steady_runway_study.py
"""
from __future__ import annotations

import json
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
import bars  # noqa: E402
import combined_score_study as cs  # noqa: E402
import max_runway_study as mx  # noqa: E402
import morning_funnel as mf  # noqa: E402

TARGETS = (2.0, 3.0, 5.0)
RULES = ((0.5, 1.0), (0.5, 2.0), (1.0, 1.0), (1.0, 2.0))   # (dip, pull)
MAIN = (5.0, 1.0, 2.0)                                      # target, dip, pull


def steady(B, i, px, last, *, up=True):
    """{(target, dip, pull): minutes to target or None}, one forward pass.

    Inside a bar the adverse extreme is charged before the favorable one, so
    a bar that dips and then tags the target counts as the dip first.
    """
    t, _o, h, l, _c, _v = B
    out = {}
    live = {(tg, d, p) for tg in TARGETS for d, p in RULES}
    best = px          # running extreme in the trade's favor
    worst_dip = 0.0    # deepest move against the entry, %
    worst_pull = 0.0   # deepest give-back from the running extreme, %
    for k in range(i + 1, last + 1):
        if not live:
            break
        if up:
            worst_dip = max(worst_dip, (1 - l[k] / px) * 100)
            worst_pull = max(worst_pull, (best - l[k]) / px * 100)
            gain = (h[k] / px - 1) * 100
        else:
            worst_dip = max(worst_dip, (h[k] / px - 1) * 100)
            worst_pull = max(worst_pull, (h[k] - best) / px * 100)
            gain = (1 - l[k] / px) * 100
        for key in list(live):
            tg, d, p = key
            if worst_dip > d or worst_pull > p:
                out[key] = None
                live.discard(key)
            elif gain >= tg:
                out[key] = round((t[k] - t[i]) / 60)
                live.discard(key)
        best = max(best, h[k]) if up else min(best, l[k])
    for key in live:
        out[key] = None
    return out


def feats(B, i, prev_close, daily, day, i0):
    t, _o, h, l, c, v = B
    m = cs.et_min(t[i]) + 1
    prior = [x for x in daily if x[0] < day][-20:]
    avg_vol = statistics.mean(x[2] for x in prior) if len(prior) >= 10 else None
    pm = list(range(i0))
    hi, lo = max(h[i0:i + 1]), min(l[i0:i + 1])
    rets = [(c[j] / c[j - 1] - 1) * 100 for j in range(i - 14, i + 1)]
    return {
        "day_chg": (c[i] / prev_close - 1) * 100,
        "pm_range": ((max(h[j] for j in pm) / min(l[j] for j in pm) - 1) * 100) if pm else None,
        "vol_1m": statistics.stdev(rets),
        "vol_pace": sum(v[i0:i + 1]) / (avg_vol * mf.expected_fraction(m - 570)) if avg_vol else None,
        "price": c[i],
        "room_hod": (1 - c[i] / hi) * 100,
        "range_pos": 100 * (c[i] - lo) / (hi - lo) if hi > lo else 50.0,
        "ret15": (c[i] / c[i - 15] - 1) * 100,
        "mins": m - 570,
    }


def rate(rows, key, side="up"):
    return statistics.mean(1.0 if r[side].get(key) is not None else 0.0 for r in rows)


def main():
    cache = pickle.load(open(cs.BARS, "rb"))
    days = sorted({d for _s, d in cache})
    daily = cs.daily_bars(cs.client(), sorted({s for s, _d in cache}), days[-1])
    nom = mx.nominations()
    h1 = set(days[:len(days) // 2])

    def rth_of(B):
        rth = [k for k in range(len(B[0])) if 570 <= cs.et_min(B[0][k]) < 960]
        return rth

    # desk fills
    fills = []
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        et, px, sym = r.get("entry_time"), r.get("entry_price"), str(r.get("symbol") or "")
        if not isinstance(et, (int, float)) or not px:
            continue
        day = bars.day_of(et)
        rec = cache.get((sym, day))
        if not rec or not rec[0]:
            continue
        B = rec[0]
        rth = rth_of(B)
        if len(rth) < 200:
            continue
        i = bars.index_at(B[0], et)
        if i < rth[0] + 15 or i > rth[-1] - 1:
            continue
        fills.append({"day": day, "sym": sym, "up": steady(B, i, float(px), rth[-1]),
                      "dn": steady(B, i, float(px), rth[-1], up=False),
                      "arm": r.get("arm_why"),
                      "f": feats(B, i, rec[1], daily.get(sym, []), day, rth[0])})

    # every minute after nomination
    mins = []
    for (sym, day), rec in cache.items():
        if not rec or not rec[0] or (sym, day) not in nom or not rec[1]:
            continue
        B = rec[0]
        rth = rth_of(B)
        if len(rth) < 200:
            continue
        t_nom, src = nom[(sym, day)]
        for i in rth:
            m = cs.et_min(B[0][i])
            if m < 580 or m > 930 or B[0][i] < t_nom - 60 or i < rth[0] + 15:
                continue
            f = feats(B, i, rec[1], daily.get(sym, []), day, rth[0])
            f["src"] = src
            mins.append({"day": day, "sym": sym, "t": B[0][i],
                         "up": steady(B, i, B[4][i], rth[-1]),
                         "dn": steady(B, i, B[4][i], rth[-1], up=False), "f": f})

    print(f"STEADY RUNWAY STUDY {days[0]}..{days[-1]}: {len(fills)} desk fills, "
          f"{len(mins)} name-minutes after nomination "
          f"({len({(r['sym'], r['day']) for r in mins})} name-days)\n")
    print("HOW OFTEN a steady climb (up) vs a steady fall (dn) of the target size happened")
    print(f"  {'target':>6} {'dip':>5} {'pull':>5} | {'fills up':>9} {'dn':>6} | "
          f"{'minutes up':>10} {'dn':>6} | name-days with >=1 up minute")
    nd = defaultdict(list)
    for r in mins:
        nd[(r["sym"], r["day"])].append(r)
    for tg in TARGETS:
        for d, p in RULES:
            key = (tg, d, p)
            has = sum(1 for g in nd.values() if any(r["up"][key] is not None for r in g))
            print(f"  {tg:>5}% {d:>4}% {p:>4}% | {rate(fills, key):>9.1%} {rate(fills, key, 'dn'):>6.1%} | "
                  f"{rate(mins, key):>10.2%} {rate(mins, key, 'dn'):>6.2%} | "
                  f"{has}/{len(nd)} ({has / len(nd):.0%})")
    key = MAIN
    times = [r["up"][key] for r in mins if r["up"][key] is not None]
    if times:
        print(f"\n  +5% steady (dip 1%, pull 2%): median {statistics.median(times)} min to target, "
              f"p25 {sorted(times)[len(times) // 4]}, p75 {sorted(times)[3 * len(times) // 4]}")

    print("\nDESK FILLS BY ARM (steady +2% / +3% / +5%, dip 1%, pull 2%)")
    by = defaultdict(list)
    for r in fills:
        by[str(r["arm"])].append(r)
    for a, g in sorted(by.items(), key=lambda kv: -len(kv[1]))[:10]:
        print(f"  {a:<24} n={len(g):<4} " + "  ".join(
            f"+{tg:g}% {rate(g, (tg, 1.0, 2.0)):.1%}" for tg in TARGETS))

    print("\nWHAT WAS KNOWN AT ENTRY (name-minutes; +5% steady, dip 1%, pull 2%). "
          "up/dn per tercile; HOLDS = up lifts more than dn, same sign both halves")
    for fn in ("day_chg", "pm_range", "vol_1m", "vol_pace", "price", "room_hod", "range_pos",
               "ret15", "mins"):
        pts = [(r["f"][fn], r) for r in mins if r["f"].get(fn) is not None]
        xs = sorted(x for x, _ in pts)
        a, b = xs[len(xs) // 3], xs[2 * len(xs) // 3]
        lo = [r for x, r in pts if x <= a]
        hi = [r for x, r in pts if x >= b]

        def edge(g, hs=None):
            g = [r for r in g if hs is None or (r["day"] in h1) == hs]
            return rate(g, key) - rate(g, key, "dn") if g else float("nan")
        ok = (edge(hi, True) > edge(lo, True)) == (edge(hi, False) > edge(lo, False)) \
            and abs(edge(hi) - edge(lo)) >= 0.002
        print(f"  {fn:<10} lo up {rate(lo, key):6.2%} dn {rate(lo, key, 'dn'):6.2%} | "
              f"hi up {rate(hi, key):6.2%} dn {rate(hi, key, 'dn'):6.2%} | "
              f"edge lo {edge(lo):+6.2%} hi {edge(hi):+6.2%}  cuts {a:.3g}|{b:.3g}"
              f"{'  HOLDS' if ok else ''}")
    bys = defaultdict(list)
    for r in mins:
        bys[r["f"]["src"]].append(r)
    for s, g in sorted(bys.items()):
        print(f"  src {s:<10} n={len(g):<7} up {rate(g, key):6.2%} dn {rate(g, key, 'dn'):6.2%}")

    print("\nTHE STEADY +5% NAME-DAYS (first qualifying minute; dip 1%, pull 2%)")
    seen = set()
    for r in sorted((r for r in mins if r["up"][key] is not None), key=lambda r: r["t"]):
        k = (r["sym"], r["day"])
        if k in seen:
            continue
        seen.add(k)
        f = r["f"]
        print(f"  {r['day']} {bars.et_minutes(r['t']) // 60:02d}:{bars.et_minutes(r['t']) % 60:02d} "
              f"{r['sym']:<5} {r['up'][key]:>3}m | day_chg {f['day_chg']:+5.1f} pm_rng {f['pm_range'] or 0:4.1f} "
              f"vol_1m {f['vol_1m']:.2f} pace {f['vol_pace'] or 0:4.1f} ${f['price']:<6.2f} "
              f"room {f['room_hod']:.1f} ret15 {f['ret15']:+.1f} {f['src']}")


if __name__ == "__main__":
    main()
