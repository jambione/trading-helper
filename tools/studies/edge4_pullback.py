#!/usr/bin/env python3
"""edge4_pullback.py — Round-4 test P: pullback limit entry after the arm (2026-09-26).
Pre-registered in docs/studies/edge4_prereg_P.json before results.

Arm = the live trigger (round 1 edge_c_grid.wr_signals) on IEX 1m bars with the previous session
prepended. After the arm, a buy limit at arm price (SIP close) x (1 - d) waits W minutes; filled when
a SIP 1m low trades strictly through it; fill = limit, no entry spread, half the modeled spread at
exit. Compared with market entry on the same arms and with a random-time limit (same name, same clock
hour, another day of the same part). Holdout printed only for promoted cells.

USAGE  edge4_pullback.py L | C     (writes ai_reports/edge4/cells_P_<U>.json and prints tables)
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
import edge4_study as S  # noqa: E402

DS = (("5bp", 5.0, 0), ("10bp", 10.0, 0), ("20bp", 20.0, 0), ("40bp", 40.0, 0), ("0.25ATR", 0.25, 1), ("0.5ATR", 0.5, 1))
WS = (2, 5, 10)
HS = (15, 30, 60, "eod")


def wr(h, l, c, length, span):
    """Vectorised twin of mid_rise_runway_study.percent_r_series (None -> NaN)."""
    n = len(c)
    out = np.full(n, np.nan)
    if n < length:
        return out
    from numpy.lib.stride_tricks import sliding_window_view as sw
    hh = np.full(n, np.nan); ll = np.full(n, np.nan)
    hh[length - 1:] = sw(h, length).max(1); ll[length - 1:] = sw(l, length).min(1)
    sp = hh - ll
    ok = np.isfinite(sp) & (sp > 0)
    raw = np.where(ok, -100.0 * (hh - c) / np.where(ok, sp, 1), np.nan)
    a = 2.0 / (span + 1.0)
    prev = None
    for i in np.where(ok)[0]:
        prev = raw[i] if prev is None else a * raw[i] + (1 - a) * prev
        out[i] = prev
    return out


def arms(B, PB, day, lo, hi):
    if PB is not None and len(PB[0]):
        B = tuple(np.concatenate([np.asarray(PB[q]), np.asarray(B[q])]) for q in range(6))
    t, o, h, l, c, v = (np.asarray(x, float) for x in B)
    if len(c) < 60:
        return []
    k = ((t - ec.open_ts(day)) // 60).astype(int)
    fast, slow = wr(h, l, c, 21, 7.0), wr(h, l, c, 112, 3.0)
    out, last = [], -10 ** 9
    for i in range(2, len(c)):
        e = k[i]
        if not (14 <= e <= 359) or not (lo <= c[i] <= hi):
            continue
        f0, f1 = fast[i - 1], fast[i]
        if np.isnan(f0) or np.isnan(f1) or np.isnan(slow[i]) or np.isnan(slow[i - 2]):
            continue
        if f0 <= -50 < f1 and slow[i] > slow[i - 2] and e - last >= 15:
            out.append(int(e)); last = e
    return out


def atr5(D, P):
    """ATR(14) of 5-min SIP bars aligned to 09:30, as bp of price, valid for a decision at the close of bar k."""
    def five(X):
        H = np.array([np.nanmax(X["H"][5 * j:5 * j + 5]) if np.isfinite(X["H"][5 * j:5 * j + 5]).any() else np.nan for j in range(78)])
        L = np.array([np.nanmin(X["L"][5 * j:5 * j + 5]) if np.isfinite(X["L"][5 * j:5 * j + 5]).any() else np.nan for j in range(78)])
        C = X["Cf"][4::5]
        return H, L, C
    H, L, C = five(D)
    if P is not None:
        Hp, Lp, Cp = five(P)
        H, L, C = np.concatenate([Hp, H]), np.concatenate([Lp, L]), np.concatenate([Cp, C])
        off = 78
    else:
        off = 0
    Cprev = np.concatenate([[np.nan], C[:-1]])
    with np.errstate(all="ignore"):
        tr = np.nanmax(np.vstack([H - L, np.abs(H - Cprev), np.abs(L - Cprev)]), axis=0)
    out = np.full(390, np.nan)
    for k in range(390):
        jc = (k - 4) // 5 if k >= 4 else -1         # last completed 5-min bar index today
        end = off + jc + 1
        w = tr[max(0, end - 14):end]
        if np.isfinite(w).sum() >= 10 and np.isfinite(D["Cf"][k]):
            out[k] = np.nanmean(w) / D["Cf"][k] * 1e4
    return out


def main():
    U = sys.argv[1]
    split = json.load(open(os.path.join(ROOT, "ai_reports", "edge2_split.json")))
    Dd = json.load(open(os.path.join(S.E4, "days.json")))
    if U == "L":
        import edge2_fetch as e2
        names = set(e2.U2)
        days_all = Dd["L_warm"][-1:] + [split["prior_day"]] + split["tune"] + split["validate"] + split["holdout"]
        part = {d: p for p in ("tune", "validate", "holdout") for d in split[p]}
        uni = {d: names for d in part}
        sm = ec.SpreadModel(names); lo, hi = 0, 1e9
    else:
        rows = pickle.load(open(os.path.join(ROOT, "ai_reports", "combined_stage_b_rows.pkl"), "rb"))
        names = set(Dd["C_names"])
        days_all = Dd["C_warm"][-1:] + [Dd["C_prior"]] + Dd["C_days"]
        part = {d: ("tune" if i < 40 else "holdout") for i, d in enumerate(Dd["C_days"])}
        uni = {d: {r["sym"] for r in rows[d]} for d in Dd["C_days"]}
        sm = ec.SpreadModel(); lo, hi = 20, 100
    TIGHT = bool(os.environ.get("E4_TIGHT"))   # post-hoc info cut: names whose model median spread <= 3 bp
    if TIGHT:
        uni = {d: {x for x in v if sm.sym.get(x, 99.0) <= 3.0} for d, v in uni.items()}
    SD = {}          # (sym, day) -> (Cf, L, atr) float arrays
    ARMS = []
    prevI, prevS = {}, {}
    for day in days_all:
        Sraw = S.load("sip", day)
        Ds = {s: S.dense(B, day) for s, B in Sraw.items() if s in names and len(B[0]) > 50}
        I = {s: B for s, B in S.load("iex", day).items() if s in names}
        if day in part:
            for s, D in Ds.items():
                if s not in uni[day]:
                    continue
                SD[(s, day)] = (D["Cf"], D["L"], atr5(D, prevS.get(s)))
                if s in I:
                    ARMS += [(s, day, k) for k in arms(I[s], prevI.get(s), day, lo, hi) if np.isfinite(D["Cf"][k])]
        prevI, prevS = I, Ds
        print(f"  {day}: arms {len(ARMS)}", flush=True)
    days_by_part = defaultdict(list)
    for d, p in part.items():
        days_by_part[p].append(d)
    di = {d: i for i, d in enumerate(sorted(part))}
    rng = random.Random(11)
    if len(sys.argv) > 2 and sys.argv[2] == "book":
        return book(sys.argv[3:], SD, ARMS, part, days_by_part, sm)

    def last_ok(W, h):
        return 364 - W if h == "eod" else 379 - W - h

    def sim(s, d, k, dd, W, h):
        """-> (filled, net_fill, gross_fill, mkt_net, mkt_gross) or None"""
        Cf, L, A = SD[(s, d)]
        p0 = Cf[k]
        dbp = dd[1] if dd[2] == 0 else dd[1] * A[k]
        if not np.isfinite(dbp) or not np.isfinite(p0):
            return None
        xm = 379 if h == "eod" else k + h
        if not np.isfinite(Cf[xm]):
            return None
        mg = (Cf[xm] / p0 - 1) * 1e4
        mn = mg - sm.get(s, d, k + 1, p0)
        lim = p0 * (1 - dbp / 1e4)
        for j in range(k + 1, k + W + 1):
            if np.isfinite(L[j]) and L[j] < lim:
                x = 379 if h == "eod" else j + h
                if not np.isfinite(Cf[x]):
                    return None
                g = (Cf[x] / lim - 1) * 1e4
                return True, g - sm.get(s, d, x + 1, p0) / 2, g, mn, mg
        return False, 0.0, None, mn, mg

    cells = []
    for dd in DS:
        for W in WS:
            for h in HS:
                per = defaultdict(list)
                cands = defaultdict(list)
                for kk in range(14, last_ok(W, h) + 1):
                    cands[S.hour_of(kk)].append(kk)
                for (s, d, k) in ARMS:
                    if k > last_ok(W, h):
                        continue
                    r = sim(s, d, k, dd, W, h)
                    if r is None:
                        continue
                    cand = cands[S.hour_of(k)]
                    rv = []
                    for _ in range(3):
                        d2 = rng.choice(days_by_part[part[d]])
                        if d2 == d or (s, d2) not in SD or not cand:
                            continue
                        q = sim(s, d2, rng.choice(cand), dd, W, h)
                        if q is not None:
                            rv.append(q)
                    per[part[d]].append((di[d], r, rv))
                keys = list(per) + (["tune+validate"] if U == "L" else [])
                for p in keys:
                    rr = per[p] if p in per else per.get("tune", []) + per.get("validate", [])
                    if len(rr) < 10:
                        continue
                    day_ = np.array([x[0] for x in rr])
                    filled = np.array([x[1][0] for x in rr])
                    perarm = np.array([x[1][1] for x in rr])
                    mkt = np.array([x[1][3] for x in rr]); mktg = np.array([x[1][4] for x in rr])
                    sa = ec.stats(perarm, day_, boot=300)
                    sm_ = ec.stats(mkt, day_, gross=mktg, boot=300)
                    sf = ec.stats(perarm[filled], day_[filled], gross=np.array([x[1][2] for x in rr if x[1][0]]), boot=300) if filled.sum() >= 5 else None
                    rvs = [q for x in rr for q in x[2]]
                    rf = [q for q in rvs if q[0]]
                    ex = np.array([(x[0], x[1][1] - np.mean([q[1] for q in x[2]])) for x in rr if x[2]])
                    sx = ec.stats(ex[:, 1], ex[:, 0], boot=200) if len(ex) >= 10 else None
                    cells.append({"U": U, "d": dd[0], "W": W, "hold": str(h), "part": p, "arms": int(len(rr)),
                                  "fill": float(filled.mean()), "per_arm": sa["net"], "t_arm": sa["t"],
                                  "lo": sa["lo"], "hi": sa["hi"],
                                  "per_fill": sf["net"] if sf else None, "gross_fill": sf["gross"] if sf else None,
                                  "t_fill": sf["t"] if sf else None,
                                  "mkt": sm_["net"], "mkt_gross": sm_["gross"], "t_mkt": sm_["t"],
                                  "mkt_gross_filled": float(mktg[filled].mean()) if filled.any() else None,
                                  "mkt_gross_unfilled": float(mktg[~filled].mean()) if (~filled).any() else None,
                                  "rand_fill": float(np.mean([q[0] for q in rvs])) if rvs else None,
                                  "rand_per_fill": float(np.mean([q[1] for q in rf])) if rf else None,
                                  "rand_gross_fill": float(np.mean([q[2] for q in rf])) if rf else None,
                                  "rand_per_attempt": float(np.mean([q[1] for q in rvs])) if rvs else None,
                                  "excess_vs_rand": sx["net"] if sx else None, "t_excess": sx["t"] if sx else None})
                print(f"  cell {dd[0]} W{W} {h} done", flush=True)
    json.dump(cells, open(os.path.join(S.E4, f"cells_P_{U}{'_tight' if TIGHT else ''}.json"), "w"))
    print(f"wrote {len(cells)} cells; arms {len(ARMS)}")


def book(specs, SD, ARMS, part, days_by_part, sm, cap=5, dollars=1000.0):
    """Book simulation: max `cap` concurrent positions, one per name, arms 09:45-15:30, exits by 15:50.
    spec 'mkt:H' (market at the arm bar close, full spread) or 'D:W:H' (limit, e.g. 10bp:5:30).
    A limit order takes a slot only when it fills; a fill that finds the book full or the name open is dropped."""
    dmap = {x[0]: x for x in DS}
    byday = defaultdict(list)
    for a in ARMS:
        byday[a[1]].append(a)
    print(f"\n== Book: max {cap} concurrent, 1 per name, ${dollars:.0f}/position, 09:45-15:50 ==")
    print(f"{'spec':16} {'part':9} {'days':>4} {'opens':>6} {'/10min':>6} {'>=1 open':>8} {'>=2 open':>8} "
          f"{'net bp/tr':>9} {'t':>5} {'gross':>6} {'$/day':>7} {'days+':>6}")
    res = []
    trade_dump = {}
    for spec in specs:
        f = spec.split(":")
        mkt = f[0] == "mkt"
        h = f[-1]; h = "eod" if h == "eod" else int(h)
        W = None if mkt else int(f[1]); dd = None if mkt else dmap[f[0]]
        for p in ("tune", "validate", "holdout"):
            trades, occ1, occ2, nmin, dpnl = [], 0, 0, 0, []
            for d in days_by_part[p]:
                cand = []
                for (s, _d, k) in byday.get(d, []):
                    Cf, L, A = SD[(s, d)]
                    p0 = Cf[k]
                    if mkt:
                        x = 379 if h == "eod" else min(379, k + h)
                        if k >= 379 or not np.isfinite(Cf[x]):
                            continue
                        g = (Cf[x] / p0 - 1) * 1e4
                        cand.append((k, x, s, g, g - sm.get(s, d, k + 1, p0)))
                    else:
                        dbp = dd[1] if dd[2] == 0 else dd[1] * A[k]
                        if not np.isfinite(dbp):
                            continue
                        lim = p0 * (1 - dbp / 1e4)
                        for j in range(k + 1, min(k + W, 378) + 1):
                            if np.isfinite(L[j]) and L[j] < lim:
                                x = 379 if h == "eod" else min(379, j + h)
                                if np.isfinite(Cf[x]):
                                    g = (Cf[x] / lim - 1) * 1e4
                                    cand.append((j, x, s, g, g - sm.get(s, d, x + 1, p0) / 2))
                                break
                rnd = random.Random(hash(d) & 0xffff)
                rnd.shuffle(cand)
                cand.sort(key=lambda z: z[0])
                openp, day_tr = [], []
                for (e, x, s, g, n) in cand:
                    openp = [q for q in openp if q[1] > e]
                    if len(openp) >= cap or any(q[2] == s for q in openp):
                        continue
                    openp.append((e, x, s)); day_tr.append((e, x, g, n))
                cnt = np.zeros(390)
                for (e, x, g, n) in day_tr:
                    cnt[e:x] += 1
                occ1 += (cnt[14:379] >= 1).sum(); occ2 += (cnt[14:379] >= 2).sum(); nmin += 365
                trades += [(d, g, n) for (e, x, g, n) in day_tr]
                trade_dump.setdefault(spec, []).extend((d, float(g), float(n)) for (e, x, g, n) in day_tr)
                dpnl.append(sum(n for (e, x, g, n) in day_tr) * dollars / 1e4)
            nd = len(days_by_part[p])
            if not trades:
                print(f"{spec:16} {p:9} {nd:4d} no trades"); continue
            di_ = {d: i for i, d in enumerate(days_by_part[p])}
            st = ec.stats(np.array([t[2] for t in trades]), np.array([di_[t[0]] for t in trades]),
                          gross=np.array([t[1] for t in trades]), boot=300)
            row = {"spec": spec, "part": p, "days": nd, "opens": len(trades), "per10": len(trades) / (nd * 36.5),
                   "occ1": occ1 / nmin, "occ2": occ2 / nmin, "net": st["net"], "t": st["t"], "gross": st["gross"],
                   "usd_day": float(np.mean(dpnl)), "days_pos": int(sum(x > 0 for x in dpnl))}
            res.append(row)
            print(f"{spec:16} {p:9} {nd:4d} {row['opens']:6d} {row['per10']:6.2f} {row['occ1']:8.0%} {row['occ2']:8.0%} "
                  f"{row['net']:+9.1f} {row['t']:+5.2f} {row['gross']:+6.1f} {row['usd_day']:+7.2f} {row['days_pos']:3d}/{nd}")
    json.dump(res, open(os.path.join(S.E4, "book_P_L.json"), "w"), indent=1)
    json.dump(trade_dump, open(os.path.join(S.E4, "book_P_L_trades.json"), "w"))


if __name__ == "__main__":
    main()
