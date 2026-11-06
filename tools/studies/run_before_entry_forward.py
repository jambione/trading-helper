#!/usr/bin/env python3
"""Forward read for docs/studies/run_before_entry_forward_prereg.json (operator 11/06: never-greens come from buying the apex
of a run that is already exhausted). Rules are fixed there; do not retune. Counts only until 150 entries.
usage: tools/studies/run_before_entry_forward.py"""
from __future__ import annotations

import json
import math
import os
import random
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ET = ZoneInfo("America/New_York")
START, MIN_N, COST, Z_BAR, SEED = "2026-11-06", 150, 3.0, 1.96, 1106


def load(path=None, since=START):
    rows, seen, missing = [], set(), 0
    for line in open(path or os.path.join(ROOT, "ai_reports/outcomes.jsonl")):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        f = o.get("features") or {}
        e, x, t = o.get("entry_price"), o.get("exit_price"), o.get("entry_time")
        if not (e and x and t) or (o.get("symbol"), round(t)) in seen:
            continue
        d = datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d")
        if d < since:
            continue
        seen.add((o.get("symbol"), round(t)))
        if f.get("rs_rise15_bp") is None:
            missing += 1
            continue
        pk = o.get("peak_price") or e
        rows.append({"d": d, "rise": float(f["rs_rise15_bp"]), "rng": f.get("rs_rng15_bp"), "pos": f.get("rs_pos30"),
                     "ng": (pk - e) / e * 1e4 < 5, "bp": (x - e) / e * 1e4 - COST})
    return rows, missing


def cut_of(rows):
    v = sorted(r["rise"] for r in rows)
    return v[(2 * len(v)) // 3] if v else float("nan")


def z(k1, n1, k2, n2):
    if min(n1, n2) < 5:
        return float("nan")
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (k1 / n1 - k2 / n2) / se if se else float("nan")


def ngr(rs):
    return sum(r["ng"] for r in rs) / len(rs) if rs else float("nan")


def mean(rs):
    return sum(r["bp"] for r in rs) / len(rs) if rs else float("nan")


def verdict(rows):
    c = cut_of(rows)
    top = [r for r in rows if r["rise"] >= c]
    rest = [r for r in rows if r["rise"] < c]
    days = sorted({r["d"] for r in rows})
    mid = days[len(days) // 2]
    halves_ok = True
    lines = [f"cut (2/3 rank of rs_rise15_bp) = {c:.1f} bp; TOP n {len(top)}, rest n {len(rest)}"]
    for lab, sub in (("A", [r for r in rows if r["d"] < mid]), ("B", [r for r in rows if r["d"] >= mid])):
        t = [r for r in sub if r["rise"] >= c]; k = [r for r in sub if r["rise"] < c]
        good = bool(t and k and ngr(t) > ngr(k) and mean(k) > mean(sub))
        halves_ok &= good
        lines.append(f"half {lab}: TOP NG {ngr(t):.0%} vs {ngr(k):.0%}; kept net {mean(k):+.1f} vs all {mean(sub):+.1f} -> {'ok' if good else 'NO'}")
    zz = z(sum(r["ng"] for r in top), len(top), sum(r["ng"] for r in rest), len(rest))
    rng = random.Random(SEED)
    byday = {}
    for r in rows:
        byday.setdefault(r["d"], []).append(r)
    diffs = []
    for _ in range(2000):
        s = [r for d in (rng.choice(days) for _ in days) for r in byday[d]]
        k = [r for r in s if r["rise"] < c]
        if k and s:
            diffs.append(mean(k) - mean(s))
    lo5 = sorted(diffs)[int(len(diffs) * 0.05)] if diffs else float("nan")
    rv = sorted(r["rng"] for r in rows if r["rng"] is not None)
    terc = []
    if len(rv) >= 30:
        t1, t2 = rv[len(rv) // 3], rv[2 * len(rv) // 3]
        for a, b in ((-1, t1), (t1, t2), (t2, 1e18)):
            g = [r for r in rows if r["rng"] is not None and a < r["rng"] <= b]
            t = [r for r in g if r["rise"] >= c]; k = [r for r in g if r["rise"] < c]
            if len(t) >= 5 and len(k) >= 5:
                terc.append(ngr(t) - ngr(k))
    for lab, sub in (("rs_pos30 >= 0.80", [r for r in rows if r["pos"] is not None and r["pos"] >= 0.8]),
                     ("rs_pos30 < 0.80", [r for r in rows if r["pos"] is not None and r["pos"] < 0.8])):
        t = [r for r in sub if r["rise"] >= c]; k = [r for r in sub if r["rise"] < c]
        lines.append(f"info inside {lab}: TOP NG {ngr(t):.0%} (n {len(t)}) vs {ngr(k):.0%} (n {len(k)})")
    ok = halves_ok and zz >= Z_BAR and lo5 > 0 and sum(x > 0 for x in terc) >= 2
    lines.append(f"pooled NG z {zz:+.2f} (bar {Z_BAR}); bootstrap 5th pct (kept - all) {lo5:+.2f} bp; rng15-tercile NG diffs {[round(x, 2) for x in terc]}")
    lines.append("VERDICT: " + ("PASS (skeptic review next, then sweep + ship rules)" if ok else "FAIL / not shown: close per the prereg, no retuning"))
    return ok, lines


def main(argv=None):
    rows, missing = load()
    print(f"entries since {START} with rs_rise15_bp: {len(rows)} over {len({r['d'] for r in rows})} days (missing field: {missing})")
    if len(rows) < MIN_N:
        print(f"NOT DUE: needs {MIN_N} entries ({MIN_N - len(rows)} more). Counts only before the read (prereg).")
        return 0
    print("\n".join(verdict(rows)[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
