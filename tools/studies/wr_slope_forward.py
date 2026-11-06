#!/usr/bin/env python3
"""Forward read for docs/studies/wr_slope_forward_prereg.json (H1 flat, H2 steep). Rules are fixed there; do not retune.
usage: wr_slope_forward.py [--since 2026-11-06]"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feed_regime as fr  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ET = ZoneInfo("America/New_York")
MIN_N, MIN_FLAG_HALF, MIN_FLAG_TOTAL, MIN_DAYS, Z_BAR, COST, BOOT = 150, 30, 60, 8, 1.96, 3.0, 2000
START = "2026-11-06"      # pinned by the prereg: 11/04 (logging began 16:00, sideways test day) and 11/05 (suggested H2) are excluded
MAX_N = 400               # extension rule: re-read every +50 entries until a hypothesis has enough flagged entries; at 400 close as UNDERPOWERED
LONG_HOLD_S = 120.0
HYP = {"H1_flat": lambda f: max(abs(f["wr_d5_fast"]), abs(f["wr_d5_slow"])) <= 10.7,
       "H2_steep": lambda f: f["wr_d5_fast"] >= 30.0}


MISSING_PEAK = [0]


def load(since):
    rows, seen = [], set()
    for line in open(os.path.join(ROOT, "ai_reports/outcomes.jsonl")):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        f = o.get("features") or {}
        e, x, t = o.get("entry_price"), o.get("exit_price"), o.get("entry_time")
        if f.get("wr_d5_fast") is None or f.get("wr_d5_slow") is None or not (e and x and t) or (o.get("symbol"), round(t)) in seen:
            continue
        seen.add((o.get("symbol"), round(t)))
        d = datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d")
        if d < since:
            continue
        pk = o.get("peak_price")
        if pk is None:                       # a missing peak is NOT never-green: drop and count
            MISSING_PEAK[0] += 1
            continue
        rows.append({"d": d, "t": float(t), "f": f, "ng": (pk - e) / e * 1e4 < 5, "bp": (x - e) / e * 1e4 - COST,
                     "rng": f.get("rs_rng15_bp"), "hold": float(o.get("hold_sec") or 0.0)})
    return rows


def z(k1, n1, k2, n2):
    if min(n1, n2) < 5:
        return float("nan")
    p = (k1 + k2) / (n1 + n2); se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (k1 / n1 - k2 / n2) / se if se else float("nan")


def read(rows, name, flag):
    days = sorted({r["d"] for r in rows})
    ts = sorted(r["t"] for r in rows); mid_t = ts[len(ts) // 2]          # split at the MEDIAN ENTRY, not the median day
    halves = {"A": [r for r in rows if r["t"] < mid_t], "B": [r for r in rows if r["t"] >= mid_t]}
    ok = True; under = False
    for lab, sub in halves.items():
        fl = [r for r in sub if flag(r["f"])]; rest = [r for r in sub if not flag(r["f"])]
        if len(fl) < MIN_FLAG_HALF or len({r["d"] for r in sub}) < MIN_DAYS:
            under = True
        good = bool(fl and rest and sum(r["ng"] for r in fl) / len(fl) > sum(r["ng"] for r in rest) / len(rest)
                    and sum(r["bp"] for r in rest) / len(rest) > sum(r["bp"] for r in sub) / len(sub))
        ok &= good
        print(f"  half {lab}: flagged n{len(fl)} NG {sum(r['ng'] for r in fl)/max(1,len(fl)):.0%} vs {sum(r['ng'] for r in rest)/max(1,len(rest)):.0%}; "
              f"kept net {sum(r['bp'] for r in rest)/max(1,len(rest)):+.1f} vs all {sum(r['bp'] for r in sub)/max(1,len(sub)):+.1f} -> {'ok' if good else 'NO'}")
    fl = [r for r in rows if flag(r["f"])]; rest = [r for r in rows if not flag(r["f"])]
    if len(fl) < MIN_FLAG_TOTAL:
        under = True
    zz = z(sum(r["ng"] for r in fl), len(fl), sum(r["ng"] for r in rest), len(rest))
    if zz != zz:
        under = True                         # NaN z is underpowered, not a failure
    rng = random.Random(1106); byday = {}
    for r in rows:
        byday.setdefault(r["d"], []).append(r)
    lows, ngd = [], []
    for _ in range(BOOT):
        s_ = [r for d in (rng.choice(days) for _ in days) for r in byday[d]]
        k = [r for r in s_ if not flag(r["f"])]; f_ = [r for r in s_ if flag(r["f"])]
        if k and s_:
            lows.append(sum(r["bp"] for r in k) / len(k) - sum(r["bp"] for r in s_) / len(s_))
        if k and f_:
            ngd.append(sum(r["ng"] for r in f_) / len(f_) - sum(r["ng"] for r in k) / len(k))
    lo5 = sorted(lows)[int(len(lows) * 0.05)] if lows else float("nan")
    ng_lo = sorted(ngd)[int(len(ngd) * 0.025)] if ngd else float("nan")       # day-clustered, one-sided 0.025
    long_ = [r for r in rows if r["hold"] >= LONG_HOLD_S]
    fl_l, rest_l = [r for r in long_ if flag(r["f"])], [r for r in long_ if not flag(r["f"])]
    long_ok = bool(fl_l and rest_l and sum(r["ng"] for r in fl_l) / len(fl_l) > sum(r["ng"] for r in rest_l) / len(rest_l))
    mh = lambda v: sorted(r["hold"] for r in v)[len(v) // 2] if v else float("nan")
    terc = []
    rv = sorted(r["rng"] for r in rows if r["rng"] is not None)
    if len(rv) >= 30:
        t1, t2 = rv[len(rv) // 3], rv[2 * len(rv) // 3]
        for a_, b_ in ((float("-inf"), t1), (t1, t2), (t2, float("inf"))):
            g = [r for r in rows if r["rng"] is not None and a_ < r["rng"] <= b_] if a_ != float("-inf") else [r for r in rows if r["rng"] is not None and r["rng"] <= b_]
            f_, r_ = [r for r in g if flag(r["f"])], [r for r in g if not flag(r["f"])]
            if len(f_) >= 5 and len(r_) >= 5:
                terc.append(sum(x["ng"] for x in f_) / len(f_) - sum(x["ng"] for x in r_) / len(r_))
    testable = len(terc) == 3
    conf_ok = (not testable) or sum(x > 0 for x in terc) >= 2
    base = under is False and ok and zz >= Z_BAR and ng_lo > 0 and lo5 > 0 and long_ok and conf_ok
    verdict = "UNDERPOWERED (extend by +50 entries; close at %d)" % MAX_N if under else (
        ("PASS (skeptic review next)" + ("" if testable else " - range confound NOT TESTABLE") if base else "FAIL"))
    print(f"  pooled flagged {len(fl)} / rest {len(rest)}; NG z {zz:+.2f} (bar {Z_BAR}); day-clustered NG-gap 2.5th pct {ng_lo:+.3f}; "
          f"bootstrap 5th pct (unflagged - all) {lo5:+.2f} bp; hold>=120 s check {'ok' if long_ok else 'NO'}")
    print(f"  median hold flagged {mh(fl):.0f} s vs rest {mh(rest):.0f} s; range terciles {[round(x, 2) for x in terc] or 'NOT TESTABLE'}\n  {name}: {verdict}")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--since", default=START)
    since = max(ap.parse_args().since, START)             # the prereg pins the start; an earlier date is ignored
    rows = load(since)
    print(f"rows dropped for a missing peak_price: {MISSING_PEAK[0]}")
    print(f"entries with slope fields since the start date: {len(rows)} over {len({r['d'] for r in rows})} days")
    for n, fn in HYP.items():
        fl = [r for r in rows if fn(r["f"])]
        print(f"{n}: flagged {len(fl)} ({len(fl)/max(1,len(rows)):.0%})")
    label, sample, info = fr.pick(rows, MIN_N)
    print("\n".join(info))
    if sample is None:
        biggest = max(len(x) for x in fr.split(rows).values())
        print(f"NOT DUE: needs {MIN_N} entries in one feed sample ({MIN_N - biggest} more). Do not peek before the due date (prereg)."); return 0
    rows = sample
    for n, fn in HYP.items():
        print(n); read(rows, n, fn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
