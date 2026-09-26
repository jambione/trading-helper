#!/usr/bin/env python3
"""edge5_t4.py — Round-5 T4 "Mark / Get set / Go" (Jonathan), pre-registered in docs/studies/edge5b_prereg.json.

Mark (pre-open): top decile of U2 by 60-day return (tie-break 20-day), only if SPY closed above its 50-day SMA the
prior day (daily adjusted SIP closes, ai_reports/edge5/daily.pkl). Get set: first bar closing 10:00..11:00 with
close > VWAP and return since the 09:30 open > SPY's. Go: unsmoothed %R(14) crosses up through -50 by 11:30 (enter
next open) OR a 10 bp pullback limit fills within 10 bars. Stages x holds (30m, 60m, 15:50, 1d, 5d) vs 5 random
unmarked U2 names at the same minute; costs = modeled spread (+2 bp overnight). 1-min SIP bars from ai_reports/edge4/sip.

USAGE  edge5_t4.py [mark_file.json]   (optional alternative Mark: {day: [symbols]} -> variant 2)
"""
from __future__ import annotations

import json
import os
import pickle
import random
import sys
from collections import defaultdict

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import edge_common as ec  # noqa: E402
import edge2_fetch as e2  # noqa: E402
import edge4_study as S  # noqa: E402

HOLDS = ("30m", "60m", "15:50", "1d", "5d")
STAGES = ("Mark-only", "Mark+Set", "Mark+Set+Go(%R)", "Mark+Set+Go(pullback)")


def wr14(D):
    H, L, C = D["H"], D["L"], D["Cf"]
    out = np.full(390, np.nan)
    for i in range(13, 390):
        h = H[i - 13:i + 1]; l = L[i - 13:i + 1]
        if not np.isfinite(h).any() or not np.isfinite(C[i]):
            continue
        hh, ll = np.nanmax(h), np.nanmin(l)
        if hh > ll:
            out[i] = -100 * (hh - C[i]) / (hh - ll)
    return out


def main():
    U = sorted(e2.U2)
    split = json.load(open(os.path.join(ROOT, "ai_reports", "edge2_split.json")))
    days = split["tune"] + split["validate"] + split["holdout"]
    part = {d: p for p in ("tune", "validate", "holdout") for d in split[p]}
    sm = ec.SpreadModel(set(U))
    daily = pickle.load(open(os.path.join(ROOT, "ai_reports", "edge5", "daily.pkl"), "rb"))
    dcal = [r[0] for r in daily["SPY"]]
    dclose = {s: {r[0]: r[2] for r in rows} for s, rows in daily.items() if s in U}
    di = {d: i for i, d in enumerate(dcal)}
    # ---- Mark
    marks = {}
    alt = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else None
    for d in days:
        if alt is not None:
            marks[d] = alt.get(d, []); continue
        i = di[d]
        prev = dcal[i - 1]
        spy = [dclose["SPY"][dcal[k]] for k in range(i - 50, i)]
        if dclose["SPY"][prev] <= np.mean(spy):
            marks[d] = []; continue
        sc = []
        for s in U:
            c = dclose[s]
            if prev in c and dcal[i - 61] in c and dcal[i - 21] in c:
                sc.append((c[prev] / c[dcal[i - 61]] - 1, c[prev] / c[dcal[i - 21]] - 1, s))
        sc.sort(reverse=True)
        marks[d] = [x[2] for x in sc[:max(1, round(len(sc) / 10))]]
    # ---- day closes (raw 1-min, last RTH close) for overnight holds
    close = {}
    for d in days:
        for s, B in S.load("sip", d).items():
            if s in dclose or s == "SPY":
                close[(s, d)] = float(B[4][-1])
    rng = random.Random(5)
    rows = defaultdict(list)   # (stage, hold, part) -> [(dayidx, net, rand_net, minus_spy)]
    dix = {d: k for k, d in enumerate(days)}
    for n, d in enumerate(days):
        ms = marks[d]
        if not ms:
            continue
        Sraw = S.load("sip", d)
        Dd = {s: S.dense(B, d) for s, B in Sraw.items() if s in dclose and len(B[0]) > 100}
        if "SPY" not in Dd:
            continue
        spy = Dd["SPY"]
        spy_o = spy["O"][0] if np.isfinite(spy["O"][0]) else spy["Cf"][0]
        nxt1 = days[n + 1] if n + 1 < len(days) else None
        nxt5 = days[n + 5] if n + 5 < len(days) else None
        others = [s for s in Dd if s not in ms and s != "SPY"]

        def exit_px(s, D, e, hold):
            if hold == "30m":
                return D["Cf"][e + 29] if e + 29 <= 389 else np.nan
            if hold == "60m":
                return D["Cf"][e + 59] if e + 59 <= 389 else np.nan
            if hold == "15:50":
                return D["Cf"][379]
            dd = nxt1 if hold == "1d" else nxt5
            return close.get((s, dd), np.nan) if dd else np.nan

        def trade(s, D, e, px, hold, limit):
            x = exit_px(s, D, e, hold)
            if not (np.isfinite(x) and np.isfinite(px) and px > 0):
                return None
            g = (x / px - 1) * 1e4
            sp = sm.get(s, d, e)
            cost = (sp / 2 if limit else sp) + (2.0 if hold in ("1d", "5d") else 0.0)
            return g - cost

        def mkt_entry(D, e):
            p = D["O"][e]
            return p if np.isfinite(p) else D["Cf"][e - 1]

        def spy_ret(e, hold):
            x = exit_px("SPY", spy, e, hold)
            p = mkt_entry(spy, e)
            return (x / p - 1) * 1e4 if np.isfinite(x) and np.isfinite(p) else np.nan

        def setup(D):
            o = D["O"][0] if np.isfinite(D["O"][0]) else D["Cf"][0]
            for k in range(29, 90):
                if np.isfinite(D["Cf"][k]) and D["Cf"][k] > D["vwap"][k] and \
                        D["Cf"][k] / o - 1 > spy["Cf"][k] / spy_o - 1:
                    return k
            return None

        def go_wr(D, ks):
            W = wr14(D)
            for j in range(ks + 1, 120):
                if np.isfinite(W[j - 1]) and np.isfinite(W[j]) and W[j - 1] <= -50 < W[j]:
                    return j + 1
            return None

        def go_pb(D, ks):
            lim = D["Cf"][ks] * (1 - 0.001)
            for j in range(ks + 1, ks + 11):
                if np.isfinite(D["L"][j]) and D["L"][j] < lim:
                    return j, lim
            return None

        def rand_net(e, hold, limit_from=None):
            vals = []
            for s in rng.sample(others, min(5, len(others))):
                D = Dd[s]
                if limit_from is not None:
                    r = go_pb(D, limit_from)
                    if r is None:
                        vals.append(0.0)   # unfilled random limit: no trade (0), mirrors per-attempt accounting
                        continue
                    t = trade(s, D, r[0], r[1], hold, True)
                else:
                    t = trade(s, D, e, mkt_entry(D, e), hold, False)
                if t is not None:
                    vals.append(t)
            return float(np.mean(vals)) if vals else np.nan

        for s in ms:
            if s not in Dd:
                continue
            D = Dd[s]
            cand = {"Mark-only": (15, mkt_entry(D, 15), False, None)}
            ks = setup(D)
            if ks is not None:
                cand["Mark+Set"] = (ks + 1, mkt_entry(D, ks + 1), False, None)
                e = go_wr(D, ks)
                if e is not None and e <= 389:
                    cand["Mark+Set+Go(%R)"] = (e, mkt_entry(D, e), False, None)
                r = go_pb(D, ks)
                if r is not None:
                    cand["Mark+Set+Go(pullback)"] = (r[0], r[1], True, ks)
            for st, (e, px, lim, kfrom) in cand.items():
                for h in HOLDS:
                    t = trade(s, D, e, px, h, lim)
                    if t is None:
                        continue
                    rr = rand_net(e, h, kfrom)
                    sr = spy_ret(e, h)
                    g = t + (sm.get(s, d, e) / 2 if lim else sm.get(s, d, e)) + (2.0 if h in ("1d", "5d") else 0.0)
                    rows[(st, h, part[d])].append((dix[d], t, rr, g - sr if np.isfinite(sr) else np.nan))
        if n % 25 == 0:
            print(f"  {d}: marks {len(ms)}", flush=True)
    ndays = {p: sum(1 for d in days if part[d] == p) for p in ("tune", "validate", "holdout")}
    mdays = {p: sum(1 for d in days if part[d] == p and marks[d]) for p in ndays}
    print(f"\nT4 {'variant 2 (alt Mark)' if alt else 'Mark = top-decile 60d return, SPY > SMA50'}; mark days tune {mdays['tune']}/{ndays['tune']}, "
          f"validate {mdays['validate']}/{ndays['validate']}, holdout {mdays['holdout']}/{ndays['holdout']}")
    print(f"{'stage':22} {'hold':>5} | {'part':8} {'n':>5} {'/day':>5} {'net':>6} {'t':>5} {'CI':>15} | {'rand':>6} {'excess':>6} {'t_ex':>5} | {'gross-SPY':>9} {'t':>5}")
    out = []
    for st in STAGES:
        for h in HOLDS:
            for p in ("tune", "validate", "holdout"):
                v = rows.get((st, h, p), [])
                if len(v) < 5:
                    print(f"{st:22} {h:>5} | {p:8} {len(v):5d}"); continue
                A = np.array(v, float)
                s1 = ec.stats(A[:, 1], A[:, 0], boot=400)
                ok = np.isfinite(A[:, 2])
                sx = ec.stats(A[ok, 1] - A[ok, 2], A[ok, 0], boot=200)
                ok2 = np.isfinite(A[:, 3])
                ss = ec.stats(A[ok2, 3], A[ok2, 0], boot=100)
                print(f"{st:22} {h:>5} | {p:8} {len(v):5d} {len(v)/ndays[p]:5.2f} {s1['net']:+6.1f} {s1['t']:+5.2f} [{s1['lo']:+6.1f},{s1['hi']:+6.1f}] | "
                      f"{np.nanmean(A[:, 2]):+6.1f} {sx['net']:+6.1f} {sx['t']:+5.2f} | {ss['net']:+9.1f} {ss['t']:+5.2f}")
                out.append({"stage": st, "hold": h, "part": p, "n": len(v), "per_day": len(v) / ndays[p], **{k: s1[k] for k in ("net", "t", "lo", "hi", "median", "win")},
                            "rand": float(np.nanmean(A[:, 2])), "excess": sx["net"], "t_excess": sx["t"], "minus_spy_gross": ss["net"], "t_mspy": ss["t"]})
    json.dump(out, open(os.path.join(ROOT, "ai_reports", "edge5", f"t4{'_v2' if alt else ''}.json"), "w"))


if __name__ == "__main__":
    main()
