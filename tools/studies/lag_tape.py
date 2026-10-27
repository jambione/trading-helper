#!/usr/bin/env python3
"""lag_tape.py (DIAGNOSTIC, no pass/fail): stream/tape state of bought names in their off-board and tape-blocked
minutes between the %R turn and the fill (minute labels exactly as tools/studies/lag_blockers.py).

Per labelled minute, from the recording:
  recorder_prints.jsonl.gz  rows for the symbol in that minute: present or not, src (finnhub / other), age_sec of the
                            last print the desk saw (median of the minute's rows)
  signal_state.json         realtime_tape.subscribed (count) from the nearest snapshot: is the 50-subscription cap full,
                            and is the symbol in realtime_tape.silent
Tape-blocked = block codes in lag_blockers.GROUPS['plumbing'].
USAGE (mini): .venv/bin/python tools/studies/lag_tape.py
"""
from __future__ import annotations

import bisect
import gzip
import json
import os
import statistics
from collections import Counter, defaultdict

import lag_blockers as LB

SNAP = LB.SNAP
CAP = 50


def day_tape(day: str, syms: set):
    prints = defaultdict(list)
    path = os.path.join(SNAP, day, "recorder_prints.jsonl.gz")
    if os.path.exists(path):
        with gzip.open(path, "rt") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("symbol") in syms:
                    prints[r["symbol"]].append((float(r["ts"]), str(r.get("src") or "none"), r.get("age_sec")))
    subs = []
    with gzip.open(os.path.join(SNAP, day, "state_snapshots.jsonl.gz"), "rt") as fh:
        for line in fh:
            if '"signal_state.json"' not in line[:300]:
                continue
            d = json.loads(line)
            rt = (d.get("data") or {}).get("realtime_tape") or {}
            if rt:
                subs.append((float(d["ts"]), int(rt.get("subscribed") or 0), set(rt.get("silent") or [])))
    for v in prints.values():
        v.sort(key=lambda x: x[0])
    subs.sort(key=lambda x: x[0])
    return prints, subs


def main() -> None:
    rows = [r for r in json.load(open(LB.IN)) if not r.get("skip") and r.get("fill_after_turn_s", -1) > 0]
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    stats = {k: {"min": 0.0, "with_prints": 0.0, "src": Counter(), "ages": [], "cap_full": 0.0, "cap_45": 0.0,
                 "silent": 0.0, "subs": []} for k in ("off_board", "tape_blocked")}
    for day, rs in sorted(by_day.items()):
        syms = {r["sym"] for r in rs}
        idx = LB.board_index(day, syms)
        prints, subs = day_tape(day, syms)
        sub_ts = [s[0] for s in subs]
        for r in rs:
            snaps = idx.get(r["sym"], [])
            t, end = r["turn_ts"], r["fill_ts"]
            while t < end:
                t1 = min(t + 60.0, end)
                w = (t1 - t) / 60.0
                codes = Counter(c for ts, c in snaps if t <= ts < t1)
                code = codes.most_common(1)[0][0] if codes else "off_board"
                kind = "off_board" if code == "off_board" else ("tape_blocked" if LB.group_of(code) == "plumbing" else None)
                if kind:
                    s = stats[kind]
                    s["min"] += w
                    pm = [p for p in prints.get(r["sym"], []) if t <= p[0] < t1]
                    if pm:
                        s["with_prints"] += w
                        s["src"][Counter(p[1] for p in pm).most_common(1)[0][0]] += w
                        ages = [float(p[2]) for p in pm if p[2] is not None]
                        if ages:
                            s["ages"].append(statistics.median(ages))
                    k = bisect.bisect_right(sub_ts, t1) - 1
                    if k >= 0:
                        n_sub, silent = subs[k][1], subs[k][2]
                        s["subs"].append(n_sub)
                        s["cap_full"] += w if n_sub >= CAP else 0
                        s["cap_45"] += w if n_sub >= 45 else 0
                        s["silent"] += w if r["sym"] in silent else 0
                t = t1
    for kind, s in stats.items():
        m = s["min"]
        if not m:
            continue
        ages = sorted(s["ages"])
        print(f"== {kind}: {m:.0f} minutes ({m / len(rows):.1f} per buy)")
        print(f"  minutes with any recorded print for the name: {s['with_prints'] / m:.0%}")
        print(f"  print source (minutes with prints): {{{', '.join(f'{k}: {v / max(s['with_prints'], 1e-9):.0%}' for k, v in s['src'].most_common())}}}")
        if ages:
            print(f"  last-print age seen by the desk: median {statistics.median(ages):.0f} s, p75 {ages[int(.75 * len(ages))]:.0f} s, "
                  f"> 15 s {sum(a > 15 for a in ages) / len(ages):.0%}, > 60 s {sum(a > 60 for a in ages) / len(ages):.0%}")
        if s["subs"]:
            print(f"  stream subscriptions at the time: median {statistics.median(s['subs']):.0f}, max {max(s['subs'])}; "
                  f"cap full (>= {CAP}) {s['cap_full'] / m:.0%} of minutes, >= 45 {s['cap_45'] / m:.0%}; "
                  f"name in the stream's silent list {s['silent'] / m:.0%}")


if __name__ == "__main__":
    main()
