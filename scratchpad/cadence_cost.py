#!/usr/bin/env python3
"""What does the 20-second arm cadence cost?

The desk now has sub-second indicator data (bars_age_sec ~0.6s) and a 2s
sync that stamps fresh %R and RSI onto every watch record. But the arm gate
itself runs only in poll_once, on ai_watch_poll_sec = 20s, and the sync
explicitly does not re-decide ("not a place to re-run the whole arm
decision"). So ~18 of every 20 seconds of freshness is discarded before
anything acts on it.

Two things that costs, both measured here in R because that is the unit the
edge is in:

  1. DRIFT — how far price moves while the gate is asleep. This is the
     entry-price error the cadence imposes on every fill.
  2. FLICKER — how often the armable state changes between consecutive
     polls, which lower-bounds the opportunities that open and close
     unseen inside one sleep.

Consecutive shadow rows for a symbol are ~20s apart, so their delta IS the
gate's blind window. RTH only.

Read-only.
"""
from __future__ import annotations

import sys
from collections import defaultdict

sys.path.insert(0, ".")
sys.path.insert(0, "tools")

import bars  # noqa: E402
import shadow_report as sr  # noqa: E402

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
OPEN_MIN, CLOSE_MIN = 9 * 60 + 30, 16 * 60

rows = sr.load()
days = sorted({bars.day_of(r["ts"]) for r in rows if r.get("ts")})[-DAYS:]
dayset = set(days)
rth = [r for r in rows
       if r.get("ts") and bars.day_of(r["ts"]) in dayset
       and OPEN_MIN <= bars.et_minutes(r["ts"]) < CLOSE_MIN]
print(f"sessions: {', '.join(days)}   RTH rows: {len(rth)}\n")

per = defaultdict(list)
for r in rth:
    key = (r.get("symbol"), bars.day_of(r["ts"]))
    per[key].append(r)

gaps, drift_r, drift_pct, flips = [], [], [], 0
pairs = 0
arm_runs = defaultdict(int)
for key, seq in per.items():
    seq.sort(key=lambda r: float(r["ts"]))
    for a, b in zip(seq, seq[1:]):
        dt = float(b["ts"]) - float(a["ts"])
        if not (1.0 <= dt <= 120.0):
            continue          # session boundary or a re-admit, not a poll gap
        pa, pb = a.get("price"), b.get("price")
        stop = a.get("stop_price")
        gaps.append(dt)
        pairs += 1
        if a.get("arm_ok") is not None and b.get("arm_ok") is not None:
            if bool(a["arm_ok"]) != bool(b["arm_ok"]):
                flips += 1
        try:
            pa, pb, stop = float(pa), float(pb), float(stop)
        except (TypeError, ValueError):
            continue
        risk = pa - stop
        if risk <= 0 or pa <= 0:
            continue
        drift_r.append(abs(pb - pa) / risk)
        drift_pct.append(abs(pb - pa) / pa * 100.0)

for key, seq in per.items():
    n = sum(1 for r in seq if r.get("arm_ok") is True)
    if n:
        arm_runs[n] += 1


def dist(label, xs, unit=""):
    if not xs:
        print(f"  {label:<28} none")
        return
    xs = sorted(xs)
    n = len(xs)
    q = lambda p: xs[int(p * (n - 1))]  # noqa: E731
    print(f"  {label:<28} n={n:<6} p50 {q(.5):>7.3f}  p90 {q(.9):>7.3f}  "
          f"p99 {q(.99):>8.3f}{unit}")


print("OBSERVED POLL GAP (the gate's blind window)")
dist("seconds between samples", gaps, "s")

print("\nDRIFT WHILE THE GATE SLEEPS")
dist("|price move| per gap, in R", drift_r)
dist("|price move| per gap, in %", drift_pct, "%")
if drift_r:
    d = sorted(drift_r)
    med = d[len(d) // 2]
    print(f"\n  Median blind-window drift is {med:.3f}R.")
    print(f"  For scale: HANDOFF §2 puts the ratchet's whole edge at "
          f"+0.167 R/trade,")
    print(f"  and the median trade MFE at ~0.046R.")
    over = 100 * sum(1 for v in d if v > 0.046) / len(d)
    print(f"  Gaps that drift further than the median MFE: {over:.0f}%")

print("\nFLICKER")
if pairs:
    print(f"  arm_ok changed between consecutive polls: {flips}/{pairs} "
          f"= {100*flips/pairs:.1f}% of gaps")
    print(f"  Every flip is a state the gate saw only at its edges; a state "
          f"that\n  opened AND closed inside one 20s sleep is not counted "
          f"here at all,\n  so this is a floor, not an estimate.")

print("\nARM PERSISTENCE (how long an armable state lasted, in polls)")
tot = sum(arm_runs.values())
for k in sorted(arm_runs)[:8]:
    print(f"  armed on {k:>2} consecutive-ish samples: {arm_runs[k]:>4} "
          f"symbol-days ({100*arm_runs[k]/max(1,tot):.0f}%)")
print(f"  symbol-days with any arm: {tot}")
