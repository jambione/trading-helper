#!/usr/bin/env python3
"""cost_gates_study.py — would a higher price floor, or a spread-vs-movement gate, cut the desk's spread cost
without cutting what the trades earn?

Round trips are built from the fill ledger exactly as edge_a_cost_decomp.py does (FIFO buys vs sells per
symbol, SIP NBBO at each submit; shares its cache ai_reports/edge_nbbo_cache.json). Per round trip, bp:
  net    sell fill / buy fill - 1                    what the desk earned
  move   SIP mid at sell submit / SIP mid at buy submit - 1
  cost   move - net                                  spread + slippage paid
  spr    full SIP spread at the buy, bp of mid
  vol    stdev of SIP 1m returns over the 15 min before the buy, bp (bars.realized_vol)
  ratio  spr / vol                                   spread in units of a typical minute's move

Cutoffs are read on the BEFORE window and checked, unchanged, on the AFTER window (the current gates).
t is clustered by day. Nothing here is a live change.

USAGE (mini, after hours):
  .venv/bin/python tools/studies/cost_gates_study.py --before 2026-08-01:2026-09-26 --after 2026-09-29:2026-10-02
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict, deque

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import exec_report as er  # noqa: E402

CACHE = os.path.join(ROOT, "ai_reports", "edge_nbbo_cache.json")


def round_trips(lo: str, hi: str, cache: dict, cl, want_vol: bool = True) -> list[dict]:
    days = sorted(f[:-6] for f in os.listdir(os.path.join(ROOT, "ai_reports", "fills"))
                  if f.endswith(".jsonl") and lo <= f[:-6] <= hi)
    rows = []
    for day in days:
        os_ = er.orders(day)
        for o in os_:
            if not o["t_sub"]:
                continue
            if o["oid"] not in cache:
                q = er.nbbo_at(cl, o["sym"], o["t_sub"])
                cache[o["oid"]] = list(q) if q else None
                time.sleep(0.3)   # the live engine shares these data keys
            q = cache[o["oid"]]
            o["bid"], o["ask"] = q if q else (None, None)
        json.dump(cache, open(CACHE, "w"))
        book = defaultdict(deque)
        for o in os_:
            qty = float(o["qty"] or 0)
            if qty <= 0:
                continue
            if o["side"] == "buy":
                book[o["sym"]].append([o, qty])
                continue
            left = qty
            while left > 1e-9 and book[o["sym"]]:
                b, bq = book[o["sym"]][0]
                q = min(bq, left)
                if b.get("bid") and o.get("bid") and b["ask"] > 0 and o["ask"] > 0:
                    mb, ms = (b["bid"] + b["ask"]) / 2, (o["bid"] + o["ask"]) / 2
                    net = o["px"] / b["px"] - 1
                    mv = ms / mb - 1
                    rows.append({"day": day, "sym": b["sym"], "px": b["px"], "t": b["t_sub"].timestamp(),
                                 "hold": (o["t_sub"] - b["t_sub"]).total_seconds(),
                                 "net": net * 1e4, "move": mv * 1e4, "cost": (mv - net) * 1e4,
                                 "spr": (b["ask"] - b["bid"]) / mb * 1e4})
                left -= q
                book[o["sym"]][0][1] -= q
                if book[o["sym"]][0][1] <= 1e-9:
                    book[o["sym"]].popleft()
    for r in rows:
        if not want_vol:
            r["vol"] = r["ratio"] = None
            continue
        st, cls = bars.fetch(r["sym"], r["day"], "sip")
        v = bars.realized_vol(st, cls, r["t"] - 60) if st else None   # bars completed before the buy
        r["vol"] = v * 100 if v else None                              # % -> bp
        r["ratio"] = r["spr"] / r["vol"] if r["vol"] else None
    return rows


def stats(xs, k="net"):
    by = defaultdict(list)
    for x in xs:
        by[x["day"]].append(x[k])
    dm = [statistics.mean(v) for v in by.values()]
    t = (statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm)))
         if len(dm) > 2 and statistics.stdev(dm) > 0 else None)
    return statistics.mean(x[k] for x in xs), t, len(by)


def line(label, xs, ndays):
    if not xs:
        print(f"  {label:<24}{0:>5}")
        return
    n, tn, _ = stats(xs, "net")
    print(f"  {label:<24}{len(xs):>5}{len(xs) / ndays:>7.1f}{n:>+9.1f}"
          f"{(f'{tn:+.2f}' if tn is not None else '—'):>7}{stats(xs, 'move')[0]:>+9.1f}{stats(xs, 'cost')[0]:>+8.1f}"
          f"{statistics.median(x['spr'] for x in xs):>8.1f}"
          f"{sum(x['net'] for x in xs) / 1e4 * 1000:>+10.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", default="2026-08-01:2026-09-26")
    ap.add_argument("--after", default="2026-09-29:2026-10-02")
    ap.add_argument("--timing-churn", action="store_true",
                    help="instead of floors/ratios: entry time of day, and repeat round trips per name-day")
    args = ap.parse_args()
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    cl = bars.client()
    sets = {}
    for name, rng in (("BEFORE", args.before), ("AFTER", args.after)):
        lo, hi = rng.split(":")
        rows = round_trips(lo, hi, cache, cl, want_vol=not args.timing_churn)
        sets[name] = (rows, len({r["day"] for r in rows}), rng)

    hdr = (f"  {'group':<24}{'n':>5}{'/day':>7}{'net':>9}{'t':>7}{'move':>9}{'cost':>8}{'med spr':>8}"
           f"{'$ @1k':>10}")
    if args.timing_churn:
        return timing_churn(sets, hdr)
    for name, (rows, nd, rng) in sets.items():
        print(f"\n######## {name} {rng}: {len(rows)} round trips, {nd} days (bp; $ at $1,000/trade)\n")
        print("(2) PRICE FLOOR — entry price >= floor\n" + hdr)
        for f in (5, 10, 15, 20, 25, 30, 40, 50):
            line(f">= ${f}", [r for r in rows if r["px"] >= f], nd)
        base = [r for r in rows if r["px"] >= 10 and r["ratio"] is not None]
        print(f"\n(3) SPREAD / 1-MIN VOL — entry price >= $10 with vol known ({len(base)})\n" + hdr)
        if not base:
            continue
        qs = sorted(r["ratio"] for r in base)
        for i in range(5):
            lo_q, hi_q = qs[int(len(qs) * i / 5)], qs[min(len(qs) - 1, int(len(qs) * (i + 1) / 5) - 1)]
            line(f"quintile {i + 1} ({lo_q:.2f}-{hi_q:.2f})",
                 [r for r in base if lo_q <= r["ratio"] <= hi_q], nd)
        for cap in (0.25, 0.5, 0.75, 1.0, 1.5):
            line(f"ratio <= {cap:g}", [r for r in base if r["ratio"] <= cap], nd)
        print(f"\n(3b) SPREAD alone, same names\n" + hdr)
        for cap in (3, 5, 8, 12, 20):
            line(f"spread <= {cap} bp", [r for r in base if r["spr"] <= cap], nd)


def timing_churn(sets, hdr):
    for name, (rows, nd, rng) in sets.items():
        rows = [r for r in rows if r["px"] >= 10]
        print(f"\n######## {name} {rng}: {len(rows)} round trips >= $10, {nd} days (bp; $ at $1,000/trade)\n")
        print("(1) ENTRY TIME OF DAY (ET)\n" + hdr)
        for a, b in (("09:30", "10:00"), ("10:00", "10:30"), ("10:30", "12:00"), ("12:00", "14:00"),
                     ("14:00", "15:30"), ("15:30", "16:00")):
            ma, mb = (int(a[:2]) * 60 + int(a[3:]), int(b[:2]) * 60 + int(b[3:]))
            line(f"{a}-{b}", [r for r in rows if ma <= bars.et_minutes(r["t"]) < mb], nd)
        for a in ("10:00", "10:30"):
            ma = int(a[:2]) * 60 + int(a[3:])
            line(f"start at {a}", [r for r in rows if bars.et_minutes(r["t"]) >= ma], nd)
        # (2) churn: the k-th round trip in the same name that day, and what the previous one did
        seq = defaultdict(list)
        for r in sorted(rows, key=lambda r: r["t"]):
            seq[(r["sym"], r["day"])].append(r)
        for v in seq.values():
            for i, r in enumerate(v):
                r["k"] = i + 1
                r["prev"] = None if i == 0 else ("win" if v[i - 1]["net"] > 0 else "loss")
                r["gap"] = None if i == 0 else r["t"] - v[i - 1]["t"] - v[i - 1]["hold"]
        per = [len(v) for v in seq.values()]
        print(f"\n(2) CHURN: {len(seq)} name-days, round trips per name-day mean {statistics.mean(per):.1f}, "
              f"max {max(per)}; name-days with 5+: {sum(1 for x in per if x >= 5)}\n" + hdr)
        line("1st in name-day", [r for r in rows if r["k"] == 1], nd)
        line("2nd", [r for r in rows if r["k"] == 2], nd)
        line("3rd-4th", [r for r in rows if 3 <= r["k"] <= 4], nd)
        line("5th+", [r for r in rows if r["k"] >= 5], nd)
        line("re-entry after a win", [r for r in rows if r["prev"] == "win"], nd)
        line("re-entry after a loss", [r for r in rows if r["prev"] == "loss"], nd)
        line("  ...within 5 min", [r for r in rows if r["prev"] == "loss" and r["gap"] is not None and r["gap"] < 300], nd)
        line("  ...after 5+ min", [r for r in rows if r["prev"] == "loss" and r["gap"] is not None and r["gap"] >= 300], nd)
        line("cap 2 per name-day", [r for r in rows if r["k"] <= 2], nd)
        line("no re-entry after loss", [r for r in rows if r["prev"] != "loss"], nd)


if __name__ == "__main__":
    main()
