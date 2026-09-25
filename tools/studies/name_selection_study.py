#!/usr/bin/env python3
"""Can choosing the right seed names lift the long win rate — after the spread?

The operator's thesis (2026-09-25): the edge is in WHICH seed names we open.
Room below the day's high already splits the -50 cross runner rate 36% -> 60%
(BOLLINGER_STUDY_2026-09-25). This stacks it with the other name facts known
at the cross and asks, on held-out days, whether the best names win more AND
make money after the spread, and how many opens the selection gives up.

Population: bollinger_cross_study.build_events (every -50 cross with the slow
line rising on the runway bar cache, RTH 09:35-15:30; same labels: runner =
>=0.35R or >=0.6% within 60m before -1R; ret30/ret60 close-to-close).

Conditions — directions fixed from prior studies and the TRAIN half only:
  A room      dist_hod_pct <= train bottom-tercile cut (>= ~2.7% below HOD)
  B not_top   position in the prior 30-min range < 90
  C green     day change > 0 (gap-down/red names fade: daily-context study)
  D volume    rvol_pace >= 1.0 (SIP volume so far vs own 20-day normal)
  E early     within 120 min of the open (runway study: runway decays)
Score = number met. Net return = ret - cost by price tier (spread estimate:
$20+: 0.10%, $10-20: 0.20%, $5-10: 0.40%, <$5: 1.00%).

Usage (on the mini)::

    .venv/bin/python tools/studies/name_selection_study.py [--min-price 20 --max-price 100]
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import statistics
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bollinger_cross_study as bcs  # noqa: E402

mr, dcf = bcs.mr, bcs.dcf


def cost(px: float) -> float:
    return 0.10 if px >= 20 else 0.20 if px >= 10 else 0.40 if px >= 5 else 1.00


def rng30(B, ts):
    t, o, h, l, c, v = B
    i = next((k for k, x in enumerate(t) if x == ts), None)
    if i is None or i < 5:
        return None
    lo_i = max(0, i - 30)
    hi, lo = max(h[lo_i:i + 1]), min(l[lo_i:i + 1])
    return (c[i] - lo) / (hi - lo) * 100 if hi > lo else None


def stats(rows):
    if not rows:
        return None
    n = len(rows)
    run = sum(1 for r in rows if r["runner"]) / n
    r30 = statistics.mean(r["net30"] for r in rows)
    r60 = statistics.mean(r["net60"] for r in rows)
    sd = defaultdict(list)
    for r in rows:
        sd[(r["symbol"], r["day"])].append(r)
    sd_run = statistics.mean(sum(x["runner"] for x in v) / len(v) for v in sd.values())
    sd_60 = statistics.mean(statistics.mean(x["net60"] for x in v) for v in sd.values())
    se60 = statistics.pstdev([r["net60"] for r in rows]) / n ** 0.5 if n > 1 else 0
    return {"n": n, "symdays": len(sd), "runner": run, "net30": r30, "net60": r60,
            "t60": r60 / se60 if se60 else 0, "sd_runner": sd_run, "sd_net60": sd_60}


def fmt(label, s, base_n=None):
    if not s:
        return f"  {label:28s} n=0"
    keep = f"  keeps {s['n'] / base_n:4.0%}" if base_n else ""
    return (f"  {label:28s} n={s['n']:4d} ({s['symdays']:3d} sym-days)  runner {s['runner']:4.0%} "
            f"(per sym-day {s['sd_runner']:4.0%})  net30 {s['net30']:+.3f}%  net60 {s['net60']:+.3f}% "
            f"(t {s['t60']:+.1f}; per sym-day {s['sd_net60']:+.3f}%){keep}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-price", type=float, default=20.0)
    ap.add_argument("--max-price", type=float, default=100.0)
    ap.add_argument("--avgvol-cache", default="/tmp/bb_avgvol.json")
    ap.add_argument("--fetch-daily", action="store_true")
    a = ap.parse_args()

    cache_path = dcf._first_path(dcf.CACHE_CANDIDATES)
    cache = pickle.load(open(cache_path, "rb"))
    supply_path = dcf._first_path(dcf.SUPPLY_CANDIDATES)
    supply = json.load(open(supply_path)) if supply_path else {}
    stops = mr.load_stop_map()
    events, _skipped = bcs.build_events(cache, supply, stops, a.min_price, a.max_price)
    days = sorted({e["day"] for e in events})
    train = {d for d in days if d <= bcs.TRAIN_END}
    test = {d for d in days if d >= bcs.TEST_START}

    av = {}
    if os.path.exists(a.avgvol_cache):
        av = {tuple(k.split("|")): v for k, v in json.load(open(a.avgvol_cache)).items()}
    if a.fetch_daily and not av:
        av = dcf.avg_daily_volume({e["symbol"] for e in events}, days, sleep_s=1.0)
        json.dump({f"{s}|{d}": v for (s, d), v in av.items()}, open(a.avgvol_cache, "w"))

    rows = []
    for e in events:
        if e.get("ret_30") is None or e.get("ret_60") is None:
            continue
        B = cache[(e["symbol"], e["day"])]
        avg = av.get((e["symbol"], e["day"]))
        fr = dcf.expected_fraction(e["mins_open"]) if avg else 0
        e["rvol_pace"] = e["vol_so_far"] / (avg * fr) if avg and fr > 0 else None
        e["rng30"] = rng30(B, e["ts"])
        c = cost(e["price"])
        e["net30"], e["net60"] = e["ret_30"] - c, e["ret_60"] - c
        rows.append(e)

    tr_hod = sorted(r["dist_hod_pct"] for r in rows if r["day"] in train and r["dist_hod_pct"] is not None)
    cut = tr_hod[len(tr_hod) // 3] if tr_hod else -2.7
    conds = {
        "A room": lambda r: r["dist_hod_pct"] is not None and r["dist_hod_pct"] <= cut,
        "B not_top": lambda r: r["rng30"] is not None and r["rng30"] < 90,
        "C green": lambda r: r["day_chg_pct"] is not None and r["day_chg_pct"] > 0,
        "D volume": lambda r: r.get("rvol_pace") is not None and r["rvol_pace"] >= 1.0,
        "E early": lambda r: r["mins_open"] <= 120,
    }
    for r in rows:
        r["score"] = sum(1 for f in conds.values() if f(r))

    print(f"name selection: {len(rows)} crosses, ${a.min_price:g}-${a.max_price:g}, "
          f"days {days[0]}..{days[-1]} (train <= {bcs.TRAIN_END}, test >= {bcs.TEST_START}); "
          f"room cut {cut:.2f}%; rvol_pace known {sum(1 for r in rows if r.get('rvol_pace') is not None)}")
    for half, D in (("TRAIN", train), ("TEST (held out)", test)):
        H = [r for r in rows if r["day"] in D]
        print(f"\n=== {half}: {len(H)} crosses ===")
        print(fmt("all crosses", stats(H)))
        for name, f in conds.items():
            print(fmt(f"  {name}", stats([r for r in H if f(r)]), len(H)))
        print("  by score (conditions met):")
        for k in range(0, 6):
            print(fmt(f"  score = {k}", stats([r for r in H if r["score"] == k]), len(H)))
        for k in (3, 4):
            print(fmt(f"  score >= {k}", stats([r for r in H if r["score"] >= k]), len(H)))
    print("\nper day, score >= 3 vs all (net60 per sym-day, runner):")
    for d in days:
        Dall = [r for r in rows if r["day"] == d]
        Dtop = [r for r in Dall if r["score"] >= 3]
        s_all, s_top = stats(Dall), stats(Dtop)
        if s_all and s_top:
            print(f"  {d} {'test ' if d in test else 'train'}  all {s_all['sd_net60']:+.3f}% {s_all['runner']:4.0%} "
                  f"(n{s_all['n']})   top {s_top['sd_net60']:+.3f}% {s_top['runner']:4.0%} (n{s_top['n']})")


if __name__ == "__main__":
    main()
