#!/usr/bin/env python3
"""cf_score.py — counterfactual replays scored with std_scorer (INFORMATION ONLY: these sessions are seen; no verdicts).

For each variant vs a reference over the given days: same-entry paired difference (std_scorer.paired_variants), per-day dollar
difference at $1k per trade (empty days = $0), trades per day, and net per trade. All trades and tight-source trades separately.
Net = replay ret minus the full SIP spread at entry (replay_costing); no NBBO = that day's pooled median across variants.
USAGE (mini): .venv/bin/python tools/studies/cf_score.py --dir /tmp/cf --ref base --vars np_lob_st np_lob_nodecay both_sq DAY [DAY ...]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

ROOT = os.getcwd()
sys.path[:0] = [os.path.join(ROOT, "tools", "studies"), os.path.join(ROOT, "tools"), ROOT]
import replay_costing as RC  # noqa: E402
import std_scorer as S  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/tmp/cf")
    ap.add_argument("--ref", default="base")
    ap.add_argument("--vars", nargs="+", required=True)
    ap.add_argument("days", nargs="+")
    a = ap.parse_args()
    first = {}
    for line in open(os.path.join(ROOT, "ai_reports", "admit_range.jsonl")):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        k = (r.get("day"), r.get("symbol"))
        if k[0] in a.days and r.get("ts") is not None and (k not in first or float(r["ts"]) < first[k][0]):
            first[k] = (float(r["ts"]), r.get("source"))
    cache, box = RC.load_cache(), {}
    names = [a.ref] + a.vars
    trades = {v: [] for v in names}
    missing = []
    for d in a.days:
        rows, spreads = {}, []
        for v in names:
            p = os.path.join(a.dir, f"{d}-{v}.json")
            if not os.path.exists(p):
                missing.append(f"{d}-{v}")
                rows[v] = []
                continue
            rows[v] = []
            for t in json.load(open(p)).get("closed") or []:
                if t.get("ret") is None:
                    continue
                spr = RC.entry_spread_bp(cache, t["symbol"], t["entry_ts"], box)
                if spr is not None:
                    spreads.append(spr)
                rows[v].append((t, spr))
        med = statistics.median(spreads) if spreads else None
        for v in names:
            for t, spr in rows[v]:
                if spr is None:
                    if med is None:
                        continue
                    spr = med
                f = first.get((d, t["symbol"]))
                trades[v].append({"day": d, "symbol": t["symbol"], "entry_ts": float(t["entry_ts"]), "net_bp": t["ret"] * 1e4 - spr,
                                  "tight": bool(f and f[1] == "tight" and f[0] <= float(t["entry_ts"])), "reason": t.get("reason")})
    RC.save_cache(cache)
    print(f"COUNTERFACTUALS (information only; seen sessions) {a.days[0]}..{a.days[-1]} ({len(a.days)} days), reference '{a.ref}'")
    if missing:
        print(f"  missing replays: {missing}")
    for scope in ("all", "tight"):
        print(f"\n  [{scope} trades]")
        sel = lambda xs: xs if scope == "all" else [x for x in xs if x["tight"]]  # noqa: E731
        ref = sel(trades[a.ref])
        print(f"  {a.ref:16s} trades {len(ref):5d} ({len(ref) / len(a.days):5.1f}/day) net {statistics.fmean([x['net_bp'] for x in ref]) if ref else float('nan'):+6.2f} bp/trade "
              f"${sum(x['net_bp'] for x in ref) / 1e4 * 1000 / len(a.days):+7.2f}/day")
        for v in a.vars:
            xs = sel(trades[v])
            r = S.paired_variants(ref, xs, a.days)
            print(f"  {v:16s} trades {len(xs):5d} ({len(xs) / len(a.days):5.1f}/day) net {statistics.fmean([x['net_bp'] for x in xs]) if xs else float('nan'):+6.2f} bp/trade "
                  f"${sum(x['net_bp'] for x in xs) / 1e4 * 1000 / len(a.days):+7.2f}/day | same-entry diff {r['paired']['mean']:+6.2f} bp (t {r['paired']['t']:+.2f}, "
                  f"{r['matched']} matched) | $/day diff {r['dollars']['mean']:+7.2f} (t {r['dollars']['t']:+.2f})")


if __name__ == "__main__":
    main()
