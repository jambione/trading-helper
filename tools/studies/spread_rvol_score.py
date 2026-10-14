#!/usr/bin/env python3
"""Score the spread cap x arm RVOL sweep. Spec: docs/studies/spread_rvol_sweep_prereg.json.

Inputs: DIR/DAY_ARM.json from scripts/spread_rvol_sweep.sh (replay_session output, `closed` trades).
Costing: net bp = ret x 1e4 - full SIP quoted spread at entry (replay_costing.entry_spread_bp, charged once,
as hist_sim_read); no NBBO -> that day's pooled median across arms (counted).
Per arm and day: opens, opens per 10 minutes of the 09:45-15:50 window, mean concurrency (sum of hold
seconds / window seconds), net bp per trade, and VALUE = sum of net bp that day (proportional to dollars at a
fixed position size). Day-clustered means and paired t vs the reference arms.
USAGE (mini, repo root, after 16:15 ET - quote fetches): .venv/bin/python tools/studies/spread_rvol_score.py --dir /tmp/srs_DATE
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

ARMS = ("s05r125", "s15r125", "s25r125", "s05r06", "s15r06", "s25r06", "s15r095")
REFS = ("s05r125", "s15r06")      # the 10/13 live setting and the 10/14 mid-session setting
WINDOW_SEC = (15 * 60 + 50 - (9 * 60 + 45)) * 60
OPENS_BAR = 1.0                   # book open standard: >= 1 open per 10 minutes


def day_stat(xs: list[float]) -> dict:
    n = len(xs)
    if n == 0:
        return {"mean": None, "t": None, "n": 0}
    mu = statistics.fmean(xs)
    if n < 3:
        return {"mean": mu, "t": None, "n": n}
    sd = statistics.stdev(xs)
    return {"mean": mu, "t": (mu / (sd / n ** 0.5)) if sd > 0 else None, "n": n}


def arm_day(closed: list[dict], spread_of, med: float | None) -> dict:
    nets, hold, miss = [], 0.0, 0
    for t in closed:
        if t.get("ret") is None:
            continue
        s = spread_of(t["symbol"], float(t["entry_ts"]))
        if s is None:
            miss += 1
            s = med
        if s is None:
            continue
        nets.append(t["ret"] * 1e4 - s)
        if t.get("exit_ts"):
            hold += max(0.0, float(t["exit_ts"]) - float(t["entry_ts"]))
    return {"n": len(nets), "per10": len(nets) / (WINDOW_SEC / 600), "conc": hold / WINDOW_SEC,
            "net": statistics.fmean(nets) if nets else None, "value": sum(nets), "no_nbbo": miss}


def pick(summary: dict) -> str | None:
    """Pre-registered: the arm with the highest mean daily VALUE among arms averaging >= 1 open / 10 min."""
    ok = [a for a, s in summary.items() if s["per10"] is not None and s["per10"] >= OPENS_BAR and s["value"]["mean"] is not None]
    return max(ok, key=lambda a: summary[a]["value"]["mean"]) if ok else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    a = ap.parse_args()
    import replay_costing as RC
    cache, box = RC.load_cache(), {}
    spread_of = lambda s, ts: RC.entry_spread_bp(cache, s, ts, box)  # noqa: E731
    days = sorted({os.path.basename(p)[:10] for p in glob.glob(os.path.join(a.dir, "20??-??-??_*.json"))})
    per = {arm: {} for arm in ARMS}
    incomplete, vacuous = [], []
    for d in days:
        raw = {}
        for arm in ARMS:
            p = os.path.join(a.dir, f"{d}_{arm}.json")
            raw[arm] = json.load(open(p)) if os.path.exists(p) else None
        if any(v is None for v in raw.values()):
            incomplete.append(d)
            continue
        if all(not (v.get("closed") or []) for v in raw.values()):
            vacuous.append(d)
            continue
        pooled = [s for v in raw.values() for t in (v.get("closed") or [])
                  if (s := spread_of(t["symbol"], float(t["entry_ts"]))) is not None]
        med = statistics.median(pooled) if pooled else None
        for arm in ARMS:
            per[arm][d] = arm_day(raw[arm].get("closed") or [], spread_of, med)
    RC.save_cache(cache)
    scored = sorted(set(per[ARMS[0]]))
    print(f"days found {len(days)}; scored {len(scored)} {scored}; incomplete {incomplete}; vacuous {vacuous}\n")
    summary = {}
    print("| arm | opens/day | opens/10m | concurrency | net bp/trade (t) | value bp/day (t) | no NBBO |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for arm in ARMS:
        rows = [per[arm][d] for d in scored]
        if not rows:
            continue
        nets = [r["net"] for r in rows if r["net"] is not None]
        s = {"n": statistics.fmean(r["n"] for r in rows), "per10": statistics.fmean(r["per10"] for r in rows),
             "conc": statistics.fmean(r["conc"] for r in rows), "net": day_stat(nets),
             "value": day_stat([r["value"] for r in rows]), "miss": sum(r["no_nbbo"] for r in rows)}
        summary[arm] = s
        f = lambda x: "—" if x is None else f"{x:+.1f}"  # noqa: E731
        g = lambda x: "—" if x is None else f"{x:+.2f}"  # noqa: E731
        print(f"| {arm} | {s['n']:.1f} | {s['per10']:.2f} | {s['conc']:.2f} | {f(s['net']['mean'])} ({g(s['net']['t'])}) "
              f"| {f(s['value']['mean'])} ({g(s['value']['t'])}) | {s['miss']} |")
    print("\nPaired per-day VALUE differences vs the references (t >= 2 needed to call one better):")
    for ref in REFS:
        for arm in ARMS:
            if arm == ref or arm not in summary or ref not in summary:
                continue
            st = day_stat([per[arm][d]["value"] - per[ref][d]["value"] for d in scored])
            print(f"  {arm} - {ref}: {st['mean']:+.1f} bp/day (t {st['t'] if st['t'] is None else round(st['t'], 2)}, {st['n']} days)")
    best = pick(summary)
    print(f"\nPRE-REGISTERED PICK (highest mean daily value among arms with >= {OPENS_BAR} open / 10 min): {best}")
    print("A pick whose paired value vs s15r06 has t < 2 is a SUGGESTION, not evidence; the live change is the operator's call.")


if __name__ == "__main__":
    main()
