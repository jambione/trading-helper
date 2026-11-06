#!/usr/bin/env python3
"""Forward read for docs/studies/run_efficiency_forward_prereg.json (operator 11/06: tell a choppy, stalled run from a clean one).
Rules are fixed there; do not retune. Counts only until 150 entries.   usage: tools/studies/run_efficiency_forward.py"""
from __future__ import annotations

import json
import os
import random
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_before_entry_forward import mean, ngr, z  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ET = ZoneInfo("America/New_York")
START_TS = datetime(2026, 11, 6, 12, 0, tzinfo=ET).timestamp()
MIN_N, COST, Z_BAR, SEED = 150, 3.0, 2.13, 1106


def load(path=None, start_ts=START_TS):
    rows, seen, missing = [], set(), 0
    for line in open(path or os.path.join(ROOT, "ai_reports/outcomes.jsonl")):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        f = o.get("features") or {}
        e, x, t = o.get("entry_price"), o.get("exit_price"), o.get("entry_time")
        if not (e and x and t) or t < start_ts or (o.get("symbol"), round(t)) in seen:
            continue
        seen.add((o.get("symbol"), round(t)))
        if f.get("rs_er15") is None:
            missing += 1
            continue
        pk = o.get("peak_price") or e
        rows.append({"d": datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d"), "er": float(f["rs_er15"]), "rng": f.get("rs_rng15_bp"),
                     "rise": f.get("rs_rise15_bp"), "dwell": f.get("dwell_sec"), "ng": (pk - e) / e * 1e4 < 5, "bp": (x - e) / e * 1e4 - COST})
    return rows, missing


def cut_of(rows):
    v = sorted(r["er"] for r in rows)
    return v[len(v) // 3] if v else float("nan")


def verdict(rows):
    c = cut_of(rows)
    flag = [r for r in rows if r["er"] <= c]
    rest = [r for r in rows if r["er"] > c]
    days = sorted({r["d"] for r in rows})
    mid = days[len(days) // 2]
    halves_ok = True
    lines = [f"cut (1/3 rank of rs_er15) = {c:.3f}; FLAG (<= cut) n {len(flag)}, rest n {len(rest)}"]
    for lab, sub in (("A", [r for r in rows if r["d"] < mid]), ("B", [r for r in rows if r["d"] >= mid])):
        t = [r for r in sub if r["er"] <= c]; k = [r for r in sub if r["er"] > c]
        good = bool(t and k and ngr(t) > ngr(k) and mean(k) > mean(sub))
        halves_ok &= good
        lines.append(f"half {lab}: FLAG NG {ngr(t):.0%} vs {ngr(k):.0%}; kept net {mean(k):+.1f} vs all {mean(sub):+.1f} -> {'ok' if good else 'NO'}")
    zz = z(sum(r["ng"] for r in flag), len(flag), sum(r["ng"] for r in rest), len(rest))
    rng = random.Random(SEED)
    byday = {}
    for r in rows:
        byday.setdefault(r["d"], []).append(r)
    diffs = []
    for _ in range(2000):
        s = [r for d in (rng.choice(days) for _ in days) for r in byday[d]]
        k = [r for r in s if r["er"] > c]
        if k and s:
            diffs.append(mean(k) - mean(s))
    lo5 = sorted(diffs)[int(len(diffs) * 0.05)] if diffs else float("nan")
    rv = sorted(r["rng"] for r in rows if r["rng"] is not None)
    terc = []
    if len(rv) >= 30:
        t1, t2 = rv[len(rv) // 3], rv[2 * len(rv) // 3]
        for a, b in ((-1, t1), (t1, t2), (t2, 1e18)):
            g = [r for r in rows if r["rng"] is not None and a < r["rng"] <= b]
            t = [r for r in g if r["er"] <= c]; k = [r for r in g if r["er"] > c]
            if len(t) >= 5 and len(k) >= 5:
                terc.append(ngr(t) - ngr(k))
    rv2 = sorted(r["rise"] for r in rows if r["rise"] is not None)
    if rv2:
        cr = rv2[(2 * len(rv2)) // 3]
        for lab, sub in (("rise15 top third", [r for r in rows if r["rise"] is not None and r["rise"] >= cr]),
                         ("rise15 rest", [r for r in rows if r["rise"] is not None and r["rise"] < cr])):
            t = [r for r in sub if r["er"] <= c]; k = [r for r in sub if r["er"] > c]
            lines.append(f"info inside {lab}: FLAG NG {ngr(t):.0%} (n {len(t)}) vs {ngr(k):.0%} (n {len(k)})")
    for lab, sub in (("dwell < 60 s", [r for r in rows if r["dwell"] is not None and r["dwell"] < 60]),
                     ("dwell >= 60 s", [r for r in rows if r["dwell"] is not None and r["dwell"] >= 60])):
        t = [r for r in sub if r["er"] <= c]; k = [r for r in sub if r["er"] > c]
        lines.append(f"info inside {lab}: FLAG NG {ngr(t):.0%} (n {len(t)}) vs {ngr(k):.0%} (n {len(k)})")
    ok = halves_ok and zz >= Z_BAR and lo5 > 0 and sum(x > 0 for x in terc) >= 2
    lines.append(f"pooled NG z {zz:+.2f} (family bar {Z_BAR}); bootstrap 5th pct (kept - all) {lo5:+.2f} bp; rng15-tercile NG diffs {[round(x, 2) for x in terc]}")
    lines.append("VERDICT: " + ("PASS (skeptic review next, then sweep + ship rules)" if ok else "FAIL / not shown: close per the prereg, no retuning"))
    return ok, lines


def main(argv=None):
    rows, missing = load()
    print(f"entries since 2026-11-06 12:00 ET with rs_er15: {len(rows)} over {len({r['d'] for r in rows})} days (missing field: {missing})")
    if len(rows) < MIN_N:
        print(f"NOT DUE: needs {MIN_N} entries ({MIN_N - len(rows)} more). Counts only before the read (prereg).")
        return 0
    print("\n".join(verdict(rows)[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
