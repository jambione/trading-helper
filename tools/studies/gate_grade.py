#!/usr/bin/env python3
"""Grade the two in-sample admission gates on refusals logged AFTER each gate was chosen (ideas list #11).

Pre-registered here, committed before any outcome is computed (2026-10-04):

  Gates     gapped_down    (ai_watch_gap_down_block_pct 1.0, live from 2026-09-24; chosen on earlier daily data)
            admit_range_pos (ai_watch_admit_max_range_pos 90, live from 2026-09-06; chosen on data to 2026-09-05)
  Data      ai_reports/proposal_ledger/<day>.jsonl, every logged day from the gate's live date (the ledger starts
            2026-09-16), RTH decisions 09:45-15:00 ET. Free SIP 1-minute bars (runway_study cache).
  Units     name-days. REFUSED = name-days with at least one drop for that gate's reason; KEPT = name-days with at
            least one "kept" row and no drop for that reason. Start time = the first such row in the window.
  Outcome   gross, no cost: close of the last 1-min bar COMPLETED before the start time -> close 30 min later
            (primary, bp); -> 15:50 close (information). A name-day is dropped (counted) if either bar is missing
            or the exit bar is more than 2 minutes off target.
  Statistic diff = mean(REFUSED) - mean(KEPT), OLS on a REFUSED dummy, t from day-clustered (CR1) SE.
            Also: medians, the >= $10 stratum, and per-day diffs.
  Reading   The gates are live, so the bar is asymmetric:
              SUPPORTED     diff < 0 with t <= -2  (refused names really do worse: keep the gate)
              COSTS OPENS   diff > 0 with t >= 2   (refused names do better: the gate removes good names)
              UNCONFIRMED   otherwise (keep as is, re-grade later; no evidence either way)
            Two gates are graded, so a COSTS OPENS reading needs t >= 2.24 (Bonferroni 2) before any change is
            proposed, and any change still goes to the user after the close.

Run on the mini: .venv/bin/python tools/studies/gate_grade.py
"""
from __future__ import annotations

import collections
import glob
import json
import math
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import runway_study as rs  # noqa: E402

GATES = {"gapped_down": "2026-09-24", "admit_range_pos": "2026-09-06"}
T0, T1 = 9 * 60 + 45, 15 * 60
H = 30 * 60
END = 15 * 60 + 50


def cl_diff(y, g, day):
    """OLS of y on [1, g]; returns (coef on g, day-clustered CR1 SE)."""
    n = len(y)
    m0 = [y[i] for i in range(n) if not g[i]]
    m1 = [y[i] for i in range(n) if g[i]]
    a, b = statistics.fmean(m0), statistics.fmean(m1) - statistics.fmean(m0)
    sxx = sum((gi - sum(g) / n) ** 2 for gi in g)
    gbar = sum(g) / n
    by = collections.defaultdict(float)
    for i in range(n):
        u = y[i] - a - b * g[i]
        by[day[i]] += (g[i] - gbar) * u
    G = len(by)
    v = sum(s * s for s in by.values()) / sxx ** 2 * (G / (G - 1)) * ((n - 1) / (n - 2))
    return b, math.sqrt(v)


def load(gate, since):
    first = {}          # (sym, day) -> (ts, kind)
    for fp in sorted(glob.glob(os.path.join(ROOT, "ai_reports", "proposal_ledger", "*.jsonl"))):
        day = os.path.basename(fp)[:10]
        if day < since:
            continue
        refused, kept = {}, {}
        for line in open(fp):
            try:
                e = json.loads(line)
            except Exception:
                continue
            sym, ts = str(e.get("symbol") or ""), e.get("ts")
            if not rs.SYM_RE.match(sym) or not isinstance(ts, (int, float)) or e.get("heartbeat"):
                continue
            m = bars.et_minutes(ts)
            if not (T0 <= m <= T1):
                continue
            if e.get("decision") == "dropped" and e.get("reason") == gate:
                refused[sym] = min(refused.get(sym, ts), ts)
            elif e.get("decision") == "kept":
                kept[sym] = min(kept.get(sym, ts), ts)
        for sym, ts in refused.items():
            first[(sym, day)] = (ts, True)
        for sym, ts in kept.items():
            if sym not in refused:
                first[(sym, day)] = (ts, False)
    return first


def outcome(B, ts):
    st, cl = B[0], B[4]
    j = bars.index_at(st, ts - 60)
    if j < 0:
        return None
    px = cl[j]
    k = bars.index_at(st, st[j] + H)
    if k <= j or abs(st[k] - (st[j] + H)) > 120:
        return None
    r30 = (cl[k] / px - 1) * 1e4
    e = max((i for i, t in enumerate(st) if bars.et_minutes(t) <= END), default=-1)
    rend = (cl[e] / px - 1) * 1e4 if e > j else None
    return px, r30, rend


def main():
    cache = rs._load_cache()
    for gate, since in GATES.items():
        first = load(gate, since)
        want = collections.defaultdict(set)
        for (sym, day) in first:
            want[day].add(sym)
        for day, syms in sorted(want.items()):
            syms = sorted(syms)
            for i in range(0, len(syms), 100):
                rs.fetch_days({day: set(syms[i:i + 100])}, cache)
        rows, drops = [], collections.Counter()
        for (sym, day), (ts, ref) in first.items():
            B = cache.get((sym, day))
            if not B:
                drops["no_bars"] += 1
                continue
            o = outcome(B, ts)
            if o is None:
                drops["no_outcome"] += 1
                continue
            rows.append({"sym": sym, "day": day, "ref": ref, "px": o[0], "r30": o[1], "rend": o[2]})
        days = sorted({r["day"] for r in rows})
        print(f"\n=== {gate} (live from {since}; ledger days {days[0]}..{days[-1]}, {len(days)} days) ===")
        print(f"  name-days REFUSED {sum(r['ref'] for r in rows)}  KEPT {sum(not r['ref'] for r in rows)}  drops {dict(drops)}")
        for lab, sub in (("all", rows), (">= $10", [r for r in rows if r["px"] >= 10]), ("< $10", [r for r in rows if r["px"] < 10])):
            for key, name in (("r30", "30 min (PRIMARY)" if lab == "all" else "30 min"), ("rend", "to 15:50 (info)")):
                xs = [r for r in sub if r[key] is not None]
                R = [r[key] for r in xs if r["ref"]]
                K = [r[key] for r in xs if not r["ref"]]
                if len(R) < 10 or len(K) < 10 or len({r["day"] for r in xs}) < 3:
                    print(f"  {lab:<7}{name:<18} too few (R {len(R)}, K {len(K)})")
                    continue
                b, se = cl_diff([r[key] for r in xs], [1 if r["ref"] else 0 for r in xs], [r["day"] for r in xs])
                print(f"  {lab:<7}{name:<18} REFUSED n {len(R):>4} mean {statistics.fmean(R):>+7.1f} med {statistics.median(R):>+7.1f}"
                      f" | KEPT n {len(K):>4} mean {statistics.fmean(K):>+7.1f} med {statistics.median(K):>+7.1f}"
                      f" | diff {b:>+7.1f} bp  SE {se:5.1f}  t {b / se:>+5.2f}")
        xs = [r for r in rows]
        b, se = cl_diff([r["r30"] for r in xs], [1 if r["ref"] else 0 for r in xs], [r["day"] for r in xs])
        t = b / se
        verdict = "SUPPORTED" if t <= -2 else "COSTS OPENS" + (" (passes Bonferroni)" if t >= 2.24 else " (fails Bonferroni 2.24)") if t >= 2 else "UNCONFIRMED"
        print(f"  VERDICT (30 min, all): {verdict}")
        print("  per day diff (30 min):", " ".join(
            f"{d[5:]}:{statistics.fmean([r['r30'] for r in xs if r['day'] == d and r['ref']] or [float('nan')]) - statistics.fmean([r['r30'] for r in xs if r['day'] == d and not r['ref']] or [float('nan')]):+.0f}"
            for d in days))


if __name__ == "__main__":
    main()
