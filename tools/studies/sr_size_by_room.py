#!/usr/bin/env python3
"""Size by room to resistance: half-size when room < 0.40%, full when wider.
Pre-registration: docs/studies/sr_size_by_room_prereg.json (committed BEFORE this run).

Sample: E square arms, indicator_levels rows.json + ext.pkl.
Room: % to nearest charted resistance bottom above price (order_blocks charted 3); none => 0.
Primary: w=0.5 if room < 0.40% else 1.0. Control: w=1. Contribution = w * return.
Pass: both chrono halves: mean(w*gross)-mean(gross) > mean(w)*20 bp, n>=50. Report net.
Run: .venv/bin/python tools/studies/sr_size_by_room.py
"""
from __future__ import annotations

import collections
import json
import math
import os
import pickle
import statistics
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import order_blocks as OB  # noqa: E402
from order_block_gate import series  # noqa: E402

ET = ZoneInfo("America/New_York")
IND = os.path.join(ROOT, "ai_reports", "indicator_levels")
OUT = os.path.join(ROOT, "ai_reports", "sr_size_by_room")
COST_BP = 20.0  # 0.20% in bp
FLOOR = 0.40


def is_resist(b):
    return (b.kind == "bear" and not b.breaker) or (b.kind == "bull" and b.breaker)


def tstat(xs, dd):
    if len(xs) < 2:
        return float("nan"), float("nan")
    m = statistics.fmean(xs)
    by = collections.defaultdict(float)
    for v, d in zip(xs, dd):
        by[d] += v - m
    G, n = len(by), len(xs)
    if G < 2:
        return m, float("nan")
    se = math.sqrt(sum(s * s for s in by.values()) * G / (G - 1)) / n
    return m, (m / se if se and se == se and se > 0 else float("nan"))


def main():
    os.makedirs(OUT, exist_ok=True)
    R = json.load(open(os.path.join(IND, "rows.json")))
    days = list(R["days"])
    mid = len(days) // 2
    half = {d: ("A" if k < mid else "B") for k, d in enumerate(days)}
    cache = pickle.load(open(os.path.join(IND, "ext.pkl"), "rb"))
    cal = sorted({d for (_, d) in cache})
    prev_of = {d: cal[cal.index(d) - 1] if cal.index(d) else None for d in cal}

    arms = [r for r in R["rows"] if r["pop"] in ("E", "F") and r.get("net15") is not None and r.get("px")]
    by = collections.defaultdict(list)
    for r in arms:
        by[(r["sym"], r["day"])].append(r)

    # attach room
    miss = collections.Counter()
    for (sym, day), rs_ in by.items():
        rows = series(cache, sym, day, prev_of.get(day))
        if len(rows) < 50:
            for r in rs_:
                r["room"] = None
            miss["short_series"] += len(rs_)
            continue
        states = list(OB.order_blocks(rows, bar_sec=60))
        closes = [rows[i][0] + 60 for i, _ in states]
        for r in rs_:
            tq = float(r["t"])
            px = float(r["px"])
            j = int(np.searchsorted(closes, tq, side="right")) - 1
            if j < 0:
                r["room"] = None
                miss["no_bar"] += 1
                continue
            ch = OB.charted([b for b in states[j][1] if b.known_ts <= tq])
            above = [b.btm for b in ch if is_resist(b) and b.btm > px]
            r["room"] = 0.0 if not above else (min(above) / px - 1) * 100

    def score(pop, floor, half_wt):
        """half_wt: weight when room < floor (0.5 primary, 0.0 = skip)."""
        xs = [r for r in arms if r["pop"] == pop and r.get("room") is not None]
        rows_out = []
        for r in xs:
            w = half_wt if r["room"] < floor else 1.0
            g = (r["net15"] + 0.0020) * 1e4  # reconstruct gross bp from net15
            n = r["net15"] * 1e4
            rows_out.append({
                "sym": r["sym"], "day": r["day"], "room": r["room"], "w": w,
                "gross": g, "net": n,
                "c_gross": w * g, "c_net": w * n,
            })
        return rows_out

    primary = score("E", FLOOR, 0.5)
    info_skip = score("E", FLOOR, 0.0)
    info_60 = score("E", 0.60, 0.5)
    info_f = score("F", FLOOR, 0.5)

    json.dump({
        "primary": primary, "miss": dict(miss),
        "days": days, "half_A": days[:mid], "half_B": days[mid:],
        "prereg": "docs/studies/sr_size_by_room_prereg.json",
    }, open(os.path.join(OUT, "rows.json"), "w"))

    L = [
        "# Size by room (prereg docs/studies/sr_size_by_room_prereg.json)",
        "",
        f"sample days {len(days)} ({days[0]}..{days[-1]}); chrono A n_days {mid}; B {len(days)-mid}",
        f"PRIMARY E half-size@{FLOOR}%: arms {len(primary)}; miss {dict(miss)}",
        "",
    ]
    res = {}

    def summarize(label, rows, store_primary=False):
        for hh in ("A", "B", "all"):
            tr = [t for t in rows if hh == "all" or half[t["day"]] == hh]
            if len(tr) < 5:
                L.append(f"- {label} half {hh}: {len(tr)} (too few)")
                if store_primary:
                    res[hh] = None
                continue
            mw = statistics.fmean([t["w"] for t in tr])
            sg = statistics.fmean([t["c_gross"] for t in tr])
            cg = statistics.fmean([t["gross"] for t in tr])
            sn = statistics.fmean([t["c_net"] for t in tr])
            cn = statistics.fmean([t["net"] for t in tr])
            lift = sg - cg
            rt = mw * COST_BP
            under = sum(t["w"] < 1.0 - 1e-12 for t in tr) / len(tr)
            lm, lt = tstat([t["c_gross"] - t["gross"] for t in tr], [t["day"] for t in tr])
            L.append(
                f"- {label} half {hh}: n {len(tr)} on {len({t['day'] for t in tr})} days | "
                f"mean w {mw:.3f} | under-floor {under:.0%} | "
                f"sized gross {sg:+.1f} / net {sn:+.1f} | "
                f"equal gross {cg:+.1f} / net {cn:+.1f} | "
                f"lift gross {lift:+.1f} bp (t {lt:+.2f}) | "
                f"measured RT {rt:.1f} bp | lift>RT? {lift > rt} | "
                f"median room {statistics.median(t['room'] for t in tr):.2f}%"
            )
            if store_primary:
                res[hh] = (len(tr), lift, rt, sn, cn)

    summarize("PRIMARY E w=0.5@0.40%", primary, True)
    summarize("INFO E skip@0.40%", info_skip)
    summarize("INFO E w=0.5@0.60%", info_60)
    summarize("INFO F w=0.5@0.40%", info_f)

    if any(res.get(h) is None or res[h][0] < 50 for h in ("A", "B")):
        v = "UNDERPOWERED (fewer than 50 arms in a half): no verdict"
    elif all(res[h][1] > res[h][2] for h in ("A", "B")):
        v = "PASS — PENDING SKEPTIC REVIEW (not a live size change; needs IEX confirmation)"
    else:
        v = "FAIL"

    L.append("")
    L.append(
        f"**PRIMARY: {v}** (bar, both chronological halves: sized−equal gross > mean(w)×20 bp, "
        f"n >= 50; also report net)"
    )
    if res.get("A") and res.get("B"):
        for h in ("A", "B"):
            n, lift, rt, sn, cn = res[h]
            L.append(f"Half {h}: n {n}, lift {lift:+.1f} bp vs RT {rt:.1f} bp, sized net {sn:+.1f} / equal net {cn:+.1f} bp")

    open(os.path.join(OUT, "report.md"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
