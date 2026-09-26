#!/usr/bin/env python3
"""edge2_study.py — Round-2 strategy-edge study (2026-09-26), liquid names only.

Universe: edge2_fetch.U2 (LIQ list + SPY QQQ IWM DIA + 11 sector ETFs); a stock is
eligible on a day if its PRIOR-day median SIP spread <= 3 bp; the 15 ETFs always.
Split (pre-registered in ai_reports/edge2_split.json before any analysis):
tune 150 days (2025-09-29..2026-05-04), validate 50, holdout 50 (2026-07-17..09-25).
Cost: full SIP spread at entry (round-1 model, re-fitted on U2 samples only);
hedge leg adds SPY's spread. Stats: day-clustered t, day-bootstrap 95% CI.

R  short-term reversal: decisions every 15 min; lookback L = 15 or 30 min with the
   window starting >= 09:45; exit after H = 30/60/120 min, by 15:50.
     dec  cross-sectional: long bottom decile of r_L, short top decile
     z2   r_L / (prior-day 1m vol * sqrt(L)): long z <= -2, short z >= +2
   variants long-only, short-only, long/short; each raw and SPY-hedged.
   'vs rnd' = net minus the same-side (same-hedge) mean net of ALL eligible names at
   the same decision time and hold.
G  Gao et al. intraday momentum on the 15 ETFs: r1 = prior close->10:00, r1o =
   09:30->10:00, r12 = 15:00->15:30; trade 15:30 -> 16:00 (15:59 bar close) or
   15:30 -> 15:50 in the direction of the predictor; conditions: all days,
   high-vol (first-30m realized vol in the top third of its own trailing 20 days),
   high-volume (first-30m IEX volume, same rule). Benchmark: always long.
O  overnight vs intraday: prior 15:59 close -> 09:30 open (first IEX print) vs
   open -> 15:59 close; cost = half the spread at each end.

USAGE  edge2_study.py            tune + validate only (holdout never printed)
       edge2_study.py holdout    holdout for the promoted cells in ai_reports/edge2_promoted.json
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import edge_common as ec  # noqa: E402
from edge2_fetch import ETFS, OUT, SPLIT, U2  # noqa: E402

PROMOTED = os.path.join(ROOT, "ai_reports", "edge2_promoted.json")
HS = (30, 60, 120)


def load(day):
    import pickle
    p = os.path.join(OUT, f"{day}.pkl")
    return pickle.load(open(p, "rb")) if os.path.exists(p) else {}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "tv"
    reg = json.load(open(SPLIT))
    days = reg["tune"] + reg["validate"] + reg["holdout"]
    if os.environ.get("EDGE_SMOKE"):  # smoke test: a few tune days + a few validate days, no output files
        days = reg["tune"][:6] + reg["validate"][:6]
    part = {d: "T" for d in reg["tune"]}
    part.update({d: "V" for d in reg["validate"]})
    part.update({d: "H" for d in reg["holdout"]})
    sm = ec.SpreadModel(set(U2))
    print(f"spread model (U2 only): {sm.n_obs} samples, {len(sm.sd)} sym-days; profile "
          + " ".join(f"{k}:{v:.2f}" for k, v in sorted(sm.prof.items())))
    cells = defaultdict(list)  # name -> [(dayidx, part, net, gross, base)]
    prev = {}
    for s, B in load(reg["prior_day"]).items():
        prev[s] = ec.Dense(B, reg["prior_day"])
    hist = defaultdict(list)  # (sym, 'vol'|'volu') -> trailing values
    prevday = reg["prior_day"]
    elig_counts = []
    for di, day in enumerate(days):
        P = part[day]
        if mode != "holdout" and P == "H":
            break
        bars = load(day)
        D = {s: ec.Dense(B, day) for s, B in bars.items() if len(B[0]) > 100}
        elig = []
        for s, d in D.items():
            if s not in prev:
                continue
            pc = prev[s].Cf[389]
            if np.isnan(pc):
                continue
            ps = sm.sd.get((s, prevday), sm.sym.get(s, 99))
            if s in ETFS or ps <= 3.0:
                elig.append(s)
        elig_counts.append(len(elig))
        spr = lambda s, T, px=None: sm.get(s, day, T, px)  # noqa: E731
        # ── R reversal
        vol = {}
        for s in elig:
            c = prev[s].Cf
            r = np.diff(c[~np.isnan(c)]) / c[~np.isnan(c)][:-1]
            vol[s] = float(np.std(r)) if len(r) > 100 else np.nan
        spy = D.get("SPY")
        for L in (15, 30):
            for T in range(15 + L, 381 - 30, 15):
                e = T - 1
                rs = {}
                for s in elig:
                    d = D[s]
                    if d.age[e] <= 2 and d.age[e - L] <= 5:
                        rs[s] = d.Cf[e] / d.Cf[e - L] - 1
                if len(rs) < 30 or spy is None:
                    continue
                syms = list(rs)
                rv = np.array([rs[s] for s in syms])
                lo_c, hi_c = np.percentile(rv, [10, 90])
                for H in HS:
                    if T + H > 380:
                        continue
                    g = np.array([(D[s].Cf[e + H] / D[s].Cf[e] - 1) * 1e4 for s in syms])
                    sp = np.array([spr(s, T, D[s].Cf[e]) for s in syms])
                    gs = (spy.Cf[e + H] / spy.Cf[e] - 1) * 1e4
                    ss = spr("SPY", T)
                    base = {("long", 0): np.mean(g - sp), ("short", 0): np.mean(-g - sp),
                            ("long", 1): np.mean(g - gs - sp - ss), ("short", 1): np.mean(-g + gs - sp - ss)}
                    z = np.array([rs[s] / (vol[s] * np.sqrt(L)) if vol.get(s) and vol[s] > 0 else 0 for s in syms])
                    for meth, lm, sm_ in (("dec", rv <= lo_c, rv >= hi_c), ("z2", z <= -2, z >= 2)):
                        for side, m in (("long", lm), ("short", sm_)):
                            for hed in (0, 1):
                                for k in np.where(m)[0]:
                                    gg = g[k] if side == "long" else -g[k]
                                    net = gg - sp[k] - (0 if not hed else (gs if side == "long" else -gs) + ss)
                                    if hed:
                                        gg = gg - (gs if side == "long" else -gs)
                                    nm = f"R {meth} L{L} H{H:3d} {'hedged' if hed else 'raw   '}"
                                    row = (di, P, net, gg, base[(side, hed)])
                                    cells[f"{nm} {side}"].append(row)
                                    cells[f"{nm} long/short"].append(row)
        # ── G Gao momentum on ETFs
        for s in ETFS:
            if s not in D or s not in prev:
                continue
            d, pc = D[s], prev[s].Cf[389]
            first = np.where(~np.isnan(d.O[:3]))[0]
            if not len(first) or d.age[29] > 3 or d.age[359] > 3 or d.age[329] > 3:
                continue
            op = d.O[first[0]]
            r1, r1o, r12 = d.Cf[29] / pc - 1, d.Cf[29] / op - 1, d.Cf[359] / d.Cf[329] - 1
            c30 = d.Cf[:30]
            rv30 = float(np.nanstd(np.diff(c30) / c30[:-1]))
            vo30 = float(np.nansum(d.V[:30]))
            hv = len(hist[(s, "vol")]) >= 20 and rv30 > np.percentile(hist[(s, "vol")][-20:], 66.7)
            hvo = len(hist[(s, "volu")]) >= 20 and vo30 > np.percentile(hist[(s, "volu")][-20:], 66.7)
            hist[(s, "vol")].append(rv30); hist[(s, "volu")].append(vo30)
            sp = spr(s, 360, d.Cf[359])
            grp = ["ALL15"] + (["IDX4"] if s in ("SPY", "QQQ", "IWM", "DIA") else []) + ([s] if s in ("SPY", "QQQ") else [])
            for tgt, j in (("->16:00", 389), ("->15:50", 379)):
                gret = (d.Cf[j] / d.Cf[359] - 1) * 1e4
                for pn, sig in (("r1 ", np.sign(r1)), ("r1o", np.sign(r1o)),
                                ("r1&r12", np.sign(r1) if np.sign(r1) == np.sign(r12) else 0)):
                    if sig == 0:
                        continue
                    for cond, ok in (("all ", True), ("hiVol", hv), ("hiVolu", hvo)):
                        if not ok:
                            continue
                        for g_ in grp:
                            cells[f"G {g_:5} {pn:6} {cond:6} {tgt}"].append(
                                (di, P, sig * gret - sp, sig * gret, gret - sp))
                for g_ in grp:
                    cells[f"G {g_:5} benchmark long   {tgt}"].append((di, P, gret - sp, gret, gret - sp))
        # ── O overnight vs intraday
        for s in elig:
            d, pc = D[s], prev[s].Cf[389]
            first = np.where(~np.isnan(d.O[:3]))[0]
            if not len(first) or d.age[389] > 5:
                continue
            op = d.O[first[0]]
            s_open = spr(s, 1, op)
            s_close = sm.get(s, prevday, 380, pc)
            s_close_today = spr(s, 380, d.Cf[389])
            on = (op / pc - 1) * 1e4
            intr = (d.Cf[389] / op - 1) * 1e4
            grp = ["ETF15" if s in ETFS else "STOCKS"] + (["SPY"] if s == "SPY" else []) + (["QQQ"] if s == "QQQ" else [])
            for g_ in grp:
                cells[f"O {g_:6} overnight close->open"].append((di, P, on - (s_open + s_close) / 2, on, 0.0))
                cells[f"O {g_:6} intraday open->close"].append((di, P, intr - (s_open + s_close_today) / 2, intr, 0.0))
        prev = D
        prevday = day
    print(f"eligible names/day: median {np.median(elig_counts):.0f} (min {min(elig_counts)}, max {max(elig_counts)})")

    nd = {p: sum(1 for d in days if part[d] == p) for p in "TVH"}

    def st(rows, p):
        a = [r for r in rows if r[1] in p]
        if len(a) < 10:
            return None, None
        A = np.array([(r[0], r[2], r[3], r[4]) for r in a])
        s = ec.stats(A[:, 1], A[:, 0], gross=A[:, 2], n_days_total=sum(nd[x] for x in p))
        b = ec.stats(A[:, 1] - A[:, 3], A[:, 0])
        return s, b

    if mode == "holdout":
        prom = json.load(open(PROMOTED))
        print(f"\nHOLDOUT ({len(reg['holdout'])} days, {reg['holdout'][0]}..{reg['holdout'][-1]}) for promoted cells:")
        for nm in prom:
            s, b = st(cells[nm], "H")
            print(f"  {nm:44} {ec.fmt(s)} | vs base {b['net']:+6.1f} (t {b['t']:+4.1f})" if s else f"  {nm}: too few")
        return
    print("\nnet bp/trade  TUNE | VALIDATE   (t day-clustered)   vs base = minus random/benchmark on the same trades")
    summ = []
    for nm in sorted(cells):
        sT, bT = st(cells[nm], "T")
        sV, bV = st(cells[nm], "V")
        sP, _ = st(cells[nm], "TV")
        if not sT or not sV:
            continue
        promote = sT["net"] > 0 and sV["net"] > 0 and sP["t"] >= 2.0 and "benchmark" not in nm
        summ.append((nm, sP["t"], promote))
        print(f"  {nm:44} {sT['per_day']:6.1f}/d  gross {sT['gross']:+6.1f}|{sV['gross']:+6.1f}  "
              f"net {sT['net']:+6.1f} (t{sT['t']:+4.1f}) | {sV['net']:+6.1f} (t{sV['t']:+4.1f})  "
              f"CI_V [{sV['lo']:+6.1f},{sV['hi']:+6.1f}]  vs base {bT['net']:+5.1f}|{bV['net']:+5.1f}"
              f"{'   <<PROMOTE' if promote else ''}")
    prom = [nm for nm, t, p in sorted(summ, key=lambda x: -x[1]) if p][:3]
    print(f"\n{len(summ)} cells; promoted to holdout (net>0 tune & validate, pooled t>=2, top 3 by t): {prom}")
    if not os.environ.get("EDGE_SMOKE"):
        json.dump(prom, open(PROMOTED, "w"), indent=1)


if __name__ == "__main__":
    main()
