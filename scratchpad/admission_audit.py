#!/usr/bin/env python3
"""Does the watchlist admit each source quickly, and is selection weighted
the way the desk says it is?

Two of the five things the operator asked about:

  4. ADMISSION — momentum, trending, research and bb_live each seed the
     book. How fast does a seeded name become watchable, how many make it,
     and does one source crowd out the rest?
  5. WEIGHTING — the stated setup (HANDOFF 5F) is: up >=10% on the day,
     RVOL >= 5x, a catalyst, $2-20, float < 10M. This asks what the book
     ACTUALLY selected on, by measuring how often each leg was even
     knowable at admission.

Read-only.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict

sys.path.insert(0, ".")
sys.path.insert(0, "tools")

import bars  # noqa: E402
import shadow_report as sr  # noqa: E402

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 5

rows = sr.load()
days = sorted({bars.day_of(r["ts"]) for r in rows if r.get("ts")})[-DAYS:]
dayset = set(days)
sel = [r for r in rows if r.get("ts") and bars.day_of(r["ts"]) in dayset]
print(f"sessions: {', '.join(days)}   rows: {len(sel)}\n")

# ── 4. admission ────────────────────────────────────────────────────────
print("=" * 66)
print("4. ADMISSION BY SOURCE")
print("=" * 66)

first = {}
for r in sel:
    key = (r.get("symbol"), bars.day_of(r["ts"]))
    ts = float(r["ts"])
    if key not in first or ts < first[key][0]:
        first[key] = (ts, r)

by_src = defaultdict(list)
for key, (ts, r) in first.items():
    src = str(r.get("source") or "?")
    by_src[src].append((key, ts, r))

print(f"{'source':<14}{'names':>7}{'admitted':>10}{'armed':>7}"
      f"{'first-seen ET':>15}{'dwell→arm':>12}")
print("-" * 66)
armed_syms = {(r.get("symbol"), bars.day_of(r["ts"]))
              for r in sel if r.get("arm_ok") is True}
for src, items in sorted(by_src.items(), key=lambda kv: -len(kv[1])):
    n = len(items)
    adm = sum(1 for k, ts, r in items if r.get("admit_ts"))
    arm = sum(1 for k, ts, r in items if k in armed_syms)
    hrs = sorted(bars.et_minutes(ts) for k, ts, r in items)
    med = hrs[len(hrs) // 2]
    dwell = []
    for k, ts, r in items:
        a = r.get("admit_ts")
        if a:
            try:
                dwell.append(ts - float(a))
            except (TypeError, ValueError):
                pass
    dw = sorted(dwell)
    dws = f"{dw[len(dw)//2]:.0f}s" if dw else "-"
    print(f"{src:<14}{n:>7}{adm:>10}{arm:>7}"
          f"{med//60:>12}:{med%60:02d}{dws:>12}")

print("\nADMISSION LATENCY — seconds from admit_ts to first shadow row")
lat = []
for key, (ts, r) in first.items():
    a = r.get("admit_ts")
    if not a:
        continue
    try:
        d = ts - float(a)
    except (TypeError, ValueError):
        continue
    if 0 <= d < 3600:
        lat.append(d)
if lat:
    lat.sort()
    q = lambda p: lat[int(p * (len(lat) - 1))]  # noqa: E731
    print(f"  n={len(lat)}  p50 {q(.5):.1f}s  p90 {q(.9):.1f}s  "
          f"max {lat[-1]:.1f}s")

# ── 5. weighting ────────────────────────────────────────────────────────
print("\n" + "=" * 66)
print("5. WHAT THE BOOK COULD ACTUALLY SELECT ON")
print("=" * 66)
print("The stated setup needs five legs. A leg that is None at admission")
print("cannot have been weighed, whatever the rule says.\n")

legs = {
    "pct_change >= 10": "pct_change",
    "rvol >= 5": "rvol",
    "price $2-20": "price",
    "float < 10M": "shares_out_m",
    "catalyst (news)": "news_n_24h",
}
print(f"{'leg':<20}{'known':>9}{'of rows':>9}{'coverage':>10}{'passing':>10}")
print("-" * 60)
for label, field in legs.items():
    known = [r for r in sel if r.get(field) is not None]
    n = len(known)
    passing = 0
    for r in known:
        try:
            v = float(r[field])
        except (TypeError, ValueError):
            continue
        if field == "pct_change" and v >= 10:
            passing += 1
        elif field == "rvol" and v >= 5:
            passing += 1
        elif field == "price" and 2.0 <= v <= 20.0:
            passing += 1
        elif field == "shares_out_m" and v < 10:
            passing += 1
        elif field == "news_n_24h" and v > 0:
            passing += 1
    cov = 100 * n / max(1, len(sel))
    pas = f"{100*passing/n:.0f}%" if n else "-"
    print(f"{label:<20}{n:>9}{len(sel):>9}{cov:>9.0f}%{pas:>10}")

print("\nSETUP LEGS SATISFIED PER ROW (setup_n_legs)")
c = Counter(str(r.get("setup_n_legs")) for r in sel)
for k, v in sorted(c.items(), key=lambda kv: str(kv[0])):
    print(f"  {k:<8} {v:>7}  ({100*v/len(sel):.0f}%)")

print("\nBLOCK REASONS — what actually decided the book")
b = Counter(str(r.get("block_code") or r.get("arm_why"))
            for r in sel if r.get("arm_ok") is False)
tot = sum(b.values())
for k, v in b.most_common(10):
    print(f"  {k:<26}{v:>7}  ({100*v/max(1,tot):.0f}%)")
