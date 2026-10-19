#!/usr/bin/env python3
"""Score the turn-open sweep. Spec: docs/studies/turn_open_prereg.json.

Inputs: DIR/DAY_ARM.json from scripts/turn_open_sweep.sh (replay_session output, `closed` trades). Costing is the
exit sweep's (spread_rvol_score.arm_day: full SIP quoted spread at entry, charged once). Adds the halves, the
7 bp stop-slippage line, exit reasons, and the pre-registered words.
USAGE (mini, repo root, after 16:15 ET): .venv/bin/python tools/studies/turn_open_score.py --dir /tmp/turn_open_DATE
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
from collections import Counter

ROOT = os.getcwd()
sys.path[:0] = [os.path.join(ROOT, "tools", "studies"), os.path.join(ROOT, "tools"), ROOT]
import spread_rvol_score as S  # noqa: E402

ARMS = ("live", "turn5", "turn20")
TURNS = ("turn5", "turn20")
REF = "live"
HALF_A = ("2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09")
HALF_B = ("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16")
STOP_SLIP_BP = 7.0
MIN_DAYS, MIN_TRADES = 8, 100
T_PASS, T_PROMISING, T_VALUE = 2.0, 1.5, 1.5


def stop_exit(t: dict) -> bool:
    r = str(t.get("reason") or t.get("exit_reason") or "").lower()
    return any(k in r for k in ("stop", "trail", "ratchet"))


def word(net_a, net_b, net_all_t, val_a, val_b, val_t, days: int, trades: int) -> str:
    def pos(x):
        return x is not None and x > 0
    if days < MIN_DAYS or trades < MIN_TRADES:
        return "UNDERPOWERED"
    if (pos(net_a) and pos(net_b) and net_all_t is not None and net_all_t >= T_PASS
            and pos(val_a) and pos(val_b) and val_t is not None and val_t >= T_VALUE):
        return "PASS"
    if pos(net_a) and pos(net_b) and net_all_t is not None and net_all_t >= T_PROMISING:
        return "PROMISING"
    return "FAIL"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    a = ap.parse_args()
    import replay_costing as RC
    cache, box = RC.load_cache(), {}
    spread_of = lambda s, ts: RC.entry_spread_bp(cache, s, ts, box)  # noqa: E731
    days = sorted({os.path.basename(p)[:10] for p in glob.glob(os.path.join(a.dir, "20??-??-??_*.json"))})
    per = {arm: {} for arm in ARMS}
    slip = {arm: {} for arm in ARMS}
    reasons = {arm: Counter() for arm in ARMS}
    synth = Counter()
    incomplete = []
    for d in days:
        raw = {}
        for arm in ARMS:
            p = os.path.join(a.dir, f"{d}_{arm}.json")
            raw[arm] = json.load(open(p)) if os.path.exists(p) else None
        if any(v is None for v in raw.values()):
            incomplete.append(d)
            continue
        pooled = [s for v in raw.values() for t in (v.get("closed") or [])
                  if (s := spread_of(t["symbol"], float(t["entry_ts"]))) is not None]
        med = statistics.median(pooled) if pooled else None
        for arm in ARMS:
            closed = raw[arm].get("closed") or []
            day = S.arm_day(closed, spread_of, med)
            per[arm][d] = day
            n_stop = sum(1 for t in closed if t.get("ret") is not None and stop_exit(t))
            slip[arm][d] = None if day["net"] is None or not day["n"] else day["net"] - STOP_SLIP_BP * n_stop / day["n"]
            for t in closed:
                reasons[arm][str(t.get("reason") or t.get("exit_reason") or "?")] += 1
                if t.get("synthetic"):
                    synth[arm] += 1
    RC.save_cache(cache)
    scored = sorted(per[REF])
    print(f"days found {len(days)}; scored {len(scored)} {scored}; incomplete {incomplete}\n")
    f = lambda x: "—" if x is None else f"{x:+.1f}"  # noqa: E731
    g = lambda x: "—" if x is None else f"{x:+.2f}"  # noqa: E731
    print("| arm | slice | days | opens/day | opens/10m | conc | win | median hold s | net bp/trade (t) | net -7bp stops | value bp/day (t) |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    stats = {}
    for arm in ARMS:
        for tag, sl in (("all", scored), ("A", [d for d in scored if d in HALF_A]), ("B", [d for d in scored if d in HALF_B])):
            rows = [per[arm][d] for d in sl]
            if not rows:
                continue
            net = S.day_stat([r["net"] for r in rows if r["net"] is not None])
            val = S.day_stat([r["value"] for r in rows])
            sl7 = S.day_stat([slip[arm][d] for d in sl if slip[arm][d] is not None])
            stats[(arm, tag)] = {"net": net, "val": val, "days": len(rows), "trades": sum(r["n"] for r in rows)}
            win = [r["win"] for r in rows if r["win"] is not None]
            hold = [r["hold_med"] for r in rows if r["hold_med"] is not None]
            print(f"| {arm} | {tag} | {len(rows)} | {statistics.fmean(r['n'] for r in rows):.1f} | "
                  f"{statistics.fmean(r['per10'] for r in rows):.2f} | {statistics.fmean(r['conc'] for r in rows):.2f} | "
                  f"{(statistics.fmean(win) if win else 0):.0%} | {(statistics.median(hold) if hold else 0):.0f} | "
                  f"{f(net['mean'])} ({g(net['t'])}) | {f(sl7['mean'])} | {f(val['mean'])} ({g(val['t'])}) |")
    print("\nPaired VALUE vs live, bp/day:")
    words = []
    for arm in TURNS:
        diffs = {tag: S.day_stat([per[arm][d]["value"] - per[REF][d]["value"] for d in scored if tag == "all" or d in (HALF_A if tag == "A" else HALF_B)])
                 for tag in ("all", "A", "B")}
        for tag, st in diffs.items():
            print(f"  {arm} - live [{tag}]: {f(st['mean'])} (t {g(st['t'])}, {st['n']} days)")
        if (arm, "all") not in stats:
            words.append(f"{arm}: UNDERPOWERED")
            continue
        w = word(stats.get((arm, "A"), {}).get("net", {}).get("mean"), stats.get((arm, "B"), {}).get("net", {}).get("mean"),
                 stats[(arm, "all")]["net"]["t"], diffs["A"]["mean"], diffs["B"]["mean"], diffs["all"]["t"],
                 stats[(arm, "all")]["days"], stats[(arm, "all")]["trades"])
        words.append(f"{arm}: {w}")
    print("\nExit reasons:")
    for arm in ARMS:
        print(f"  {arm}: {dict(reasons[arm].most_common(8))}; synthetic-row opens {synth[arm]}")
    print("\n**" + " | ".join(words) + "**")


if __name__ == "__main__":
    main()
