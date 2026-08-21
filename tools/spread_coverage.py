#!/usr/bin/env python3
"""Did today actually record what crossing costs?

ai_max_spread_r is 0 "until it can be set from these rows rather than
guessed". Two defects kept those rows from being able to say it, both fixed
2026-08-21: 82% of arm-evaluated shadow rows reached the log with no bid, and
13% of the rows that DID have one recorded ask == bid as a free round trip
(0.000R) rather than as a missing bid.

This is the check that the fixes took. Run it half an hour after the open
rather than waiting for the close — a session that is not recording is a
session of crossing-cost data you do not get back, and the whole point of
running the desk unchanged is to collect it.

WHAT GOOD LOOKS LIKE
  coverage climbing toward 80-90% on arm-evaluated rows, locked-book rows near
  zero, and spread_r present on outcome rows (it never was before). Coverage
  still near 18% means the cached quote is as empty as the raw bid was — the
  record is structurally incomplete and the next move is the quote feed, not
  the threshold.

Read-only. Usage:
    python3 tools/spread_coverage.py            # today
    python3 tools/spread_coverage.py --days 5
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bars  # noqa: E402
import shadow_report as sr  # noqa: E402

OUTCOMES = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=1)
    args = ap.parse_args()

    rows = sr.load()
    if not rows:
        print("no shadow log")
        return 1
    days = sorted({bars.day_of(r["ts"]) for r in rows if r.get("ts")})[-args.days:]
    dayset = set(days)

    # Pre-market books are wide for reasons that have nothing to do with the
    # names: at 07:00 a median spread of 1.75R is the session, not the symbol.
    # Pooling the two makes the record look ruinous before the open and fine
    # after it, so they are counted apart and only RTH prices the threshold.
    per = defaultdict(lambda: {"eval": 0, "bid": 0, "sp": 0, "locked": 0,
                               "pre": 0})
    vals, pre_vals = [], []
    for r in rows:
        ts = r.get("ts")
        if not ts:
            continue
        day = bars.day_of(ts)
        if day not in dayset or r.get("arm_ok") is None:
            continue
        rth = 9 * 60 + 30 <= bars.et_minutes(ts) < 16 * 60
        d = per[day]
        if not rth:
            d["pre"] += 1
        d["eval"] += 1
        bid, ask = r.get("bid"), r.get("price")
        if bid is not None:
            d["bid"] += 1
            try:
                if ask is not None and float(bid) >= float(ask):
                    d["locked"] += 1
            except (TypeError, ValueError):
                pass
        sp = r.get("spread_r")
        if sp is not None:
            d["sp"] += 1
            try:
                (vals if rth else pre_vals).append(float(sp))
            except (TypeError, ValueError):
                pass

    if not per:
        print(f"no arm-evaluated rows yet for {days[-1] if days else '?'} — "
              "the desk has not started evaluating candidates")
        return 0

    print(f"{'session':<12} {'arm-eval':>9} {'pre-mkt':>8} {'bid':>7} "
          f"{'spread_r':>9} {'locked':>8}")
    print("-" * 58)
    for day in sorted(per):
        d = per[day]
        n = max(1, d["eval"])
        print(f"{day:<12} {d['eval']:>9} {d['pre']:>8} {100*d['bid']/n:>6.0f}% "
              f"{100*d['sp']/n:>8.0f}% {d['locked']:>8}")

    def _dist(label, xs):
        if not xs:
            print(f"\n{label}: no rows yet")
            return
        xs = sorted(xs)
        q = lambda p: xs[int(p * (len(xs) - 1))]  # noqa: E731
        cheap = sum(1 for v in xs if v <= 0.01)
        print(f"\n{label}  n={len(xs)}  p10 {q(.1):.3f}  median {q(.5):.3f}  "
              f"p90 {q(.9):.3f}  max {max(xs):.2f}   (R units)")
        print(f"  cheap moments (<= 0.01R): {100*cheap/len(xs):.0f}%")

    _dist("RTH spread_r      ", vals)
    _dist("PRE-MARKET spread_r", pre_vals)
    if pre_vals and not vals:
        print("\n  Only pre-market so far — those books are wide for reasons")
        print("  that have nothing to do with these names. Re-run after 09:30;")
        print("  the RTH line is the one that prices the threshold.")

    # The record the gate was actually waiting on: cost beside result.
    got = tot = 0
    if os.path.exists(OUTCOMES):
        with open(OUTCOMES, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if bars.day_of(d.get("ts") or 0) not in dayset:
                    continue
                tot += 1
                if (d.get("features") or {}).get("spread_r") is not None:
                    got += 1
    print(f"\noutcome rows with spread_r: {got} of {tot}"
          + ("  — cost now sits beside result" if got else
             "  (none yet; needs closed trades)"))

    print("\n18% was the old coverage. Climbing toward 80-90% means the record")
    print("is being written; still near 18% means the cached quote is as empty")
    print("as the raw bid was, and the quote feed is the next problem, not the")
    print("threshold. locked = ask==bid rows, which are now refused as missing")
    print("rather than recorded as a free round trip.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
