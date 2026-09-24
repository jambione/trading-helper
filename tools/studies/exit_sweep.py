#!/usr/bin/env python3
"""Capture sweep: arm level x trail-from-peak x 60s rule.

Two entry sets, same exit walker (tools/studies/exit_study.walk):
  FILLS    the desk's real fills (outcomes.jsonl) on RTH bars
  MIDRISE  fast %R -50 crosses (slow rising) after admission, 09-14..09-22,
           on extended-hours bars — what the one-arm entry would have taken

kept   = sum(exit return) / sum(best move in 30m), a capture ratio
net    = mean exit return - 0.20% round trip
h1/h2  = net in each date half (must agree before a setting is trusted)
"""
from __future__ import annotations

import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import entry_screen as es  # noqa: E402
import exit_study as ex  # noqa: E402
import runway_study as rs  # noqa: E402

COST = 0.20
ARMS = (0.2, 0.3, 0.5)
GIVES = (0.25, 0.35, 0.5, 0.75, 1.0)


def midrise_entries():
    from config import load_config
    cfg = load_config()
    days = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17",
            "2026-09-18", "2026-09-21", "2026-09-22"]
    first: dict = {}
    want = es.admitted(days, first)
    for d in days:
        want[d].add("SPY")
    cache = es.fetch_ext(want)
    out = []
    for day, syms in want.items():
        spy = cache.get(("SPY", day))
        for s in syms:
            if s == "SPY":
                continue
            df = cache.get((s, day))
            if df is None or len(df) < 150:
                continue
            ind = es.indicators(df, cfg, spy)
            B = es.to_B(df)
            t_on = first.get((s, day))
            last = -1e18
            for i in range(2, len(B[0]) - 2):
                m = ind["mins"][i]
                if m < 9 * 60 + 40 or m > 15 * 60 + 30 or B[4][i] < es.MIN_PX:
                    continue
                if t_on is None or ind["ts"][i] < t_on or ind["ts"][i] - last < es.COOL:
                    continue
                if "mid_rise" in es.fire(ind, i):
                    last = ind["ts"][i]
                    out.append({"B": B, "i0": i, "px": B[4][i], "day": day})
    return out


def sweep(label, fills):
    days = sorted({f["day"] for f in fills})
    mid = days[len(days) // 2]
    up30 = []
    for f in fills:
        s = rs.score_path(f["B"], f["i0"], f["px"])
        up30.append(s["up_30"] if s and s.get("up_30") is not None else None)
    print(f"\n== {label}: {len(fills)} entries, {days[0]}..{days[-1]} (halves at {mid}) ==")
    print(f"  {'arm':>5}{'give':>6}{'60s':>5}{'net':>9}{'±':>7}{'win':>6}{'kept':>7}"
          f"{'avg win':>9}{'avg loss':>9}{'h1':>9}{'h2':>9}{'hold m':>8}")
    rows = []
    for sixty in (False, True):
        for arm in ARMS:
            for give in GIVES:
                res = [ex.walk(f["B"], f["i0"], f["px"], arm_pct=arm, trail_pct=give,
                               sixty=sixty) for f in fills]
                g = [a for a, _ in res]
                pairs = [(a, u) for a, u in zip(g, up30) if u is not None and u > 0]
                kept = sum(a for a, _ in pairs) / sum(u for _, u in pairs) if pairs else float("nan")
                w = [a for a in g if a > 0]
                lo = [a for a in g if a <= 0]
                h1 = [a - COST for a, f in zip(g, fills) if f["day"] < mid]
                h2 = [a - COST for a, f in zip(g, fills) if f["day"] >= mid]
                rows.append((rs.mean(g) - COST, arm, give, sixty))
                print(f"  {arm:>5.2f}{give:>6.2f}{'yes' if sixty else 'no':>5}"
                      f"{rs.mean(g) - COST:>+8.3f}%{statistics.stdev(g) / len(g) ** .5:>7.3f}"
                      f"{len(w) / len(g):>6.0%}{kept:>7.0%}"
                      f"{(rs.mean(w) if w else 0):>+8.2f}%{(rs.mean(lo) if lo else 0):>+8.2f}%"
                      f"{rs.mean(h1):>+9.3f}{rs.mean(h2):>+9.3f}"
                      f"{statistics.median([b for _, b in res]):>8.0f}")
    live = [ex.walk(f["B"], f["i0"], f["px"], arm_pct=0.3, chase=True, sixty=True) for f in fills]
    lg = [a for a, _ in live]
    print(f"  LIVE NOW (arm .3, chase to last-1c, 60s): net {rs.mean(lg) - COST:+.3f}%  "
          f"win {sum(a > 0 for a in lg) / len(lg):.0%}")
    best = sorted(rows, reverse=True)[:3]
    print("  best by net:", ", ".join(f"arm {a} give {g} 60s={'y' if s else 'n'} ({n:+.3f}%)"
                                     for n, a, g, s in best))


if __name__ == "__main__":
    sweep("FILLS (desk's real entries)", ex.load())
    sweep("MIDRISE (one-arm entries, after admission)", midrise_entries())
