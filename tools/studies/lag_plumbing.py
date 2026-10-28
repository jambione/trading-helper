#!/usr/bin/env python3
"""lag_plumbing.py (DIAGNOSTIC, no pass/fail): three non-strategy delays, on recorded sessions 9/28-10/15.

(A) Watch poll loop: polls are the distinct 'pt' stamps on recorder_wire rows. Per poll (09:45-15:30): gap to the next
    poll, Alpaca REST calls made, their summed duration 'dt', the span from pt to the last call's end, 429 errors.
(B) Arm -> broker submit: per BUY submit (fills.jsonl), the poll it happened in (latest pt <= submit), seconds from that
    poll's start to the submit, and from the last arm_ok decision row to the submit.
(C) Admitted, not seated: lag_admission's 'kept, not seated' minutes for bought names; at each such minute, the latest
    book_server_shadow.jsonl row: was the name ranked, in would_symbols (would seat), its rank, n_would_seat.
USAGE (mini): cd tools/studies && ../../.venv/bin/python lag_plumbing.py
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

import lag_admission as LA
import lag_blockers as LB

ET = ZoneInfo("America/New_York")


def q(xs, lab):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return f"{lab}: n 0"
    return (f"{lab}: median {statistics.median(xs):.1f}, p75 {xs[int(.75 * len(xs))]:.1f}, "
            f"p90 {xs[int(.9 * len(xs))]:.1f}, n {len(xs)}")


def rth(t: float, lo: int = 585, hi: int = 930) -> bool:
    d = datetime.fromtimestamp(t, ET)
    return lo <= d.hour * 60 + d.minute < hi


def wire_polls(day: str):
    polls = defaultdict(lambda: {"n": 0, "dt": 0.0, "end": 0.0, "e429": 0})
    path = os.path.join(LB.SNAP, day, "recorder_wire.jsonl.gz")
    if not os.path.exists(path):
        return {}
    with gzip.open(path, "rt") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            pt = r.get("pt")
            if pt is None or r.get("ch") != "alpaca":
                continue
            p = polls[float(pt)]
            p["n"] += 1
            dt = float(r.get("dt") or 0.0)
            p["dt"] += dt
            p["end"] = max(p["end"], float(r.get("ts") or pt) + dt)
            if "429" in str(r.get("err") or "") or "too many" in str(r.get("err") or "").lower():
                p["e429"] += 1
    return polls


def main() -> None:
    rows = [r for r in json.load(open(LB.IN)) if not r.get("skip") and r.get("fill_after_turn_s", -1) > 0]
    days = sorted({r["day"] for r in rows})
    gaps, ncalls, sumdt, span, e429 = [], [], [], [], 0
    into_poll, ok_to_sub, kept_rank = [], [], Counter()
    ranks, n_seat = [], []
    for day in days:
        polls = wire_polls(day)
        pts = sorted(p for p in polls if rth(p))
        for a, b in zip(pts, pts[1:]):
            if b - a < 300:
                gaps.append(b - a)
        for p in pts:
            v = polls[p]
            ncalls.append(v["n"])
            sumdt.append(v["dt"])
            span.append(max(0.0, v["end"] - p))
            e429 += v["e429"]
        allpts = sorted(polls)
        # (B)
        okr = defaultdict(list)
        for line in open(os.path.join(LB.SNAP, day, "decision_ledger.jsonl")):
            x = json.loads(line)
            if x.get("stage") == "arm" and x.get("arm_ok"):
                okr[x["symbol"]].append(float(x["ts"]))
        for line in open(os.path.join(LB.SNAP, day, "fills.jsonl")):
            x = json.loads(line)
            if x.get("event") != "submit" or str(x.get("action")).upper() != "BUY":
                continue
            ts = float(x["ts"])
            if not rth(ts):
                continue
            k = bisect.bisect_right(allpts, ts) - 1
            if k >= 0 and ts - allpts[k] < 120:
                into_poll.append(ts - allpts[k])
            ok = max((t for t in okr.get(x["symbol"], []) if t <= ts), default=None)
            if ok is not None and ts - ok < 120:
                ok_to_sub.append(ts - ok)
        # (C)
        rs = [r for r in rows if r["day"] == day]
        syms = {r["sym"] for r in rs}
        idx = LB.board_index(day, syms)
        props = LA.proposals(day, syms)
        bs = []
        bpath = os.path.join(LB.SNAP, day, "book_server_shadow.jsonl")
        if os.path.exists(bpath):
            for line in open(bpath):
                try:
                    b = json.loads(line)
                except ValueError:
                    continue
                bs.append((float(b["ts"]), b))
        bts = [b[0] for b in bs]
        for r in rs:
            snaps = idx.get(r["sym"], [])
            pr = props.get(r["sym"], [])
            pts_p = [p[0] for p in pr]
            t, end = r["turn_ts"], r["fill_ts"]
            while t < end:
                t1 = min(t + 60.0, end)
                if not any(t <= s < t1 for s, _ in snaps):
                    k = bisect.bisect_right(pts_p, t1) - 1
                    if k >= 0 and pr[k][2] == "kept":
                        j = bisect.bisect_right(bts, t1) - 1
                        if j < 0:
                            kept_rank["no book-server row"] += 1
                        else:
                            b = bs[j][1]
                            top = [x.get("symbol") for x in (b.get("top") or [])]
                            n_seat.append(int(b.get("n_would_seat") or 0))
                            if r["sym"] in (b.get("would_symbols") or []):
                                kept_rank["would seat (seat lag)"] += 1
                            elif r["sym"] in top:
                                kept_rank["ranked, below the seat cut"] += 1
                                ranks.append(top.index(r["sym"]) + 1)
                            else:
                                kept_rank["not in the ranked list"] += 1
                t = t1
    print("(A) watch poll loop, 09:45-15:30")
    print("  " + q(gaps, "seconds between polls"))
    print("  " + q(span, "seconds from poll start to its last Alpaca call ending"))
    print("  " + q(sumdt, "seconds of Alpaca call time per poll"))
    print("  " + q(ncalls, "Alpaca calls per poll") + f"; 429s total {e429}")
    print("(B) arm -> broker submit")
    print("  " + q(into_poll, "seconds from poll start to the submit"))
    print("  " + q(ok_to_sub, "seconds from last arm_ok row to the submit"))
    print("(C) admitted, not seated (minutes, bought names)")
    tot = sum(kept_rank.values())
    for k, n in kept_rank.most_common():
        print(f"  {n / tot:6.1%}  {k}")
    if ranks:
        print("  " + q(ranks, "rank when below the cut") + "; " + q(n_seat, "seats the book server would fill"))


if __name__ == "__main__":
    main()
