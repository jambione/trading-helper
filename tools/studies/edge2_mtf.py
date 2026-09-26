#!/usr/bin/env python3
"""edge2_mtf.py — Round-2 test 6: multi-timeframe trend alignment (2026-09-26).

Spec pre-registered in ai_reports/edge2_mtf_prereg.json (copy in docs/studies/)
before any computation. Same universe, split and spread model as edge2_study.py.

  trend a  close > EMA20 and EMA20 rising over 3 bars;  trend b  3 rising highs and lows
  S1  first minute 1m, 5m and 10m are all up
  S2  5m and 10m up, 1m close crosses back above its EMA20 (buy the dip in the trend)
  S3  control: 10m trend turns up (lower timeframes ignored)
  entry next 1m open; exits 15/30/60/120 min or "5m trend no longer up"; capped 15:50
  baselines: random every-5-min entries, same name/day/clock hour/exit; and S3
  short mirror (down trends) reported as information

USAGE  edge2_mtf.py [holdout]
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
from edge2_fetch import ETFS, SPLIT, U2  # noqa: E402
from edge2_study import load  # noqa: E402

PROMOTED = os.path.join(ROOT, "ai_reports", "edge2_mtf_promoted.json")
EXITS = (15, 30, 60, 120, "t5")


def ema(x, n=20):
    a = 2.0 / (n + 1)
    out = np.empty_like(x)
    m = x[0]
    for i, v in enumerate(x):
        m = a * v + (1 - a) * m
        out[i] = m
    return out


def series(prevD, D):
    C = np.concatenate([prevD.Cf, D.Cf])
    fill = lambda A: np.where(np.isnan(A), C, A)  # noqa: E731
    H = fill(np.concatenate([prevD.H, D.H]))
    L = fill(np.concatenate([prevD.L, D.L]))
    O = np.concatenate([prevD.O, D.O])
    return O, H, L, C


def tf(H, L, C, k):
    n = len(C) // k
    return (H[:n * k].reshape(n, k).max(1), L[:n * k].reshape(n, k).min(1), C[k - 1::k][:n])


def trends(H, L, C):
    E = ema(C)
    n = len(C)
    ua = np.zeros(n, bool); da = np.zeros(n, bool); ub = np.zeros(n, bool); db = np.zeros(n, bool)
    ua[3:] = (C[3:] > E[3:]) & (E[3:] > E[:-3])
    da[3:] = (C[3:] < E[3:]) & (E[3:] < E[:-3])
    ub[2:] = (H[2:] > H[1:-1]) & (H[1:-1] > H[:-2]) & (L[2:] > L[1:-1]) & (L[1:-1] > L[:-2])
    db[2:] = (H[2:] < H[1:-1]) & (H[1:-1] < H[:-2]) & (L[2:] < L[1:-1]) & (L[1:-1] < L[:-2])
    return {"a": (ua, da), "b": (ub, db)}, E


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
    cells = defaultdict(list)
    # added after the first run (not pre-registered): the same-day same-hour random baseline
    # shares the move that created a trend signal (look-ahead against trend entries), so also
    # compare with the name's random mean for that clock hour over ALL days of the same split part
    rsum = defaultdict(float); rcnt = defaultdict(int)
    prev = {s: ec.Dense(B, reg["prior_day"]) for s, B in load(reg["prior_day"]).items()}
    prevday = reg["prior_day"]
    for di, day in enumerate(days):
        P = part[day]
        if mode != "holdout" and P == "H":
            break
        D = {s: ec.Dense(B, day) for s, B in load(day).items() if len(B[0]) > 100}
        for s, d in D.items():
            if s not in prev or np.isnan(prev[s].Cf[389]):
                continue
            if not (s in ETFS or sm.sd.get((s, prevday), sm.sym.get(s, 99)) <= 3.0):
                continue
            O, H, L, C = series(prev[s], d)
            t1, E1 = trends(H, L, C)
            H5, L5, C5 = tf(H, L, C, 5)
            t5, _ = trends(H5, L5, C5)
            H10, L10, C10 = tf(H, L, C, 10)
            t10, _ = trends(H10, L10, C10)
            base_i = 390
            spr_cache = {}

            def entry(e):
                j = base_i + e + 1
                px = O[j] if not np.isnan(O[j]) else C[base_i + e]
                return px

            # next "5m trend no longer up/down" exit minute (today's index) after each minute
            nx = {}
            for var in ("a", "b"):
                for side in (1, -1):
                    keep = t5[var][0] if side > 0 else t5[var][1]
                    arr = np.full(390, 379, np.int64)
                    nxt = 379
                    for k in range(389, -1, -1):
                        arr[k] = nxt
                        i = base_i + k
                        if (i + 1) % 5 == 0 and k <= 379 and not keep[(i + 1) // 5 - 1]:
                            nxt = k
                    nx[(var, side)] = arr

            def exit_px(e, ex, var, side):
                if ex != "t5":
                    k = e + ex
                    return C[base_i + k] if k <= 379 else None
                return C[base_i + min(379, nx[(var, side)][e])]

            def spread_at(T, px):
                sp = spr_cache.get(T // 30)
                if sp is None:
                    sp = spr_cache[T // 30] = sm.get(s, day, T, px)
                return sp

            def net_of(e, ex, var, side):
                px = entry(e)
                x = exit_px(e, ex, var, side)
                if x is None or not px:
                    return None
                g = side * (x / px - 1) * 1e4
                return g - spread_at(e + 1, px), g

            # random baseline per clock hour (grid every 5 min), vectorized
            G = np.arange(14, 360, 5)
            gpx = np.where(np.isnan(O[base_i + G + 1]), C[base_i + G], O[base_i + G + 1])
            gsp = np.array([spread_at(e + 1, p) for e, p in zip(G, gpx)])
            ghr = (570 + G + 1) // 60
            rndm = {}
            for ex in EXITS:
                for var in (("a", "b") if ex == "t5" else ("-",)):
                    for side in (1, -1):
                        if ex == "t5":
                            k = np.minimum(379, nx[(var, side)][G]); ok = np.ones(len(G), bool)
                        else:
                            k = G + ex; ok = k <= 379; k = np.minimum(k, 379)
                        net = side * (C[base_i + k] / gpx - 1) * 1e4 - gsp
                        for hr in np.unique(ghr[ok]):
                            m = ok & (ghr == hr)
                            rndm[(int(hr), var, ex, side)] = float(net[m].mean())
                            kk = (s, int(hr), var, ex, side, P)
                            rsum[kk] += float(net[m].sum()); rcnt[kk] += int(m.sum())
            for var in ("a", "b"):
                u1, d1 = t1[var]; u5, d5 = t5[var]; u10, d10 = t10[var]
                last = defaultdict(lambda: -99)
                for e in range(14, 360):
                    i = base_i + e
                    j5, j10 = (i + 1) // 5 - 1, (i + 1) // 10 - 1
                    jp5, jp10 = i // 5 - 1, i // 10 - 1
                    sig = []
                    for side, U1, U5, U10 in ((1, u1, u5, u10), (-1, d1, d5, d10)):
                        allnow = U1[i] and U5[j5] and U10[j10]
                        allprev = U1[i - 1] and U5[jp5] and U10[jp10]
                        if allnow and not allprev:
                            sig.append(("S1", side))
                        cross = (C[i - 1] <= E1[i - 1] and C[i] > E1[i]) if side > 0 else (C[i - 1] >= E1[i - 1] and C[i] < E1[i])
                        if U5[j5] and U10[j10] and cross:
                            sig.append(("S2", side))
                        if U10[j10] and not U10[jp10]:
                            sig.append(("S3", side))
                    for S, side in sig:
                        if e - last[(S, side)] < 15:
                            continue
                        last[(S, side)] = e
                        hr = (570 + e + 1) // 60
                        for ex in EXITS:
                            r = net_of(e, ex, var, side)
                            if not r:
                                continue
                            b = rndm.get((int(hr), var if ex == "t5" else "-", ex, side), np.nan)
                            nm = f"M{var} {S} {'long ' if side > 0 else 'short'} {'exit 5m-turn' if ex == 't5' else f'hold {ex:3d}m'}"
                            cells[nm].append((di, P, r[0], r[1], b, (s, int(hr), var if ex == "t5" else "-", ex, side, P)))
        prev = D
        prevday = day
        if di % 25 == 0:
            print(f"  {day} done", flush=True)

    nd = {p: sum(1 for d in days if part[d] == p) for p in "TVH"}

    def st(rows, p):
        a = [r for r in rows if r[1] in p and not np.isnan(r[4])]
        if len(a) < 10:
            return None, None
        A = np.array([(r[0], r[2], r[3], r[4]) for r in a])
        return (ec.stats(A[:, 1], A[:, 0], gross=A[:, 2], n_days_total=sum(nd[x] for x in p)),
                ec.stats(A[:, 1] - A[:, 3], A[:, 0]))

    def vs_hour(rows, p):
        a = [(r[0], r[2] - rsum[r[5]] / rcnt[r[5]]) for r in rows if r[1] in p and rcnt.get(r[5])]
        if len(a) < 10:
            return None
        A = np.array(a)
        return ec.stats(A[:, 1], A[:, 0])

    def vs_s3(nm, p):
        other = nm.replace(" S1 ", " S3 ").replace(" S2 ", " S3 ")
        dm = lambda rows: {d: np.mean([r[2] for r in rows if r[0] == d]) for d in {r[0] for r in rows if r[1] in p}}  # noqa: E731
        a, b = dm(cells[nm]), dm(cells[other])
        ks = sorted(set(a) & set(b))
        if len(ks) < 5:
            return float("nan"), float("nan")
        x = np.array([a[k] - b[k] for k in ks])
        return float(x.mean()), float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))

    if mode == "holdout":
        prom = json.load(open(PROMOTED))
        print(f"\nMTF HOLDOUT ({reg['holdout'][0]}..{reg['holdout'][-1]}):")
        for nm in prom:
            s, b = st(cells[nm], "H")
            d3 = vs_s3(nm, "H")
            h = vs_hour(cells[nm], "H")
            print(f"  {nm:34} {ec.fmt(s)} | vs rnd same day {b['net']:+5.1f} (t {b['t']:+4.1f}) | vs rnd name-hour all days {h['net']:+5.1f} (t {h['t']:+4.1f}) | vs S3 {d3[0]:+5.1f} (t {d3[1]:+4.1f})")
        return
    print("\nMTF net bp/trade  TUNE | VALIDATE ; vs rnd = minus random same name/day/hour/exit ; vs S3 = daily-mean difference")
    summ = []
    for nm in sorted(cells):
        (sT, bT), (sV, bV), (sP, bP) = st(cells[nm], "T"), st(cells[nm], "V"), st(cells[nm], "TV")
        if not sT or not sV:
            continue
        dT, dV = vs_s3(nm, "T"), vs_s3(nm, "V")
        hT, hV = vs_hour(cells[nm], "T"), vs_hour(cells[nm], "V")
        promote = (sT["net"] > 0 and sV["net"] > 0 and sP["t"] >= 2.0 and bT["net"] > 0 and bV["net"] > 0)
        summ.append((nm, sP["t"], promote))
        print(f"  {nm:34} {sT['per_day']:6.1f}/d  gross {sT['gross']:+5.1f}|{sV['gross']:+5.1f}  "
              f"net {sT['net']:+5.1f} (t{sT['t']:+4.1f}) | {sV['net']:+5.1f} (t{sV['t']:+4.1f}) CI_V [{sV['lo']:+5.1f},{sV['hi']:+5.1f}]  "
              f"vs rnd(day) {bT['net']:+5.1f}(t{bT['t']:+4.1f})|{bV['net']:+5.1f}(t{bV['t']:+4.1f})  "
              f"vs rnd(name-hour) {hT['net']:+5.1f}(t{hT['t']:+4.1f})|{hV['net']:+5.1f}(t{hV['t']:+4.1f})  "
              f"vs S3 {dT[0]:+5.1f}(t{dT[1]:+4.1f})|{dV[0]:+5.1f}(t{dV[1]:+4.1f}){'  <<PROMOTE' if promote else ''}")
    prom = [nm for nm, t, p in sorted(summ, key=lambda x: -x[1]) if p and " long " in nm][:3]
    print(f"\n{len(summ)} cells; promoted (long side only): {prom}")
    if not os.environ.get("EDGE_SMOKE"):
        json.dump(prom, open(PROMOTED, "w"), indent=1)


if __name__ == "__main__":
    main()
