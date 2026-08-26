#!/usr/bin/env python3
"""Are EXH and RSI fresh at the moment the desk decides to arm?

The desk gates arming on the age of the PRINT — _row_tape_stale, 8s via
ai_watch_decision_max_age_sec. But %R and Connors RSI are not computed from
the print; they come from bars and from the indicator map. A fresh print
with a stale indicator passes that gate and still decides on old numbers.

This asks, on RTH rows only (premarket is shelved and its price is frozen):

  1. how fresh is the print at decision time, and does the 8s gate bind?
  2. how fresh are the indicators, and is that ever checked?
  3. on the rows that actually ARMED, was every input fresh?
  4. what do pctr_src / cm_rsi_src say the numbers were built from?

Read-only.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict

sys.path.insert(0, ".")
sys.path.insert(0, "tools")

import bars  # noqa: E402
import shadow_report as sr  # noqa: E402

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
GATE = 8.0
OPEN_MIN, CLOSE_MIN = 9 * 60 + 30, 16 * 60

rows = sr.load()
days = sorted({bars.day_of(r["ts"]) for r in rows if r.get("ts")})[-DAYS:]
dayset = set(days)
rth = [r for r in rows
       if r.get("ts") and bars.day_of(r["ts"]) in dayset
       and OPEN_MIN <= bars.et_minutes(r["ts"]) < CLOSE_MIN]
print(f"sessions: {', '.join(days)}")
print(f"RTH shadow rows: {len(rth)}\n")
if not rth:
    raise SystemExit(1)

age_fields = sorted({k for r in rth for k in r
                     if "age" in k.lower() and isinstance(r.get(k), (int, float))})
print("age fields present:", ", ".join(age_fields) or "none")


def dist(label, xs, gate=None):
    if not xs:
        print(f"  {label:<22} none")
        return
    xs = sorted(xs)
    n = len(xs)
    q = lambda p: xs[int(p * (n - 1))]  # noqa: E731
    extra = ""
    if gate is not None:
        over = 100 * sum(1 for v in xs if v > gate) / n
        extra = f"   over {gate:.0f}s: {over:>5.1f}%"
    print(f"  {label:<22} n={n:<6} p50 {q(.5):>7.1f}  p90 {q(.9):>8.1f}  "
          f"max {xs[-1]:>9.1f}{extra}")


print("\nFRESHNESS AT DECISION TIME (all RTH rows)")
for f in age_fields:
    dist(f, [float(r[f]) for r in rth if r.get(f) is not None], GATE)

print("\nWHERE THE NUMBERS COME FROM")
for f in ("price_src", "pctr_src", "cm_rsi_src"):
    c = Counter(str(r.get(f)) for r in rth)
    tot = sum(c.values())
    top = "  ".join(f"{k}={100*v/tot:.0f}%" for k, v in c.most_common(4))
    print(f"  {f:<14} {top}")

armed = [r for r in rth if r.get("arm_ok") is True]
blocked = [r for r in rth if r.get("arm_ok") is False]
print(f"\nARM DECISIONS: {len(armed)} armed, {len(blocked)} blocked, "
      f"{len(rth)-len(armed)-len(blocked)} not evaluated")

if armed:
    print("\nON THE ROWS THAT ARMED — was every input fresh?")
    for f in age_fields:
        v = [float(r[f]) for r in armed if r.get(f) is not None]
        dist(f"  {f}", v, GATE)
    miss = defaultdict(int)
    for r in armed:
        for f in ("pctr", "cm_rsi", "exhaustion"):
            if r.get(f) is None:
                miss[f] += 1
    print(f"  armed with a MISSING indicator: "
          f"{dict(miss) if miss else 'none — all present'}")
    for f in age_fields:
        stale = [r for r in armed
                 if r.get(f) is not None and float(r[f]) > GATE]
        if stale:
            print(f"  !! armed with {f} > {GATE:.0f}s: "
                  f"{len(stale)}/{len(armed)} "
                  f"({100*len(stale)/len(armed):.0f}%)")

print("\nWHY ROWS WERE BLOCKED (top 8)")
c = Counter(str(r.get("block_code") or r.get("arm_why")) for r in blocked)
for k, v in c.most_common(8):
    print(f"  {k:<24} {v:>6}  ({100*v/max(1,len(blocked)):.0f}%)")

print("\nDOES THE PRINT GATE BIND? rows whose tape_age crosses 8s")
ta = [float(r["tape_age_sec"]) for r in rth if r.get("tape_age_sec") is not None]
if ta:
    over = sum(1 for v in ta if v > GATE)
    print(f"  tape_age_sec > {GATE:.0f}s on {over}/{len(ta)} "
          f"= {100*over/len(ta):.1f}% of RTH rows")
    sq = sum(1 for r in rth if str(r.get("block_code")) == "stale_quote")
    print(f"  block_code == stale_quote on {sq}/{len(rth)} "
          f"= {100*sq/len(rth):.1f}% of RTH rows")
