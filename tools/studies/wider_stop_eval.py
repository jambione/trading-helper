#!/usr/bin/env python3
"""wider_stop_eval.py — the pre-registered verdict for docs/studies/wider_stop_prereg.json (5eb0044).

Reads the tick simulator's rows (one per entry window; desk rows carry the live trade's own net as desk_pnl) and
compares, per desk trade with entry >= $10 on the held-out days, the simulated exit's net with the live exit's net.
PASS: PRIMARY (0.25% initial stop + 0.35% trail, print trigger) paired improvement >= +3 bp, day-clustered
t >= 2, n >= 100.

USAGE:  python3 tools/studies/wider_stop_eval.py /tmp/skeptic_ws/rows.jsonl
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict

PRIMARY = "0.25%|pct35|print"
REPORTED = ("0.50%|pct35|print", "1c|last1c|print")


def dct(pairs):
    by = defaultdict(list)
    for d, v in pairs:
        by[d].append(v)
    dm = [statistics.mean(v) for v in by.values()]
    t = (statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm)))
         if len(dm) > 2 and statistics.stdev(dm) > 0 else float("nan"))
    return statistics.mean(v for _, v in pairs), t, len(pairs), len(by)


def main():
    rows = [json.loads(line) for line in open(sys.argv[1])]
    desk = [r for r in rows if r["src"] == "desk" and r["ask0"] >= 10 and "desk_pnl" in r]
    rand = [r for r in rows if r["src"] == "rand" and r["ask0"] >= 10]
    days = sorted({r["day"] for r in desk})
    print(f"WIDER STOP (held out {days[0]}..{days[-1]}): {len(desk)} desk trades >= $10, {len(rand)} random windows\n")
    live = dct([(r["day"], r["desk_pnl"] * 1e4) for r in desk])
    print(f"  live exits:                 net {live[0]:+6.1f} bp (t {live[1]:+.2f})")
    verdict = None
    for k in (PRIMARY,) + REPORTED:
        sim = [(r["day"], r["v"][k]["net"] * 1e4) for r in desk if k in r["v"]]
        diff = [(r["day"], (r["v"][k]["net"] - r["desk_pnl"]) * 1e4) for r in desk if k in r["v"]]
        rnd = [(r["day"], r["v"][k]["net"] * 1e4) for r in rand if k in r["v"]]
        s, d, rr = dct(sim), dct(diff), dct(rnd)
        win = sum(1 for r in desk if k in r["v"] and r["v"][k]["net"] > 0) / max(1, len(sim))
        hold = statistics.median(r["v"][k]["hold"] for r in desk if k in r["v"])
        tag = "PRIMARY " if k == PRIMARY else "reported"
        print(f"  {tag} {k:<20} net {s[0]:+6.1f} (t {s[1]:+.2f})  vs live {d[0]:+6.1f} bp (t {d[1]:+.2f}, n {d[2]}, "
              f"{d[3]} days)  win {win:.0%}  med hold {hold:.0f}s  | random {rr[0]:+6.1f} (t {rr[1]:+.2f})")
        if k == PRIMARY:
            verdict = d[0] >= 3 and d[1] >= 2 and d[2] >= 100
    print(f"\nPRE-REGISTERED VERDICT (paired improvement >= +3 bp, t >= 2, n >= 100): {'PASS' if verdict else 'FAIL'}")


if __name__ == "__main__":
    main()
