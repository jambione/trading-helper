#!/usr/bin/env python3
"""No-extension time stop: sell at the bar close if the trail has not armed
(+0.3%) within N minutes. Everything else is tomorrow's exit: 1% seed stop,
arm +0.3%, 0.35% from peak, 60m cap. Same walker order as exit_study.walk.

Net subtracts 0.071% (the spread-gated one-arm round trip) and 0.11%.
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


def walk_ts(B, i0, px, tstop_min=None, seed=1.0, arm=0.3, give=0.35, horizon=60):
    t, o, h, l, c, _v = B
    stop = px * (1 - seed / 100)
    hi, armed = px, False
    end = t[i0] + horizon * 60
    last = i0
    for k in range(i0 + 1, len(t)):
        if t[k] > end:
            break
        last = k
        if l[k] <= stop:
            return (min(stop, o[k]) / px - 1) * 100, "stop"
        hi = max(hi, h[k])
        if not armed and hi >= px * (1 + arm / 100):
            armed = True
        if armed:
            stop = max(stop, hi * (1 - give / 100))
        elif tstop_min is not None and t[k] - t[i0] >= tstop_min * 60:
            return (c[k] / px - 1) * 100, "time"
    return (c[last] / px - 1) * 100, "cap"


def run(label, fills):
    days = sorted({f["day"] for f in fills})
    mid = days[len(days) // 2]
    print(f"\n== {label}: {len(fills)} entries (halves at {mid}) ==")
    print(f"  {'time stop':<14}{'gross':>9}{'±':>7}{'net .071':>10}{'net .11':>9}{'win':>6}"
          f"{'full 1% stops':>15}{'time exits':>12}{'h1':>9}{'h2':>9}")
    base = None
    for ts in (None, 5, 10, 15, 20, 30):
        res = [walk_ts(f["B"], f["i0"], f["px"], ts) for f in fills]
        g = [a for a, _ in res]
        why = [w for _, w in res]
        h1 = rs.mean([a for a, f in zip(g, fills) if f["day"] < mid])
        h2 = rs.mean([a for a, f in zip(g, fills) if f["day"] >= mid])
        full = sum(a <= -1.0 + 1e-6 for a in g) / len(g)
        lab = "none (live)" if ts is None else f"{ts} min"
        m = rs.mean(g)
        if ts is None:
            base = (m, h1, h2)
        print(f"  {lab:<14}{m:>+8.3f}%{statistics.stdev(g) / len(g) ** .5:>7.3f}"
              f"{m - .071:>+9.3f}%{m - .11:>+8.3f}%{sum(a > 0 for a in g) / len(g):>6.0%}"
              f"{full:>15.0%}{why.count('time') / len(g):>12.0%}{h1:>+9.3f}{h2:>+9.3f}"
              + ("" if ts is None else f"   Δ {m - base[0]:+.3f} (h1 {h1 - base[1]:+.3f}, h2 {h2 - base[2]:+.3f})"))


if __name__ == "__main__":
    mr = [f for f in sw.midrise_entries() if f["px"] >= 20]
    run("ONE-ARM >= $20 (tomorrow's entries)", mr)
    run("REAL FILLS >= $20", [f for f in ex.load() if f["px"] >= 20])
