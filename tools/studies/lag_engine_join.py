#!/usr/bin/env python3
"""lag_engine_join.py (DIAGNOSTIC, no pass/fail): for each bought name, how long after it passed admission did it
first appear in the signal engine (signal_state.json 'tickers'), and how full was the engine then.

Admission = first proposal_ledger row with stage 'inclusion' and decision 'kept' that day at or before the fill.
Engine join = first signal_state snapshot at or after admission whose 'tickers' contains the name (names already in
the engine at admission count as 0). Engine size = number of 'tickers' in that snapshot. Push cap at the time:
ai_watch_engine_push_max = 30 in the recorded config (push room = cap - engine size).
USAGE (mini): cd tools/studies && ../../.venv/bin/python lag_engine_join.py
"""
from __future__ import annotations

import bisect
import gzip
import json
import os
import statistics
from collections import Counter, defaultdict

import lag_blockers as LB

CAP = 30


def main() -> None:
    rows = [r for r in json.load(open(LB.IN)) if not r.get("skip") and r.get("fill_after_turn_s", -1) > 0]
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    waits, waits_turn, size_at_admit, full_share, already = [], [], [], Counter(), 0
    for day, rs in sorted(by_day.items()):
        syms = {r["sym"] for r in rs}
        kept = defaultdict(list)
        for line in open(os.path.join(LB.SNAP, day, "proposal_ledger.jsonl")):
            p = json.loads(line)
            if p.get("symbol") in syms and p.get("stage") == "inclusion" and p.get("decision") == "kept":
                kept[p["symbol"]].append(float(p["ts"]))
        snaps = []
        with gzip.open(os.path.join(LB.SNAP, day, "state_snapshots.jsonl.gz"), "rt") as fh:
            for line in fh:
                if '"signal_state.json"' not in line[:300]:
                    continue
                d = json.loads(line)
                tk = (d.get("data") or {}).get("tickers") or {}
                names = set(tk.keys()) if isinstance(tk, dict) else {str(x.get("ticker") or x.get("symbol")) for x in tk}
                snaps.append((float(d["ts"]), names))
        snaps.sort(key=lambda x: x[0])
        sts = [s[0] for s in snaps]
        for s, n in Counter(len(x[1]) >= CAP for x in snaps if True).items():
            full_share[s] += n
        for r in rs:
            ks = sorted(t for t in kept.get(r["sym"], []) if t <= r["fill_ts"])
            if not ks:
                continue
            a = ks[0]
            k = bisect.bisect_left(sts, a)
            if k >= len(snaps):
                continue
            size_at_admit.append(len(snaps[k][1]))
            if r["sym"] in snaps[k][1]:
                already += 1
                waits.append(0.0)
                continue
            j = next((i for i in range(k, len(snaps)) if r["sym"] in snaps[i][1] and snaps[i][0] <= r["fill_ts"]), None)
            if j is None:
                continue
            waits.append((snaps[j][0] - a) / 60)
            if snaps[j][0] > r["turn_ts"]:
                waits_turn.append((snaps[j][0] - max(a, r["turn_ts"])) / 60)
    q = sorted(waits)
    print(f"bought names with an admission row: {len(q)}; already in the engine at admission: {already}")
    print(f"admission -> engine join (min): median {statistics.median(q):.1f}, mean {statistics.fmean(q):.1f}, "
          f"p75 {q[int(.75 * len(q))]:.1f}, p90 {q[int(.9 * len(q))]:.1f}; joined > 2 min after admission {sum(w > 2 for w in q) / len(q):.0%}")
    if waits_turn:
        print(f"  of which after the %R turn (min/buy over all buys): {sum(waits_turn) / len(rows):.2f}; buys affected {len(waits_turn)}")
    s = sorted(size_at_admit)
    print(f"engine size when the name passed admission: median {statistics.median(s):.0f}, >= {CAP} (push room 0) in {sum(x >= CAP for x in s) / len(s):.0%}")
    tot = sum(full_share.values())
    print(f"all engine snapshots that day-set with size >= {CAP}: {full_share[True] / tot:.0%}")


if __name__ == "__main__":
    main()
