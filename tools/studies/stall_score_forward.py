#!/usr/bin/env python3
"""Forward read for docs/studies/stall_score_forward_prereg.json (operator 11/06: combine the apex-style signs to see if we buy into a stall).
Rules fixed there; do not retune. Counts only until 150 entries.   usage: tools/studies/stall_score_forward.py"""
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
MIN_N, MIN_FLAG, EXT_N, COST, Z_BAR, SEED, POS_TH = 150, 30, 300, 3.0, 2.24, 1106, 0.80


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
        if f.get("rs_rise15_bp") is None or f.get("rs_er15") is None:
            missing += 1
            continue
        pk = o.get("peak_price") or e
        rows.append({"d": datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d"), "rise": float(f["rs_rise15_bp"]), "er": float(f["rs_er15"]),
                     "pos": f.get("rs_pos30"), "rng": f.get("rs_rng15_bp"), "ng": (pk - e) / e * 1e4 < 5, "bp": (x - e) / e * 1e4 - COST})
    return rows, missing


def cuts(rows):
    r = sorted(x["rise"] for x in rows); e = sorted(x["er"] for x in rows)
    return r[(2 * len(r)) // 3], e[len(e) // 3]


def signs(r, c):
    cr, ce = c
    return {"A": r["rise"] >= cr, "B": r["er"] <= ce, "C": r["pos"] is not None and float(r["pos"]) >= POS_TH}


def score(r, c):
    return sum(signs(r, c).values())


def gap(rows, pred):
    t = [r for r in rows if pred(r)]; k = [r for r in rows if not pred(r)]
    return (ngr(t) - ngr(k)) if t and k else float("nan")


def verdict(rows):
    c = cuts(rows)
    stall = [r for r in rows if score(r, c) >= 2]
    rest = [r for r in rows if score(r, c) < 2]
    days = sorted({r["d"] for r in rows}); mid = days[len(days) // 2]
    lines = [f"cuts: rise15 top-third >= {c[0]:.1f} bp, er15 bottom-third <= {c[1]:.3f}, pos30 >= {POS_TH}; STALL (>= 2 signs) n {len(stall)} of {len(rows)} ({len(stall)/len(rows):.0%})"]
    if len(stall) < MIN_FLAG:
        lines.append(f"UNDERPOWERED: only {len(stall)} STALL entries (< {MIN_FLAG}); extend to {EXT_N} entries once")
        return False, lines
    halves_ok = True
    for lab, sub in (("A", [r for r in rows if r["d"] < mid]), ("B", [r for r in rows if r["d"] >= mid])):
        t = [r for r in sub if score(r, c) >= 2]; k = [r for r in sub if score(r, c) < 2]
        good = bool(t and k and ngr(t) > ngr(k) and mean(k) > mean(sub))
        halves_ok &= good
        lines.append(f"half {lab}: STALL NG {ngr(t):.0%} vs {ngr(k):.0%}; kept net {mean(k):+.1f} vs all {mean(sub):+.1f} -> {'ok' if good else 'NO'}")
    zz = z(sum(r["ng"] for r in stall), len(stall), sum(r["ng"] for r in rest), len(rest))
    rng = random.Random(SEED); byday = {}
    for r in rows:
        byday.setdefault(r["d"], []).append(r)
    diffs = []
    for _ in range(2000):
        s = [r for d in (rng.choice(days) for _ in days) for r in byday[d]]
        k = [r for r in s if score(r, c) < 2]
        if k and s:
            diffs.append(mean(k) - mean(s))
    lo5 = sorted(diffs)[int(len(diffs) * 0.05)] if diffs else float("nan")
    rv = sorted(r["rng"] for r in rows if r["rng"] is not None); terc = []
    if len(rv) >= 30:
        t1, t2 = rv[len(rv) // 3], rv[2 * len(rv) // 3]
        for a, b in ((-1, t1), (t1, t2), (t2, 1e18)):
            g = [r for r in rows if r["rng"] is not None and a < r["rng"] <= b]
            t = [r for r in g if score(r, c) >= 2]; k = [r for r in g if score(r, c) < 2]
            if len(t) >= 5 and len(k) >= 5:
                terc.append(ngr(t) - ngr(k))
    g_comp = gap(rows, lambda r: score(r, c) >= 2)
    parts = {n: gap(rows, lambda r, n=n: signs(r, c)[n]) for n in "ABC"}
    best = max(v for v in parts.values() if v == v) if any(v == v for v in parts.values()) else float("nan")
    for k in range(4):
        sub = [r for r in rows if score(r, c) == k]
        lines.append(f"info {k} signs: n {len(sub)}  NG {ngr(sub):.0%}  net {mean(sub):+.1f} bp" if sub else f"info {k} signs: n 0")
    lines.append("info single-sign NG gaps " + ", ".join(f"{n} {v:+.2f}" for n, v in parts.items()))
    better = g_comp > best
    ok = halves_ok and zz >= Z_BAR and lo5 > 0 and sum(x > 0 for x in terc) >= 2 and better
    lines.append(f"pooled NG z {zz:+.2f} (family bar {Z_BAR}); bootstrap 5th pct (kept - all) {lo5:+.2f} bp; rng15-tercile NG diffs {[round(x, 2) for x in terc]}; "
                 f"composite gap {g_comp:+.2f} vs best part {best:+.2f} -> {'better' if better else 'no better than its best part'}")
    lines.append("VERDICT: " + ("PASS (skeptic review next, then sweep + ship rules)" if ok else "FAIL / not shown: close per the prereg, no retuning"))
    return ok, lines


def main(argv=None):
    rows, missing = load()
    print(f"entries since 2026-11-06 12:00 ET with rs_rise15_bp and rs_er15: {len(rows)} over {len({r['d'] for r in rows})} days (missing field: {missing})")
    if len(rows) < MIN_N:
        print(f"NOT DUE: needs {MIN_N} entries ({MIN_N - len(rows)} more). Counts only before the read (prereg).")
        return 0
    print("\n".join(verdict(rows)[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
