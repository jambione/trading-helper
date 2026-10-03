#!/usr/bin/env python3
"""market_temperature_study.py — pre-registered in docs/studies/market_temperature_prereg.json (5eb0044).

HOT vs COLD mornings by T20 = the number of US common stocks (prior close >= $1, prior-day dollar volume
>= $250k) gapping >= 20% at the open. Terciles fixed on the first half. Outcomes per day:
  desk  mean open->close of names with open >= $10, gap >= 3%, prior-day dollar volume >= $20M
  bob   mean open->close (and open->high) of names with gap >= 20%, open >= $1
Pass (a, the desk decision): desk HOT - COLD >= +15 bp, Welch t >= 2 in BOTH halves, and the HOT-day desk
mean >= +20 bp in both halves. (b) Bob: HOT - COLD >= +50 bp, t >= 2 in both halves.

USAGE (mini): .venv/bin/python tools/studies/market_temperature_study.py ai_reports/allsym/daily_2025-09-01_2026-10-02.pkl
"""
from __future__ import annotations

import json
import math
import os
import pickle
import statistics
import sys
from collections import defaultdict

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools"))
WARMUP = 21


def welch(a, b):
    if len(a) < 3 or len(b) < 3:
        return float("nan")
    return (statistics.mean(a) - statistics.mean(b)) / math.sqrt(
        statistics.variance(a) / len(a) + statistics.variance(b) / len(b))


def main():
    data = pickle.load(open(sys.argv[1], "rb"))
    days = sorted({r[0] for rows in data.values() for r in rows})[WARMUP:]
    dayset = set(days)
    per = defaultdict(lambda: {"T20": 0, "T50": 0, "T100": 0, "desk": [], "bob": [], "bob_hi": []})
    for sym, rows in data.items():
        for (d0, o0, h0, l0, c0, v0), (d, o, h, l, c, v) in zip(rows, rows[1:]):
            if d not in dayset:
                continue
            if c0 < 1 or c0 * v0 < 250e3 or o <= 0:
                continue
            g = o / c0 - 1
            P = per[d]
            for G, k in ((0.2, "T20"), (0.5, "T50"), (1.0, "T100")):
                if g >= G:
                    P[k] += 1
            oc = (c / o - 1) * 1e4
            if g >= 0.2 and o >= 1:
                P["bob"].append(oc)
                P["bob_hi"].append((h / o - 1) * 1e4)
            if o >= 10 and g >= 0.03 and c0 * v0 >= 20e6:
                P["desk"].append(oc)
    days = [d for d in days if d in per]
    half = len(days) // 2
    H = {"first": days[:half], "second": days[half:]}
    t20_first = sorted(per[d]["T20"] for d in H["first"])
    lo = t20_first[int(len(t20_first) / 3)]
    hi = t20_first[int(2 * len(t20_first) / 3)]

    def temp(d):
        t = per[d]["T20"]
        return "HOT" if t >= hi and t > lo else ("COLD" if t <= lo else "MID")

    print(f"days {len(days)} ({days[0]}..{days[-1]}); halves split at {H['second'][0]}")
    print(f"T20 first-half median {statistics.median(t20_first):.0f}; cutoffs COLD <= {lo}, HOT >= {hi} "
          f"(HOT also requires > COLD cutoff)\n")
    res = {}
    for hname, ds in H.items():
        print(f"== {hname} half ({ds[0]}..{ds[-1]})")
        print(f"  {'temp':<5}{'days':>5}{'T20 med':>8}{'T100 med':>9}{'desk o->c (n names)':>22}{'bob o->c':>10}{'bob o->hi':>10}")
        for tp in ("HOT", "MID", "COLD"):
            sel = [d for d in ds if temp(d) == tp]
            dm = [statistics.mean(per[d]["desk"]) for d in sel if per[d]["desk"]]
            bm = [statistics.mean(per[d]["bob"]) for d in sel if per[d]["bob"]]
            bh = [statistics.mean(per[d]["bob_hi"]) for d in sel if per[d]["bob_hi"]]
            nn = sum(len(per[d]["desk"]) for d in sel)
            res[(hname, tp)] = (dm, bm)
            f = lambda x: f"{statistics.mean(x):+.0f}" if x else "—"
            print(f"  {tp:<5}{len(sel):>5}{statistics.median([per[d]['T20'] for d in sel]) if sel else 0:>8.0f}"
                  f"{statistics.median([per[d]['T100'] for d in sel]) if sel else 0:>9.0f}"
                  f"{f(dm):>14} ({nn:>5}){f(bm):>10}{f(bh):>10}")
        dh, bh_ = res[(hname, "HOT")]
        dc, bc = res[(hname, "COLD")]
        if dh and dc:
            print(f"  desk HOT - COLD {statistics.mean(dh) - statistics.mean(dc):+.0f} bp (Welch t {welch(dh, dc):+.2f}); "
                  f"HOT-day desk mean {statistics.mean(dh):+.0f} bp")
        if bh_ and bc:
            print(f"  bob  HOT - COLD {statistics.mean(bh_) - statistics.mean(bc):+.0f} bp (Welch t {welch(bh_, bc):+.2f})")
        print()

    def passes(kind, need_diff, need_hot=None):
        ok = True
        for hname in H:
            a = res[(hname, "HOT")][0 if kind == "desk" else 1]
            b = res[(hname, "COLD")][0 if kind == "desk" else 1]
            if not a or not b:
                return False
            ok &= (statistics.mean(a) - statistics.mean(b) >= need_diff and welch(a, b) >= 2)
            if need_hot is not None:
                ok &= statistics.mean(a) >= need_hot
        return ok
    print(f"PRE-REGISTERED VERDICT  (a) desk: {'PASS' if passes('desk', 15, 20) else 'FAIL'}   "
          f"(b) bob: {'PASS' if passes('bob', 50) else 'FAIL'}")

    # info: the desk's own days
    path = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")
    if os.path.exists(path):
        import bars  # noqa: E402
        by = defaultdict(list)
        for line in open(path):
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("entry_time") and r.get("exit_price") and r.get("entry_price") and not (r.get("hold_days") or 0):
                d = bars.day_of(float(r["entry_time"]))
                if d >= "2026-09-16" and float(r["entry_price"]) >= 10:
                    by[d].append((float(r["exit_price"]) / float(r["entry_price"]) - 1) * 1e4)
        print("\n  info — desk days since 9/16 (>= $10 fills): day, T20, temp, trades, mean P&L bp")
        for d in sorted(by):
            if d in per:
                print(f"    {d}  {per[d]['T20']:>4}  {temp(d):<5}{len(by[d]):>4}  {statistics.mean(by[d]):+7.1f}")


if __name__ == "__main__":
    main()
