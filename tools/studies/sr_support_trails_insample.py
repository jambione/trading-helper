#!/usr/bin/env python3
"""IN-SAMPLE, INFO ONLY: addendum 1bd4659 trailing stops T1-T4 on the 9/11-10/02 SIP cache.

Same arms, same seed-37 same-hour random controls and same square arms as tools/studies/sr_support_arm.py
(reproduces its hold-to-15:55 support - random +29.4 / +20.7 bp). Cost 20 bp (that study's RT).
Already-mined data: NOT evidence. The forward test (prereg c1e9c30) is the evidence.
"""
from __future__ import annotations

import collections
import json
import os
import pickle
import random
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import sr_support_arm as M  # noqa: E402
from sr_support_arm import ND, is_sup, is_res, bkey, et_min, tstat, series, INF  # noqa: E402
from sr_support_hold_close_score import TRAILS, TRAIL_DESC, trail_exits, default_block_btm  # noqa: E402

COST = M.COST


def last_bar(nd, ie):
    last = None
    for j in range(ie, len(nd.rows)):
        if nd.rows[j][0] >= nd.day_end:
            break
        last = j
    return last


def support_arms_with_block(nd):
    """M.support_arms logic, also returning the touched block S."""
    used, last, out = set(), -INF, []
    for i in nd.window(9 * 60 + 40, 15 * 60 + 30):
        ch = nd.ch(i)
        touch = [b for b in ch if is_sup(b) and nd.l[i] <= b.top and nd.c[i] >= b.btm]
        new = [b for b in touch if bkey(b) not in used]
        for b in touch:
            used.add(bkey(b))
        if not new:
            continue
        S = max(new, key=lambda b: b.top)
        res = [b for b in ch if is_res(b)]
        if any(b.btm <= nd.c[i] <= b.top for b in res):
            continue
        ab = [b.btm for b in res if b.btm > nd.c[i]]
        if not ab or (min(ab) / nd.c[i] - 1) * 100 < 0.40:
            continue
        m = et_min(nd.closes[i])
        if m - last < 15:
            continue
        if nd.exits(i, stop_btm=S.btm) is None:
            continue
        last = m
        out.append((i, S.btm))
    return out


def ctrl_idx(nd, i, rng):
    hr = et_min(nd.closes[i]) // 60
    pool = [k for k in nd.window(9 * 60 + 40, 15 * 60 + 30) if k != i and et_min(nd.closes[k]) // 60 == hr]
    return rng.choice(pool) if pool else None


def outc(nd, i, block_btm):
    ie = i + 1
    lb = last_bar(nd, ie)
    entry = nd.rows[ie][1]
    d = trail_exits(nd.rows, ie, lb, block_btm if block_btm is not None else default_block_btm(nd, i, entry))
    d["hold"] = nd.rows[lb][4] / entry - 1
    return d


def main():
    R = json.load(open(os.path.join(M.IND, "rows.json")))
    cache = pickle.load(open(os.path.join(M.IND, "ext.pkl"), "rb"))
    cal = sorted({d for (_, d) in cache})
    prev_of = {d: cal[cal.index(d) - 1] if cal.index(d) else None for d in cal}
    e_nd = collections.defaultdict(list)
    for r in R["rows"]:
        if r["pop"] == "E":
            e_nd[(r["sym"], r["day"])].append(r)
    sdays = sorted({d for (_, d) in e_nd})
    half = {d: ("A" if k < len(sdays) // 2 else "B") for k, d in enumerate(sdays)}
    rng37, rng41 = random.Random(37), random.Random(41)
    sup, sq = [], []
    for key in sorted(e_nd):
        rows = series(cache, key[0], key[1], prev_of.get(key[1]))
        if len(rows) < 100:
            continue
        nd = ND(key[0], key[1], rows)
        for i, sb in support_arms_with_block(nd):
            k = ctrl_idx(nd, i, rng37)
            sup.append({"day": key[1], "o": outc(nd, i, sb), "c": outc(nd, k, None) if k is not None else None})
        for r in e_nd[key]:
            i = nd.idx_of_close(float(r["t"]))
            if i < 0 or abs(nd.closes[i] - float(r["t"])) > 1 or nd.exits(i) is None:
                continue
            sq.append({"day": key[1], "o": outc(nd, i, None)})
            ctrl_idx(nd, i, rng41)                       # keep the square-control draw sequence as in the study
    bp = lambda x: x * 1e4  # noqa: E731
    print("IN-SAMPLE (9/11-10/02 SIP cache), INFO ONLY, already mined - NOT evidence. Addendum 1bd4659 trails T1-T4.")
    print(f"halves: A {sdays[0]}..{sdays[len(sdays)//2-1]}, B {sdays[len(sdays)//2]}..{sdays[-1]}; cost {bp(COST):.0f} bp; "
          "give-back = mean(variant - hold to 15:55); bp per trade")
    for h in ("A", "B"):
        S = [r for r in sup if half[r["day"]] == h]
        P = [r for r in S if r["c"]]
        Q = [r for r in sq if half[r["day"]] == h]
        print(f"\n## half {h}")
        print("| exit | arm | n | gross | net | median | win% | give-back vs hold | support − random (t) |")
        print("|---|---|---|---|---|---|---|---|---|")
        for v in ("hold",) + TRAILS:
            lv = [r["o"][v] - r["c"][v] for r in P]
            m, t = tstat([bp(x) for x in lv], [r["day"] for r in P])
            for nm, xs, hold in (("support", [r["o"][v] for r in S], [r["o"]["hold"] for r in S]),
                                 ("random", [r["c"][v] for r in P], [r["c"]["hold"] for r in P]),
                                 ("square", [r["o"][v] for r in Q], [r["o"]["hold"] for r in Q])):
                g = statistics.fmean(xs)
                lab = "hold 15:55" if v == "hold" else f"{v} {TRAIL_DESC[v]}"
                extra = f"{m:+.1f} (t {t:.2f})" if nm == "support" else ""
                print(f"| {lab} | {nm} | {len(xs)} | {bp(g):+.1f} | {bp(g - COST):+.1f} | {bp(statistics.median(xs)):+.1f} | "
                      f"{100 * sum(x > 0 for x in xs) / len(xs):.1f} | {bp(g - statistics.fmean(hold)):+.1f} | {extra} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
