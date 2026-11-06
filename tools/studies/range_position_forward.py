#!/usr/bin/env python3
"""Forward read for docs/studies/range_position_forward_prereg.json. Rules are fixed there; do not retune.
Reads ai_reports/outcomes.jsonl rows that carry features.rs_pos30. Prints counts always; the verdict only at >= 150 entries.
usage: tools/studies/range_position_forward.py [--since 2026-11-06]"""
from __future__ import annotations

import argparse
import json
import sys
import math
import os
import random
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feed_regime as fr  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ET = ZoneInfo("America/New_York")
TH, MIN_N, COST = 0.80, 150, 3.0


def load(since):
    rows, seen = [], set()
    for line in open(os.path.join(ROOT, "ai_reports/outcomes.jsonl")):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        f = o.get("features") or {}
        e, x, t = o.get("entry_price"), o.get("exit_price"), o.get("entry_time")
        if f.get("rs_pos30") is None or not (e and x and t) or (o.get("symbol"), round(t)) in seen:
            continue
        seen.add((o.get("symbol"), round(t)))
        d = datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d")
        if since and d < since:
            continue
        pk = o.get("peak_price") or e
        rows.append({"d": d, "t": float(t), "pos": float(f["rs_pos30"]), "rng": f.get("rs_rng15_bp"), "ng": (pk - e) / e * 1e4 < 5,
                     "bp": (x - e) / e * 1e4 - COST})
    return rows


def z(k1, n1, k2, n2):
    if min(n1, n2) < 5:
        return float("nan")
    p = (k1 + k2) / (n1 + n2); se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (k1 / n1 - k2 / n2) / se if se else float("nan")


def split(rows):
    return [r for r in rows if r["pos"] >= TH], [r for r in rows if r["pos"] < TH]


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--since", default="2026-11-06")
    rows = load(ap.parse_args().since)
    top, rest = split(rows)
    print(f"entries with rs_pos30: {len(rows)} over {len({r['d'] for r in rows})} days; top (>= {TH}) {len(top)}, rest {len(rest)}")
    label, sample, info = fr.pick(rows, MIN_N)
    print("\n".join(info))
    if sample is None:  # counts only before the read (prereg: no peeking); 11/06 moved the means below this line
        biggest = max(len(x) for x in fr.split(rows).values())
        print(f"NOT DUE: needs {MIN_N} entries in one feed sample ({MIN_N - biggest} more)"); return 0
    rows = sample
    top, rest = split(rows)
    if top:
        print(f"  top  never-green {sum(r['ng'] for r in top)/len(top):.0%}  net {sum(r['bp'] for r in top)/len(top):+.1f} bp")
    if rest:
        print(f"  rest never-green {sum(r['ng'] for r in rest)/len(rest):.0%}  net {sum(r['bp'] for r in rest)/len(rest):+.1f} bp")
    days = sorted({r["d"] for r in rows}); mid = days[len(days) // 2]
    ok = True
    for lab, sub in (("A", [r for r in rows if r["d"] < mid]), ("B", [r for r in rows if r["d"] >= mid])):
        t, r_ = split(sub)
        good = bool(t and r_ and sum(x["ng"] for x in t) / len(t) > sum(x["ng"] for x in r_) / len(r_)
                    and sum(x["bp"] for x in r_) / len(r_) > sum(x["bp"] for x in sub) / len(sub))
        ok &= good
        print(f"half {lab}: top n{len(t)} NG {sum(x['ng'] for x in t)/max(1,len(t)):.0%} vs {sum(x['ng'] for x in r_)/max(1,len(r_)):.0%}; kept net {sum(x['bp'] for x in r_)/max(1,len(r_)):+.1f} vs all {sum(x['bp'] for x in sub)/max(1,len(sub)):+.1f} -> {'ok' if good else 'NO'}")
    zz = z(sum(r["ng"] for r in top), len(top), sum(r["ng"] for r in rest), len(rest))
    rng = random.Random(1105); byday = {}
    for r in rows:
        byday.setdefault(r["d"], []).append(r)
    lows = []
    for _ in range(2000):
        s = [r for d in (rng.choice(days) for _ in days) for r in byday[d]]
        k = [r for r in s if r["pos"] < TH]
        if k and s:
            lows.append(sum(r["bp"] for r in k) / len(k) - sum(r["bp"] for r in s) / len(s))
    lo5 = sorted(lows)[int(len(lows) * 0.05)] if lows else float("nan")
    rv = sorted(r["rng"] for r in rows if r["rng"] is not None)
    terc = []
    if len(rv) >= 30:
        t1, t2 = rv[len(rv) // 3], rv[2 * len(rv) // 3]
        for a, b in ((-1, t1), (t1, t2), (t2, 1e18)):
            g = [r for r in rows if r["rng"] is not None and a < r["rng"] <= b]
            t, r_ = split(g)
            if len(t) >= 5 and len(r_) >= 5:
                terc.append(sum(x["ng"] for x in t) / len(t) - sum(x["ng"] for x in r_) / len(r_))
    ok_all = ok and zz >= 1.64 and lo5 > 0 and sum(x > 0 for x in terc) >= 2
    print(f"pooled NG z {zz:+.2f}; bootstrap 5th pct of (kept - all) {lo5:+.2f} bp; rng15-tercile NG diffs {[round(x, 2) for x in terc]}")
    print("VERDICT:", "PASS (skeptic review next, then the sweep/ship rules)" if ok_all else "FAIL / not shown: close per the prereg, no retuning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
