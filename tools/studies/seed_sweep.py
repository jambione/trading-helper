#!/usr/bin/env python3
"""Starting-stop sweep: seed stop % x trail-from-peak %, arm +0.3%, no 60s.

Same entry sets and walker as exit_sweep.py. Reported for all prices and for
entries >= $20 (tomorrow's floor). Net is after 0.11% (measured $20+ round
trip) and 0.20%.
"""
from __future__ import annotations

import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import exit_study as ex  # noqa: E402
import exit_sweep as sw  # noqa: E402
import runway_study as rs  # noqa: E402

SEEDS = (0.35, 0.5, 0.75, 1.0)
GIVES = (0.25, 0.35, 0.5)


def run(label, fills):
    days = sorted({f["day"] for f in fills})
    mid = days[len(days) // 2]
    print(f"\n== {label}: {len(fills)} entries (halves at {mid}) ==")
    print(f"  {'seed':>5}{'give':>6}{'gross':>9}{'±':>7}{'net.11':>9}{'net.20':>9}{'win':>6}"
          f"{'avg win':>9}{'avg loss':>9}{'full stop':>10}{'h1':>9}{'h2':>9}")
    best = []
    for seed in SEEDS:
        for give in GIVES:
            res = [ex.walk(f["B"], f["i0"], f["px"], stop_pct=seed, arm_pct=0.3,
                           trail_pct=give) for f in fills]
            g = [a for a, _ in res]
            w = [a for a in g if a > 0]
            lo = [a for a in g if a <= 0]
            full = sum(a <= -seed + 1e-6 for a in g) / len(g)
            h1 = rs.mean([a for a, f in zip(g, fills) if f["day"] < mid])
            h2 = rs.mean([a for a, f in zip(g, fills) if f["day"] >= mid])
            se = statistics.stdev(g) / len(g) ** .5
            best.append((rs.mean(g), seed, give))
            print(f"  {seed:>5.2f}{give:>6.2f}{rs.mean(g):>+8.3f}%{se:>7.3f}"
                  f"{rs.mean(g) - .11:>+8.3f}%{rs.mean(g) - .20:>+8.3f}%{len(w) / len(g):>6.0%}"
                  f"{(rs.mean(w) if w else 0):>+8.2f}%{(rs.mean(lo) if lo else 0):>+8.2f}%"
                  f"{full:>10.0%}{h1:>+9.3f}{h2:>+9.3f}")
    best.sort(reverse=True)
    print("  best gross:", ", ".join(f"seed {s} give {g} ({m:+.3f}%)" for m, s, g in best[:3]))


if __name__ == "__main__":
    fills = ex.load()
    mr = sw.midrise_entries()
    run("FILLS all prices", fills)
    run("FILLS >= $20", [f for f in fills if f["px"] >= 20])
    run("ONE-ARM all prices", mr)
    run("ONE-ARM >= $20", [f for f in mr if f["px"] >= 20])
