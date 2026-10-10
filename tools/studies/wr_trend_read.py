#!/usr/bin/env python3
"""wr_trend_read.py — docs/studies/wr_trend_entry_prereg.json. Scored with tools/studies/std_scorer.py.

Sessions from 2026-10-12 (9/26-10/9 seen, excluded). Per session: (wr_trend15 net bp/trade) - (base net bp/trade), and
(wr_trend15) - (wr_rand); sessions weighted equally; t = std_scorer.day_stat. A session needs >= 5 wr_trend15 trades (else
excluded, counted). A session's wr_rand leg is invalid when its trade count is not within +-15% of wr_trend15's (counted).
Net = replay ret - full SIP spread at entry; no NBBO = that session's pooled median across the three variants.
Vacuous session: any file missing, wr_trend15 identical to base, or no quotes. No verdict before 30 scored sessions.
USAGE (mini, after the nightly replay): .venv/bin/python tools/studies/wr_trend_read.py [--restart-from DAY]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys

ROOT = os.getcwd()
sys.path[:0] = [os.path.join(ROOT, "tools", "studies"), os.path.join(ROOT, "tools"), ROOT]
import replay_costing as RC  # noqa: E402
import std_scorer as S  # noqa: E402

FROM, N_READ, N_MAX = "2026-10-12", 30, 40
VARS = ("base", "wr_trend15", "wr_rand")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--restart-from", default=FROM, help="first session of a new live-entry regime (stratification rule)")
    a = ap.parse_args()
    start = max(FROM, a.restart_from)
    days = sorted({os.path.basename(p)[:10] for p in glob.glob("/tmp/tt_run/20??-??-??-wr_trend15.json") if os.path.basename(p)[:10] >= start})
    cache, box = RC.load_cache(), {}
    c = {"vacuous": [], "too_few_wr15": [], "rand_invalid": [], "no_nbbo_pooled": 0}
    rows = []
    totals = {v: 0 for v in VARS}
    for d in days:
        raw = {}
        for v in VARS:
            p = f"/tmp/tt_run/{d}-{v}.json"
            raw[v] = json.load(open(p)).get("closed") if os.path.exists(p) else None
        if any(raw[v] is None for v in VARS) or [(t["symbol"], t["entry_ts"]) for t in raw["wr_trend15"]] == \
                [(t["symbol"], t["entry_ts"]) for t in raw["base"]]:
            c["vacuous"].append(d)
            continue
        sp, per = [], {v: [] for v in VARS}
        for v in VARS:
            for t in raw[v]:
                if t.get("ret") is None:
                    continue
                s_ = RC.entry_spread_bp(cache, t["symbol"], t["entry_ts"], box)
                if s_ is not None:
                    sp.append(s_)
                per[v].append((t["ret"] * 1e4, s_))
        if not sp:
            c["vacuous"].append(d)
            continue
        med = statistics.median(sp)
        net = {}
        for v in VARS:
            xs = []
            for g, s_ in per[v]:
                if s_ is None:
                    c["no_nbbo_pooled"] += 1
                    s_ = med
                xs.append(g - s_)
            net[v] = xs
            totals[v] += len(xs)
        if len(net["wr_trend15"]) < 5:
            c["too_few_wr15"].append(d)
            continue
        m = {v: (statistics.fmean(net[v]) if net[v] else float("nan")) for v in VARS}
        rand_ok = net["wr_rand"] and abs(len(net["wr_rand"]) - len(net["wr_trend15"])) <= 0.15 * len(net["wr_trend15"])
        if not rand_ok:
            c["rand_invalid"].append(d)
        rows.append({"day": d, "vs_base": m["wr_trend15"] - m["base"], "vs_rand": (m["wr_trend15"] - m["wr_rand"]) if rand_ok else None,
                     "n": {v: len(net[v]) for v in VARS}})
    RC.save_cache(cache)
    n = len(rows)
    vb = S.day_stat([r["vs_base"] for r in rows])
    vr = S.day_stat([r["vs_rand"] for r in rows if r["vs_rand"] is not None])
    print(f"%R trend entry read from {start}: scored sessions {n} (read at {N_READ}, max {N_MAX}); trades {totals}; {c}")
    print(f"  wr_trend15 - base {vb['mean']:+.2f} bp/trade (t {vb['t']:+.2f}, n {vb['n']}); wr_trend15 - wr_rand {vr['mean']:+.2f} (t {vr['t']:+.2f}, n {vr['n']})")
    if n < N_READ:
        print(f"  direction only (no verdict before {N_READ} scored sessions)")
        if len(days) >= N_MAX:
            print(f"  VERDICT: UNDERPOWERED = FAIL ({n} scored by {len(days)} sessions)")
        return
    pos_share = sum(r["vs_base"] > 0 for r in rows) / n
    ok = (vb["mean"] >= 3 and vb["t"] >= 2 and pos_share >= 0.6 and vr["mean"] > 0 and vr["t"] >= 2
          and totals["base"] >= 300 and totals["wr_trend15"] >= 150)
    print(f"  positive sessions {pos_share:.0%} | VERDICT: {'PASS (skeptic review next; then a paper switch-on)' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
