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

    per = defaultdict(lambda: {"eval": 0, "bid": 0, "sp": 0, "locked": 0})
    vals = []
    for r in rows:
        ts = r.get("ts")
        if not ts:
            continue
        day = bars.day_of(ts)
        if day not in dayset or r.get("arm_ok") is None:
            continue
        d = per[day]
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
                vals.append(float(sp))
            except (TypeError, ValueError):
                pass

    if not per:
        print(f"no arm-evaluated rows yet for {days[-1] if days else '?'} — "
              "the desk has not started evaluating candidates")
        return 0

    print(f"{'session':<12} {'arm-eval':>9} {'bid':>7} {'spread_r':>9} {'locked':>8}")
    print("-" * 50)
    for day in sorted(per):
        d = per[day]
        n = max(1, d["eval"])
        print(f"{day:<12} {d['eval']:>9} {100*d['bid']/n:>6.0f}% "
              f"{100*d['sp']/n:>8.0f}% {d['locked']:>8}")

    if vals:
        vals.sort()
        q = lambda p: vals[int(p * (len(vals) - 1))]  # noqa: E731
        print(f"\nspread_r  n={len(vals)}  p10 {q(.1):.3f}  median {q(.5):.3f}  "
              f"p90 {q(.9):.3f}  max {max(vals):.2f}   (R units)")
        cheap = sum(1 for v in vals if v <= 0.01)
        print(f"genuinely cheap moments (<= 0.01R): {100*cheap/len(vals):.0f}% "
              f"— this is the tail a spread rule would trade")

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
