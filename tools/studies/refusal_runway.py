#!/usr/bin/env python3
"""Do refused names run better or worse than the names we traded?

Refusals come from ai_reports/admit_ledger/<day>.jsonl. Each (symbol, day,
reason) is sampled at most once per 15 minutes, RTH 09:35-15:30, and scored
from that minute's 1m close with runway_study.score_path. The comparison
row is the traded names' own minutes after their first fill, measured the
same way (from close, no fill cost), so both sides are on one basis.
"""
from __future__ import annotations

import collections
import glob
import json
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import runway_study as rs  # noqa: E402

DAYS = sys.argv[1:] or ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22"]
BUCKET = 15 * 60

samples = []          # (reason, sym, day, ts, extra)
rvols = collections.defaultdict(list)
for day in DAYS:
    seen = set()
    for line in open(os.path.join(ROOT, "ai_reports", "admit_ledger", f"{day}.jsonl")):
        try:
            e = json.loads(line)
        except Exception:
            continue
        sym = str(e.get("symbol") or "")
        if not rs.SYM_RE.match(sym):
            continue
        ts = e.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        m = bars.et_minutes(ts)
        if m < 9 * 60 + 35 or m > 15 * 60 + 30:
            continue
        reason = str(e.get("reason"))
        if reason == "thin_rvol" and isinstance(e.get("rvol"), (int, float)):
            rvols[e.get("source")].append(float(e["rvol"]))
        key = (reason, sym, day, int(ts // BUCKET))
        if key in seen:
            continue
        seen.add(key)
        samples.append((reason, sym, day, float(ts), e))
    print(f"{day}: {len(seen)} samples", file=sys.stderr)

# thin_rvol values: is the gate reading a sane number?
print("== thin_rvol drops: RVOL value distribution (every row, by source) ==")
for src, xs in sorted(rvols.items(), key=lambda x: -len(x[1])):
    xs.sort()
    print(f"  {str(src):<10} n={len(xs):>7}  p10 {rs.q(xs,.1):.4f}  p50 {rs.q(xs,.5):.4f}"
          f"  p90 {rs.q(xs,.9):.3f}  share<0.05 {sum(x < 0.05 for x in xs)/len(xs):.0%}"
          f"  share<0.5 {sum(x < 0.5 for x in xs)/len(xs):.0%}")

# bars, chunked per day so each request stays small; shares runway_study's cache
cache = rs._load_cache()
by_day = collections.defaultdict(set)
for reason, sym, day, ts, e in samples:
    by_day[day].add(sym)
for day, syms in by_day.items():
    syms = sorted(syms)
    for i in range(0, len(syms), 100):
        rs.fetch_days({day: set(syms[i:i + 100])}, cache)

scored = collections.defaultdict(list)
for reason, sym, day, ts, e in samples:
    B = cache.get((sym, day))
    if not B:
        continue
    i = bars.index_at(B[0], ts)
    if i < 0:
        continue
    s = rs.score_path(B, i, B[4][i])
    if s:
        s["px"] = B[4][i]
        scored[reason].append(s)

# traded names after first fill, same days, same basis
fills = [json.loads(l) for l in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl"))
         if l.strip()]
first = {}
for r in fills:
    et = r.get("entry_time")
    if isinstance(et, (int, float)) and bars.day_of(et) in DAYS and rs.SYM_RE.match(str(r.get("symbol"))):
        k = (r["symbol"], bars.day_of(et))
        first[k] = min(first.get(k, et), et)
for (sym, day), t0 in first.items():
    B = cache.get((sym, day))
    if not B:
        continue
    for i in range(0, len(B[0]), 5):
        if B[0][i] >= t0 and bars.et_minutes(B[0][i]) <= 15 * 60 + 30:
            s = rs.score_path(B, i, B[4][i])
            if s:
                s["px"] = B[4][i]
                scored["~TRADED (after 1st fill)"].append(s)


def row(name, xs):
    tp = [x["tp_1.0"] for x in xs if x.get("tp_1.0") is not None]
    r30 = [x["ret_30"] for x in xs if x.get("ret_30") is not None]
    u15 = [x["up_15"] for x in xs if x.get("up_15") is not None]
    d15 = [x["dn_15"] for x in xs if x.get("dn_15") is not None]
    if len(r30) < 20:
        return None
    se = statistics.stdev(r30) / len(r30) ** 0.5
    return (f"  {name:<28}{len(xs):>7}{rs.q(u15,.5):>+8.2f}{rs.q(d15,.5):>+8.2f}"
            f"{rs.mean(tp):>7.0%}{rs.mean(r30):>+8.2f} ±{se:.2f}{rs.q(r30,.5):>+8.2f}")


print(f"\n== RUNWAY FROM THE REFUSAL MINUTE ({', '.join(DAYS)}) ==")
print(f"  {'reason':<28}{'n':>7}{'up15':>8}{'dn15':>8}{'tp1':>7}{'ret30':>14}{'med30':>8}")
for name, xs in sorted(scored.items(), key=lambda kv: -len(kv[1])):
    line = row(name, xs)
    if line:
        print(line)

# price split inside the big refusal buckets (price was the one stable feature)
print("\n== SAME, SPLIT AT $10 ==")
for name in ("thin_rvol", "red", "stale_tape_admit", "~TRADED (after 1st fill)"):
    xs = scored.get(name, [])
    for lab, sub in (("<$10", [x for x in xs if x["px"] < 10]), (">=$10", [x for x in xs if x["px"] >= 10])):
        line = row(f"{name} {lab}", sub)
        if line:
            print(line)
