#!/usr/bin/env python3
"""edge_d_effects.py — Round-1 strategy-edge study, part D (2026-09-26).

Published intraday effects on free IEX 1m bars, same 60-day window and IS/OOS
split (40/20) and the same spread model (full SIP spread at entry) as
edge_c_grid.py. Universe ALL = Claude's ~759 Stage-B names + the LIQ list,
prior close >= $20, >= 150 IEX RTH bars that day.

D1 intraday momentum: r1 = 09:30 open -> 10:00; at 10:00 go with sign(r1),
   exit 15:50. Groups: SPY, QQQ, LIQ list, TOPG = day's top 10 % gainers
   (prior close -> 10:00). Baselines: always long, same names and window.
D2 opening-range breakout, R = 5/15/30 min, on the top 10 % gainers at the end
   of the range: first 1m close above the OR high (long) / below the OR low
   (short) before 15:00; stop at the other side of the range (filled at the
   worse of stop and bar open), else exit 15:50. Baseline per trade: 5 random
   entries in the same name-day, same direction, same % stop distance, same exit.
D3 gaps >= 3% (open vs prior close): enter 09:35, exit 10:30 or 15:50;
   continuation (with the gap) vs fade. Baseline: same trades' direction on
   all names with |gap| < 1% that day.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import edge_common as ec  # noqa: E402
from edge_fetch import LIQUID  # noqa: E402

rng = np.random.default_rng(11)


def orb_exit(D, e, direction, stop):
    """Walk bars after e: stop at `stop`, else 15:50 close. Returns exit price."""
    for j in range(e + 1, 380):
        lo, hi, op = D.L[j], D.H[j], D.O[j]
        if np.isnan(lo):
            continue
        if direction > 0 and lo <= stop:
            return min(stop, op) if not np.isnan(op) else stop
        if direction < 0 and hi >= stop:
            return max(stop, op) if not np.isnan(op) else stop
    return D.Cf[379]


def main():
    days, _cur, _rows = ec.load_days()
    if os.environ.get("EDGE_MAXDAYS"):
        days = days[:int(os.environ["EDGE_MAXDAYS"])]
    sm = ec.SpreadModel()
    prev_files = sorted(f[:-4] for f in os.listdir(ec.BARDIR) if f.endswith(".pkl"))
    R = defaultdict(list)  # key -> list of (dayidx, gross, net, base_net)
    prevclose = {}
    pday = max([d for d in prev_files if d < days[0]] or [None])
    if pday:
        for s, B in ec.load_bars(pday).items():
            D = ec.Dense(B, pday)
            if D.nbars:
                prevclose[s] = D.last_close()
    for di, day in enumerate(days):
        bars = ec.load_bars(day)
        if not bars:
            continue
        dense = {}
        for s, B in bars.items():
            D = ec.Dense(B, day)
            if D.nbars:
                dense[s] = D
        newclose = {s: D.last_close() for s, D in dense.items()}
        elig = {}
        for s, D in dense.items():
            pc = prevclose.get(s)
            if not pc or pc < 20 or D.nbars < 150:
                continue
            first = np.where(~np.isnan(D.O[:3]))[0]
            if not len(first):
                continue
            elig[s] = (D, pc, D.O[first[0]])
        # ── D1 intraday momentum
        topg10 = sorted((s for s, (D, pc, op) in elig.items() if D.age[29] <= 5),
                        key=lambda s: -(elig[s][0].Cf[29] / elig[s][1]))[:10]
        groups = {"SPY": ["SPY"], "QQQ": ["QQQ"], "LIQ": LIQUID, "TOPG": topg10}
        for g, syms in groups.items():
            for s in syms:
                if s not in elig:
                    continue
                D, pc, op = elig[s]
                if D.age[29] > 5:
                    continue
                r1 = D.Cf[29] / op - 1
                gr = (D.Cf[379] / D.Cf[29] - 1) * 1e4
                spr = sm.get(s, day, 30, D.Cf[29])
                sg = 1 if r1 > 0 else -1
                R[f"D1 {g:4} long/short by first-30m sign"].append((di, sg * gr, sg * gr - spr, gr - spr))
                if sg > 0:
                    R[f"D1 {g:4} long only if first-30m up"].append((di, gr, gr - spr, gr - spr))
                R[f"D1 {g:4} baseline: always long"].append((di, gr, gr - spr, gr - spr))
        # ── D2 opening-range breakout on the day's top gainers
        for Rm in (5, 15, 30):
            cands = [s for s, (D, pc, op) in elig.items() if D.age[Rm - 1] <= 5 and D.Cf[Rm - 1] >= 20]
            top = sorted(cands, key=lambda s: -(elig[s][0].Cf[Rm - 1] / elig[s][1]))[:10]
            for s in top:
                D, pc, op = elig[s]
                orh, orl = np.nanmax(D.H[:Rm]), np.nanmin(D.L[:Rm])
                if not (orh > orl):
                    continue
                done = {1: False, -1: False}
                for e in range(Rm, 330):
                    c = D.C[e]
                    if np.isnan(c):
                        continue
                    for dr, cond, stop in ((1, c > orh, orl), (-1, c < orl, orh)):
                        if done[dr] or not cond:
                            continue
                        done[dr] = True
                        x = orb_exit(D, e, dr, stop)
                        gr = dr * (x / c - 1) * 1e4
                        spr = sm.get(s, day, e + 1, c)
                        dist = abs(stop / c - 1)
                        bl = []
                        pool = [k for k in range(Rm, 330) if not np.isnan(D.C[k])]
                        for k in rng.choice(pool, size=min(5, len(pool)), replace=False):
                            ck = D.C[k]
                            st = ck * (1 - dist) if dr > 0 else ck * (1 + dist)
                            xk = orb_exit(D, int(k), dr, st)
                            bl.append(dr * (xk / ck - 1) * 1e4 - sm.get(s, day, int(k) + 1, ck))
                        side = "long " if dr > 0 else "short"
                        R[f"D2 ORB{Rm:2d} {side}"].append((di, gr, gr - spr, float(np.mean(bl))))
                        R[f"D2 ORB{Rm:2d} long+short"].append((di, gr, gr - spr, float(np.mean(bl))))
                    if all(done.values()):
                        break
        # ── D3 gaps
        flat = [s for s, (D, pc, op) in elig.items() if abs(op / pc - 1) < 0.01 and D.age[4] <= 5]
        base = {}
        for ex, j in (("10:30", 59), ("15:50", 379)):
            xs = [(elig[s][0].Cf[j] / elig[s][0].Cf[4] - 1) * 1e4 - sm.get(s, day, 5, elig[s][0].Cf[4]) for s in flat]
            xs_s = [-(elig[s][0].Cf[j] / elig[s][0].Cf[4] - 1) * 1e4 - sm.get(s, day, 5, elig[s][0].Cf[4]) for s in flat]
            base[(ex, 1)] = float(np.mean(xs)) if xs else np.nan
            base[(ex, -1)] = float(np.mean(xs_s)) if xs_s else np.nan
        for s, (D, pc, op) in elig.items():
            gap = op / pc - 1
            if abs(gap) < 0.03 or D.age[4] > 5:
                continue
            sg = 1 if gap > 0 else -1
            spr = sm.get(s, day, 5, D.Cf[4])
            for ex, j in (("10:30", 59), ("15:50", 379)):
                gr = (D.Cf[j] / D.Cf[4] - 1) * 1e4
                lab = "up  " if sg > 0 else "down"
                R[f"D3 gap {lab} continue ->{ex}"].append((di, sg * gr, sg * gr - spr, base[(ex, sg)]))
                R[f"D3 gap {lab} fade     ->{ex}"].append((di, -sg * gr, -sg * gr - spr, base[(ex, -sg)]))
        prevclose.update({s: v for s, v in newclose.items() if v})
        print(f"  {day}: elig {len(elig)}", flush=True)
    n_is, n_oos = ec.N_IS, len(days) - ec.N_IS
    print(f"\nPART D — {days[0]}..{days[-1]}; net bp/trade = gross - full SIP spread at entry; "
          "'vs base' = net minus the baseline net for the same trades\n")
    for k in sorted(R):
        a = np.array(R[k], float)
        for part, m, nd in (("IS ", a[:, 0] < n_is, n_is), ("OOS", a[:, 0] >= n_is, n_oos)):
            if m.sum() < 5:
                print(f"  {k:40} {part} n {int(m.sum())}")
                continue
            s = ec.stats(a[m, 2], a[m, 0], gross=a[m, 1], n_days_total=nd)
            d = ec.stats(a[m, 2] - a[m, 3], a[m, 0])
            print(f"  {k:40} {part} {ec.fmt(s)}  | vs base {d['net']:+6.1f} (t {d['t']:+4.1f})")


if __name__ == "__main__":
    main()
