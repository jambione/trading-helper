#!/usr/bin/env python3
"""Squares after a dip into a zone below the book-entry price. Pre-registered: docs/studies/zone_square_prereg.json.

Reuses the indicator-levels study's rows (squares E, control minutes C) and its SIP bar cache.
Run on the mini after indicator_levels_study.py:
  .venv/bin/python tools/studies/zone_square_study.py
"""
from __future__ import annotations

import collections
import json
import math
import os
import pickle
import statistics
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not os.path.isdir(os.path.join(ROOT, "ai_reports")):
    ROOT = os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools"))
import bars  # noqa: E402

LV = os.path.join(ROOT, "ai_reports", "indicator_levels")
ZONES = (0.005, 0.010, 0.015)
COST = 0.0020


def book_entries(days: set[str]) -> dict[tuple[str, str], float]:
    first = {}
    for line in open(os.path.join(ROOT, "ai_reports", "events.jsonl")):
        if '"admit_funnel"' not in line:
            continue
        try:
            e = json.loads(line)
            ts = float(e["ts"])
        except Exception:  # noqa: BLE001
            continue
        d = bars.day_of(ts)
        if d not in days:
            continue
        for s in e.get("kept_symbols") or []:
            k = (str(s), d)
            if k not in first or ts < first[k]:
                first[k] = ts
    return first


def dct(per_day):
    allv = [x for v in per_day.values() for x in v]
    if not allv:
        return None, None, 0
    dm = [statistics.mean(v) for v in per_day.values() if v]
    t = (statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm)))
         if len(dm) > 2 and statistics.stdev(dm) > 0 else None)
    return statistics.mean(allv), t, len(allv)


def main():
    z = json.load(open(os.path.join(LV, "rows.json")))
    days = z["days"]
    half = {d: "AB"[k % 2] for k, d in enumerate(days)}
    cache = pickle.load(open(os.path.join(LV, "ext.pkl"), "rb"))
    entry_ts = book_entries(set(days))

    nd = {}                                       # (sym, day) -> dict(entry_ts, entry_px, touch_ts{z}, zone fill net15)
    for (s, d), ts0 in entry_ts.items():
        df = cache.get((s, d))
        if df is None or len(df) < 100:
            continue
        t = np.array([x.timestamp() for x in df.index])
        o, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "low", "close"))
        i0 = int(np.searchsorted(t + 60, ts0, side="right")) - 1
        if i0 < 0:
            continue
        px0 = float(c[i0])
        if px0 < 10:
            continue
        rec = {"ts0": ts0, "px0": px0, "touch": {}, "zfill": {}, "zfix": {}, "gap": {}}
        for zz in ZONES:
            lvl = px0 * (1 - zz)
            hit = np.nonzero((np.arange(len(t)) > i0) & (l <= lvl))[0]
            rec["touch"][zz] = float(t[hit[0]]) + 60 if len(hit) else None
            # ZONE_ONLY: first strictly-through touch inside 09:40-15:30, fill at the zone price
            m = [k for k in np.nonzero((np.arange(len(t)) > i0) & (l < lvl))[0]
                 if 9 * 60 + 40 <= bars.et_minutes(t[k]) <= 15 * 60 + 30]
            if m:
                k = m[0]
                y, mo, dd = map(int, d.split("-"))
                from datetime import datetime
                cap = datetime(y, mo, dd, 15, 55, tzinfo=bars.ET).timestamp()
                tx = min(float(t[k]) + 15 * 60, cap)
                j = int(np.searchsorted(t, tx, side="right")) - 1
                if j > k:
                    rec["zfill"][zz] = (float(t[k]) + 60, float(c[j]) / lvl - 1 - COST)
                    # ZONE_FIX (post-review, not pre-registered): a resting limit fills at the bar open when the bar
                    # gaps through the zone, and a name already through the zone before 09:40 would have filled
                    # outside the window, so it is skipped.
                    early = any(bars.et_minutes(t[q]) < 9 * 60 + 40
                                for q in np.nonzero((np.arange(len(t)) > i0) & (l < lvl))[0])
                    if not early:
                        fill = min(float(o[k]), lvl)
                        rec["zfix"][zz] = (float(t[k]) + 60, float(c[j]) / fill - 1 - COST)
                        rec["gap"][zz] = (o[k] < lvl, lvl / float(o[k]) - 1)
        nd[(s, d)] = rec

    rows = [r for r in z["rows"] if r["net15"] is not None and (r["sym"], r["day"]) in nd
            and r["t"] >= nd[(r["sym"], r["day"])]["ts0"]]
    ctrl = collections.defaultdict(list)
    for r in rows:
        if r["pop"] == "C":
            ctrl[(r["sym"], r["day"], bars.et_minutes(r["t"] - 60) // 60)].append(r["net15"])
    cm = {k: statistics.mean(v) for k, v in ctrl.items()}

    def lift(sym, day, t, net):
        b = cm.get((sym, day, bars.et_minutes(t - 60) // 60))
        return None if b is None else net - b

    sq = [r for r in rows if r["pop"] == "E"]
    print(f"name-days with a book entry: {len(nd)}; squares after entry: {len(sq)}; control minutes: "
          f"{sum(1 for r in rows if r['pop'] == 'C')}")
    for zz in ZONES:
        touched = sum(1 for v in nd.values() if v["touch"][zz])
        print(f"  zone -{zz:.1%}: touched on {touched}/{len(nd)} name-days ({touched / len(nd):.0%})")

    P = lambda x: "—" if x is None else f"{x * 1e4:+.1f}"
    T = lambda x: "—" if x is None else f"{x:+.1f}"
    print(f"\n{'arm':16} {'A lift bp (t, n)':>22} {'B lift bp (t, n)':>22} {'A net15':>8} {'B net15':>8}  pass")

    def report(name, items, compare=None):
        out = []
        for h in "AB":
            pl, pn = collections.defaultdict(list), collections.defaultdict(list)
            for day, lf, net in items:
                if half[day] == h:
                    pn[day].append(net)
                    if lf is not None:
                        pl[day].append(lf)
            out.append((dct(pl), dct(pn)))
        ok = all(m is not None and m >= 5e-4 and t is not None and t >= 2 and n >= 100 for (m, t, n), _ in out)
        if compare is not None:
            ok = ok and all(a[0][0] is not None and b[0][0] is not None and a[0][0] > b[0][0]
                            for a, b in zip(out, compare))
        (ma, ta, na), (mb, tb, nb) = out[0][0], out[1][0]
        print(f"{name:16} {P(ma):>8} ({T(ta)}, {na:>4}) {P(mb):>8} ({T(tb)}, {nb:>4}) {P(out[0][1][0]):>8} "
              f"{P(out[1][1][0]):>8}  {'PASS' if ok else ''}")
        return out

    report("SQ_ALL", [(r["day"], lift(r["sym"], r["day"], r["t"], r["net15"]), r["net15"]) for r in sq])
    for zz in ZONES:
        zone_items, nozone_items = [], []
        for r in sq:
            tt = nd[(r["sym"], r["day"])]["touch"][zz]
            item = (r["day"], lift(r["sym"], r["day"], r["t"], r["net15"]), r["net15"])
            (zone_items if tt is not None and tt <= r["t"] else nozone_items).append(item)
        nz = report(f"SQ_NOZONE {zz:.1%}", nozone_items)
        report(f"SQ_ZONE {zz:.1%}", zone_items, compare=nz)
        zo = [(d, lift(s, d, v["zfill"][zz][0], v["zfill"][zz][1]), v["zfill"][zz][1])
              for (s, d), v in nd.items() if zz in v["zfill"]]
        report(f"ZONE_ONLY {zz:.1%}", zo)
        zf = [(d, lift(s, d, v["zfix"][zz][0], v["zfix"][zz][1]), v["zfix"][zz][1])
              for (s, d), v in nd.items() if zz in v["zfix"]]
        report(f"ZONE_FIX {zz:.1%}", zf)
        g = [v["gap"][zz] for v in nd.values() if zz in v["gap"]]
        if g:
            gaps = [x for through, x in g if through]
            print(f"  ZONE_FIX {zz:.1%}: {len(g)} fills, {len(gaps)} ({len(gaps) / len(g):.0%}) gapped through the zone"
                  + (f", mean gap {statistics.mean(gaps) * 1e4:.1f} bp" if gaps else "")
                  + f"; {sum(1 for v in nd.values() if zz in v['zfill']) - len(g)} skipped (through before 09:40)")


if __name__ == "__main__":
    main()
