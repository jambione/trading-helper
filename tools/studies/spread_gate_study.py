#!/usr/bin/env python3
"""Would a spread gate make the one-arm entries net positive?

Entries: one-arm events (fast %R -50 cross, slow rising) after admission,
>= $20, 09:40-15:30, 2026-09-14..23 (entry_screen population), exited with
tomorrow's exits (1% seed, arm +0.3%, 0.35% from peak, 60m cap).

Per entry, two SIP quotes:
  spread_now    (ask-bid)/mid at the entry minute's close — the true cost
  spread_seen   the same 16 minutes earlier — what a live gate can query
                (the plan serves SIP quotes once they are 15 minutes old)
Net = gross exit return - spread_now (crossing twice at half-spread each).
Grouped by spread_seen, the only thing a live gate could act on.
"""
from __future__ import annotations

import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import entry_screen as es  # noqa: E402
import exec_report as xr  # noqa: E402
import exit_study as ex  # noqa: E402
import runway_study as rs  # noqa: E402

DAYS = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
        "2026-09-21", "2026-09-22", "2026-09-23"]


def main():
    from config import load_config
    cfg = load_config()
    first: dict = {}
    want = es.admitted(DAYS, first)
    for d in DAYS:
        want[d].add("SPY")
    ext = es.fetch_ext(want)
    cl = bars.client()
    rows = []
    for d, syms in sorted(want.items()):
        spy = ext.get(("SPY", d))
        for s in sorted(syms):
            if s == "SPY":
                continue
            df = ext.get((s, d))
            if df is None or len(df) < 150:
                continue
            ind = es.indicators(df, cfg, spy)
            B = es.to_B(df)
            t_on = first.get((s, d))
            last = -1e18
            for i in range(2, len(B[0]) - 1):
                m = ind["mins"][i]
                if m < 9 * 60 + 40 or m > 15 * 60 + 30 or B[4][i] < 20:
                    continue
                if t_on is None or ind["ts"][i] < t_on or ind["ts"][i] - last < es.COOL:
                    continue
                if "mid_rise" not in es.fire(ind, i):
                    continue
                last = ind["ts"][i]
                t_close = datetime.fromtimestamp(B[0][i] + 59, timezone.utc)
                q_now = xr.nbbo_at(cl, s, t_close)
                time.sleep(0.25)
                q_seen = xr.nbbo_at(cl, s, t_close - timedelta(minutes=16))
                time.sleep(0.25)
                if not q_now or not q_seen:
                    continue
                sp = lambda q: (q[1] - q[0]) / ((q[1] + q[0]) / 2) * 100
                g, _ = ex.walk(B, i, B[4][i], stop_pct=1.0, arm_pct=0.3, trail_pct=0.35)
                rows.append({"sym": s, "day": d, "gross": g, "now": sp(q_now),
                             "seen": sp(q_seen), "net": g - sp(q_now)})
        print(f"  {d}: {len(rows)} entries so far", file=sys.stderr)

    print(f"ONE-ARM ENTRIES >= $20 with true SIP spreads: {len(rows)}\n")
    nows = [r["now"] for r in rows]
    print(f"  spread at entry: median {statistics.median(nows):.3f}%  "
          f"mean {statistics.mean(nows):.3f}%  p90 {rs.q(nows, .9):.3f}%")
    seen, now = [r["seen"] for r in rows], [r["now"] for r in rows]
    # does the 16-min-old spread predict the one we will pay?
    tight_seen = [r for r in rows if r["seen"] <= 0.05]
    print(f"  of names with 16-min-old spread <= 0.05%, spread at entry <= 0.10%: "
          f"{sum(r['now'] <= 0.10 for r in tight_seen) / max(1, len(tight_seen)):.0%}\n")
    print(f"  {'gate (16-min-old spread)':<28}{'n':>5}{'gross':>9}{'cost':>8}{'net':>9}"
          f"{'±':>7}{'win':>6}{'h1 net':>9}{'h2 net':>9}")
    h1 = set(DAYS[:4])
    for lab, lim in (("no gate", 99), ("<= 0.20%", 0.20), ("<= 0.10%", 0.10),
                     ("<= 0.06%", 0.06), ("<= 0.04%", 0.04), ("<= 0.03%", 0.03)):
        sel = [r for r in rows if r["seen"] <= lim]
        if len(sel) < 10:
            continue
        n = [r["net"] for r in sel]
        a = [r["net"] for r in sel if r["day"] in h1]
        b = [r["net"] for r in sel if r["day"] not in h1]
        print(f"  {lab:<28}{len(sel):>5}{rs.mean([r['gross'] for r in sel]):>+8.3f}%"
              f"{rs.mean([r['now'] for r in sel]):>7.3f}%{rs.mean(n):>+8.3f}%"
              f"{statistics.stdev(n) / len(n) ** .5:>7.3f}{sum(x > 0 for x in n) / len(n):>6.0%}"
              f"{(rs.mean(a) if a else float('nan')):>+9.3f}{(rs.mean(b) if b else float('nan')):>+9.3f}")


if __name__ == "__main__":
    main()
