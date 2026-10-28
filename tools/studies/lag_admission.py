#!/usr/bin/env python3
"""lag_admission.py (DIAGNOSTIC, no pass/fail): two non-strategy delays in the turn->fill lag.

(1) Off-board minutes (lag_blockers' 'off_board'), time-weighted by what the admission ledger said about the name in
    that minute: the last proposal_ledger row (stage seed/inclusion, decision, reason) at or before each minute's end.
    'not proposed yet' = no row so far that day; 'kept, not seated' = the last row kept it.
(2) Watch-poll cadence for seated names: gaps between successive last_poll_ts of each symbol in the entry_watch_state
    snapshots, 09:45-15:30, all seated names (not only bought ones), per session.
USAGE (mini): cd tools/studies && ../../.venv/bin/python lag_admission.py
"""
from __future__ import annotations

import bisect
import gzip
import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import lag_blockers as LB

ET = ZoneInfo("America/New_York")


def proposals(day: str, syms: set) -> dict:
    out = defaultdict(list)
    for line in open(os.path.join(LB.SNAP, day, "proposal_ledger.jsonl")):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("symbol") in syms:
            out[r["symbol"]].append((float(r["ts"]), r.get("stage"), r.get("decision"), r.get("reason")))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


def poll_gaps(day: str) -> list[float]:
    last, gaps = {}, []
    with gzip.open(os.path.join(LB.SNAP, day, "state_snapshots.jsonl.gz"), "rt") as fh:
        for line in fh:
            if "entry_watch_state" not in line[:300]:
                continue
            d = json.loads(line)
            if d.get("file") != "ai_reports/entry_watch_state.json":
                continue
            for s, rec in (d.get("data") or {}).items():
                if not isinstance(rec, dict) or rec.get("last_poll_ts") is None:
                    continue
                t = float(rec["last_poll_ts"])
                lt = datetime.fromtimestamp(t, ET)
                m = lt.hour * 60 + lt.minute
                if not (585 <= m < 930):
                    continue
                p = last.get(s)
                if p is not None and t > p:
                    g = t - p
                    if g < 300:                # longer = the name left the board and came back
                        gaps.append(g)
                last[s] = t if p is None or t > p else p
    return gaps


def main() -> None:
    rows = [r for r in json.load(open(LB.IN)) if not r.get("skip") and r.get("fill_after_turn_s", -1) > 0]
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    why_min, total_off, all_gaps, day_gap = Counter(), 0.0, [], {}
    for day, rs in sorted(by_day.items()):
        syms = {r["sym"] for r in rs}
        idx = LB.board_index(day, syms)
        props = proposals(day, syms)
        for r in rs:
            snaps = idx.get(r["sym"], [])
            pr = props.get(r["sym"], [])
            pts = [p[0] for p in pr]
            t, end = r["turn_ts"], r["fill_ts"]
            while t < end:
                t1 = min(t + 60.0, end)
                w = (t1 - t) / 60.0
                if not any(t <= ts < t1 for ts, _ in snaps):
                    total_off += w
                    k = bisect.bisect_right(pts, t1) - 1
                    if k < 0:
                        why_min["not proposed yet"] += w
                    else:
                        _, stage, dec, why = pr[k]
                        why_min["kept, not seated" if dec == "kept" else f"{stage} dropped: {why}"] += w
                t = t1
        g = poll_gaps(day)
        all_gaps += g
        if g:
            day_gap[day] = statistics.median(g)
    print(f"off-board minutes {total_off:.0f} over {len(rows)} buys ({total_off / len(rows):.1f}/buy); by the last admission row:")
    for k, m in why_min.most_common(14):
        print(f"  {m / total_off:6.1%}  {k}")
    q = sorted(all_gaps)
    if q:
        print(f"watch poll gap per seated name, 09:45-15:30: median {statistics.median(q):.1f} s, p75 {q[int(.75 * len(q))]:.1f}, "
              f"p90 {q[int(.9 * len(q))]:.1f}, p99 {q[int(.99 * len(q))]:.1f} (n {len(q)})")
        print("  per-session median:", {d[5:]: round(v, 1) for d, v in sorted(day_gap.items())})


if __name__ == "__main__":
    main()
