#!/usr/bin/env python3
"""Forward read for docs/studies/wr_slope_forward_prereg.json (H1 flat, H2 steep). Rules are fixed there; do not retune.
usage: wr_slope_forward.py [--since 2026-11-06]"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ET = ZoneInfo("America/New_York")
MIN_N, MIN_FLAG, MIN_DAYS, Z_BAR, COST, BOOT = 150, 40, 8, 1.96, 3.0, 2000
HYP = {"H1_flat": lambda f: max(abs(f["wr_d5_fast"]), abs(f["wr_d5_slow"])) <= 10.7,
       "H2_steep": lambda f: f["wr_d5_fast"] >= 30.0}


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
        pk = o.get("peak_price") or e
        rows.append({"d": d, "f": f, "ng": (pk - e) / e * 1e4 < 5, "bp": (x - e) / e * 1e4 - COST, "rng": f.get("rs_rng15_bp")})
    return rows


def z(k1, n1, k2, n2):
    if min(n1, n2) < 5:
        return float("nan")
    p = (k1 + k2) / (n1 + n2); se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (k1 / n1 - k2 / n2) / se if se else float("nan")


def read(rows, name, flag):
    days = sorted({r["d"] for r in rows}); mid = days[len(days) // 2]
    halves = {"A": [r for r in rows if r["d"] < mid], "B": [r for r in rows if r["d"] >= mid]}
    ok = True; under = False
    for lab, sub in halves.items():
        fl = [r for r in sub if flag(r["f"])]; rest = [r for r in sub if not flag(r["f"])]
        if len(fl) < MIN_FLAG or len({r["d"] for r in sub}) < MIN_DAYS:
            under = True
        good = bool(fl and rest and sum(r["ng"] for r in fl) / len(fl) > sum(r["ng"] for r in rest) / len(rest)
                    and sum(r["bp"] for r in rest) / len(rest) > sum(r["bp"] for r in sub) / len(sub))
        ok &= good
        print(f"  half {lab}: flagged n{len(fl)} NG {sum(r['ng'] for r in fl)/max(1,len(fl)):.0%} vs {sum(r['ng'] for r in rest)/max(1,len(rest)):.0%}; "
              f"kept net {sum(r['bp'] for r in rest)/max(1,len(rest)):+.1f} vs all {sum(r['bp'] for r in sub)/max(1,len(sub)):+.1f} -> {'ok' if good else 'NO'}")
    fl = [r for r in rows if flag(r["f"])]; rest = [r for r in rows if not flag(r["f"])]
    zz = z(sum(r["ng"] for r in fl), len(fl), sum(r["ng"] for r in rest), len(rest))
    rng = random.Random(1106); byday = {}
    for r in rows:
        byday.setdefault(r["d"], []).append(r)
    lows = []
    for _ in range(BOOT):
        s = [r for d in (rng.choice(days) for _ in days) for r in byday[d]]
        k = [r for r in s if not flag(r["f"])]
        if k and s:
            lows.append(sum(r["bp"] for r in k) / len(k) - sum(r["bp"] for r in s) / len(s))
    lo5 = sorted(lows)[int(len(lows) * 0.05)] if lows else float("nan")
    terc, rv = [], sorted(r["rng"] for r in rows if r["rng"] is not None)
    if len(rv) >= 30:
        t1, t2 = rv[len(rv) // 3], rv[2 * len(rv) // 3]
        for a, b in ((-1, t1), (t1, t2), (t2, 1e18)):
            g = [r for r in rows if r["rng"] is not None and a < r["rng"] <= b]
            f_, r_ = [r for r in g if flag(r["f"])], [r for r in g if not flag(r["f"])]
            if len(f_) >= 5 and len(r_) >= 5:
                terc.append(sum(x["ng"] for x in f_) / len(f_) - sum(x["ng"] for x in r_) / len(r_))
    conf_ok = (not terc) or sum(x > 0 for x in terc) >= 2
    verdict = "UNDERPOWERED" if under else ("PASS (skeptic review next)" if ok and zz >= Z_BAR and lo5 > 0 and conf_ok else "FAIL")
    print(f"  pooled flagged {len(fl)} / rest {len(rest)}; NG z {zz:+.2f} (bar {Z_BAR}); bootstrap 5th pct (unflagged - all) {lo5:+.2f} bp; "
          f"rng terciles {[round(x, 2) for x in terc] or 'n/a'}\n  {name}: {verdict}")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--since", default="2026-11-06")
    rows = load(ap.parse_args().since)
    print(f"entries with slope fields since the start date: {len(rows)} over {len({r['d'] for r in rows})} days")
    for n, fn in HYP.items():
        fl = [r for r in rows if fn(r["f"])]
        print(f"{n}: flagged {len(fl)} ({len(fl)/max(1,len(rows)):.0%})")
    if len(rows) < MIN_N:
        print(f"NOT DUE: needs {MIN_N} entries ({MIN_N - len(rows)} more)"); return 0
    for n, fn in HYP.items():
        print(n); read(rows, n, fn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
