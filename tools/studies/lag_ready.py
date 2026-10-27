#!/usr/bin/env python3
"""lag_ready.py (DIAGNOSTIC, no pass/fail): what the desk was doing in the turn->fill minutes where the bought name had
NO block_code (lag_blockers.py's 'ready', 14% of the lag). Inputs: ~/replay_runs/lag_breakdown.json and the recorded
session_snapshots (entry_watch_state, ai_positions_state, decision_ledger, fills).

Each such minute is split by the snapshot's own fields:
  order_out       status 'submitted' (an order for the name is out)
  arm_ready       status 'watching', arm_ready True (the arm says go, no order yet)
  no_verdict      status 'watching', arm_ready not True (no blocker stamped, no go either)
For no_verdict minutes, the decision-ledger arm rows of that minute (what the arm actually said) are tallied.
For every minute, open positions and open orders from the nearest ai_positions_state snapshot are recorded.
Per buy: submit -> fill seconds and the order type from fills.jsonl.
USAGE (mini): .venv/bin/python tools/studies/lag_ready.py
"""
from __future__ import annotations

import bisect
import gzip
import json
import os
import statistics
from collections import Counter, defaultdict

SNAP = os.path.expanduser("~/session_snapshots")
IN = os.path.expanduser("~/replay_runs/lag_breakdown.json")
MAX_POS = 5


def day_data(day: str, syms: set):
    ew, pos = defaultdict(list), []
    with gzip.open(os.path.join(SNAP, day, "state_snapshots.jsonl.gz"), "rt") as fh:
        for line in fh:
            head = line[:300]
            if "entry_watch_state" in head:
                d = json.loads(line)
                if d.get("file") != "ai_reports/entry_watch_state.json":
                    continue
                for s, rec in (d.get("data") or {}).items():
                    if s in syms and isinstance(rec, dict):
                        ew[s].append((float(rec.get("last_poll_ts") or d["ts"]), rec.get("block_code"),
                                      rec.get("status"), rec.get("arm_ready")))
            elif '"ai_positions_state.json"' in head:
                d = json.loads(line)
                p = (d.get("data") or {})
                if isinstance(p, dict):
                    pos.append((float(d["ts"]), len(p.get("positions") or {}), len(p.get("open_orders") or [])))
    for v in ew.values():
        v.sort(key=lambda x: x[0])
    pos.sort(key=lambda x: x[0])
    arms = defaultdict(list)
    for line in open(os.path.join(SNAP, day, "decision_ledger.jsonl")):
        r = json.loads(line)
        if r.get("stage") == "arm" and r.get("symbol") in syms:
            arms[r["symbol"]].append((float(r["ts"]), "ok" if r.get("arm_ok") else str(r.get("arm_why"))))
    sub = defaultdict(list)
    for line in open(os.path.join(SNAP, day, "fills.jsonl")):
        r = json.loads(line)
        if r.get("event") == "submit" and str(r.get("action")).upper() == "BUY":
            sub[r["symbol"]].append((float(r["ts"]), str(r.get("note") or "")[:40], r.get("order_type") or r.get("type")))
    return ew, pos, arms, sub


def main() -> None:
    rows = [r for r in json.load(open(IN)) if not r.get("skip") and r.get("fill_after_turn_s", -1) > 0]
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    kind_min, nv_arm, full_slots, submit_to_fill, notes, n_ready_min = Counter(), Counter(), Counter(), [], Counter(), 0.0
    per_buy_kind = []
    for day, rs in sorted(by_day.items()):
        ew, pos, arms, sub = day_data(day, {r["sym"] for r in rs})
        pos_ts = [p[0] for p in pos]
        for r in rs:
            snaps = ew.get(r["sym"], [])
            t, end = r["turn_ts"], r["fill_ts"]
            mine = Counter()
            while t < end:
                t1 = min(t + 60.0, end)
                w = (t1 - t) / 60.0
                win = [s for s in snaps if t <= s[0] < t1]
                codes = Counter(s[1] or "ready" for s in win)
                if win and codes.most_common(1)[0][0] == "ready":
                    ready = [s for s in win if not s[1]]
                    st = Counter("order_out" if s[2] == "submitted" else ("arm_ready" if s[3] is True else "no_verdict")
                                 for s in ready).most_common(1)[0][0]
                    kind_min[st] += w
                    mine[st] += w
                    n_ready_min += w
                    if st == "no_verdict":
                        for ts, why in arms.get(r["sym"], []):
                            if t <= ts < t1:
                                nv_arm[why] += 1
                    k = bisect.bisect_right(pos_ts, t1) - 1
                    if k >= 0:
                        full_slots["slots full (>= 5 open)" if pos[k][1] >= MAX_POS else "slot free"] += w
                t = t1
            per_buy_kind.append(mine)
            s_before = [s for s in sub.get(r["sym"], []) if s[0] <= end]
            if s_before:
                submit_to_fill.append(end - s_before[-1][0])
                notes[s_before[-1][1]] += 1
    print(f"buys {len(per_buy_kind)}; 'ready' minutes {n_ready_min:.0f} ({n_ready_min / len(per_buy_kind):.1f} per buy)")
    for k, m in kind_min.most_common():
        print(f"  {k:10s} {m / n_ready_min:6.1%}  {m / len(per_buy_kind):4.2f} min/buy  "
              f"buys with any {sum(1 for b in per_buy_kind if b.get(k, 0) > 0) / len(per_buy_kind):4.0%}")
    tot_nv = sum(nv_arm.values())
    print("  arm rows during no_verdict minutes (what the arm said):")
    for k, n in nv_arm.most_common(10):
        print(f"    {k:26s} {n / tot_nv:6.1%}")
    tot_sl = sum(full_slots.values())
    print("  slot state during 'ready' minutes:", {k: f"{v / tot_sl:.0%}" for k, v in full_slots.items()})
    if submit_to_fill:
        q = sorted(submit_to_fill)
        print(f"  submit -> fill seconds: median {statistics.median(q):.1f}, p90 {q[int(0.9 * len(q))]:.1f}, max {q[-1]:.0f}")
    print("  submit notes:", notes.most_common(6))


if __name__ == "__main__":
    main()
