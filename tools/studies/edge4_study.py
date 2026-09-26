#!/usr/bin/env python3
"""edge4_study.py — Round-4 volume and spread signals (2026-09-26). Pre-registered in
docs/studies/edge4_prereg.json before any result was seen; data from edge4_fetch.py.

Signals V1 (volume pace), V2 (volume breakout 1m/5m), V3 (VSA effort/absorption/no-supply) are
detected on SIP bars and, separately, on IEX bars; S1 (spread narrowing, NBBO size imbalance) on
SIP quote snapshots. Outcomes are always SIP closes; cost = modeled full spread at decision time.
Baselines: random same name + clock hour on other days of the same split part (5 draws) and the
same day (look-ahead caveat). Holdout rows are printed only for pre-registered promotions.

USAGE  edge4_study.py L | C | S1        (writes ai_reports/edge4/cells_<U>.json)
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

E4 = os.path.join(ROOT, "ai_reports", "edge4")
IEXD = os.path.join(ROOT, "ai_reports", "edge_iex_bars")
HOLDS = (15, 30, 60, 120, "eod")
GRID = list(range(14, 360, 15))       # V1 decisions: 09:45, 10:00, ..., 15:30 (close of bar k = 09:31+k)
REFR = 15
PRE = 30                               # previous-session bars prepended for trailing windows


def dense(B, day):
    t, o, h, l, c, v = B
    k = ((np.asarray(t) - ec.open_ts(day)) // 60).astype(np.int64)
    m = (k >= 0) & (k < 390)
    D = {x: np.full(390, np.nan) for x in "OHLC"}
    D["V"] = np.zeros(390)
    kk = k[m]
    for x, a in zip("OHLC", (o, h, l, c)):
        D[x][kk] = np.asarray(a)[m]
    D["V"][kk] = np.asarray(v)[m]
    idx = np.where(np.isfinite(D["C"]), np.arange(390), -1)
    idx = np.maximum.accumulate(idx)
    D["Cf"] = np.where(idx >= 0, D["C"][np.maximum(idx, 0)], np.nan)
    tp = np.where(D["V"] > 0, (D["H"] + D["L"] + D["C"]) / 3, 0.0)
    cv = np.cumsum(D["V"])
    with np.errstate(invalid="ignore", divide="ignore"):
        D["vwap"] = np.where(cv > 0, np.cumsum(np.nan_to_num(tp) * D["V"]) / np.where(cv > 0, cv, 1), np.nan)
    D["cumV"] = cv
    return D


def load(feed, day):
    if feed == "sip":
        p = os.path.join(E4, "sip", f"{day}.pkl")
        return pickle.load(open(p, "rb")) if os.path.exists(p) else {}
    out = {}
    for p in (os.path.join(E4, "iex", f"{day}.pkl"), os.path.join(IEXD, f"{day}.pkl"), os.path.join(IEXD, "liq", f"{day}.pkl")):
        if os.path.exists(p):
            out.update(pickle.load(open(p, "rb")))
    return out


def refract(ks):
    out, last = [], -10 ** 9
    for k in ks:
        if k - last >= REFR:
            out.append(int(k)); last = k
    return out


def ext(prev, cur, key, fill):
    a = prev[key][-PRE:] if prev is not None else np.full(PRE, fill)
    return np.concatenate([a, cur[key]])


def v_signals(D, P, hist):
    """D = today's dense (detection feed), P = previous session's dense (or None), hist = list of prior cumV.
    Returns {(sig, dir): [decision k]}."""
    out = {}
    C, O, H, L, V, Cf, vw = D["C"], D["O"], D["H"], D["L"], D["V"], D["Cf"], D["vwap"]
    pc = P["Cf"][-1] if P is not None else np.nan
    # V1 pace on the 15-min grid
    if len(hist) >= 15 and np.isfinite(pc):
        base = np.mean(hist[-20:], axis=0)
        g = np.array(GRID)
        with np.errstate(invalid="ignore", divide="ignore"):
            pace = D["cumV"][g] / base[g]
        up = (Cf[g] > vw[g]) & (Cf[g] > pc)
        dn = (Cf[g] < vw[g]) & (Cf[g] < pc)
        for X in (1.6, 2.0, 3.0):
            hit = np.isfinite(pace) & (pace >= X)
            out[(f"V1 pace>={X:g}", 1)] = list(g[hit & up])
            out[(f"V1 pace>={X:g}", -1)] = list(g[hit & dn])
    Ve, He, Le, Ce = ext(P, D, "V", 0.0), ext(P, D, "H", np.nan), ext(P, D, "L", np.nan), ext(P, D, "Cf", np.nan)
    cs = np.concatenate([[0.0], np.cumsum(Ve)])
    ks = np.arange(14, 390)
    ke = ks + PRE
    mean20 = (cs[ke] - cs[ke - 20]) / 20.0
    # V2 1m
    for N in (15, 30):
        with np.errstate(all="ignore"):
            hiN = np.array([np.nanmax(He[i - N:i]) if np.isfinite(He[i - N:i]).any() else np.nan for i in ke])
            loN = np.array([np.nanmin(Le[i - N:i]) if np.isfinite(Le[i - N:i]).any() else np.nan for i in ke])
        for m in (3, 5):
            vol = (mean20 > 0) & (V[ks] >= m * mean20) & np.isfinite(C[ks])
            out[(f"V2 1m v>={m}x hi{N}", 1)] = refract(ks[vol & (C[ks] > hiN)])
            out[(f"V2 1m v>={m}x hi{N}", -1)] = refract(ks[vol & (C[ks] < loN)])
    # V2 5m (aligned to 09:30); trailing 20 five-minute bars incl. previous session
    V5 = D["V"].reshape(78, 5).sum(1)
    V5p = P["V"].reshape(78, 5).sum(1)[-20:] if P is not None else np.zeros(20)
    V5e = np.concatenate([V5p, V5])
    for N in (15, 30):
        for m in (3, 5):
            for dirn in (1, -1):
                hits = []
                for j in range(2, 78):
                    k = 5 * j + 4
                    mu = V5e[j:j + 20].mean()
                    if mu <= 0 or V5[j] < m * mu or not np.isfinite(Cf[k]):
                        continue
                    s0 = 5 * j + PRE
                    seg = He[s0 - N:s0] if dirn == 1 else Le[s0 - N:s0]
                    if not np.isfinite(seg).any():
                        continue
                    if (dirn == 1 and Cf[k] > np.nanmax(seg)) or (dirn == -1 and Cf[k] < np.nanmin(seg)):
                        hits.append(k)
                out[(f"V2 5m v>={m}x hi{N}", dirn)] = refract(hits)
    # V3 VSA
    Re = np.where(np.isfinite(He) & np.isfinite(Le), He - Le, np.nan)
    with np.errstate(all="ignore"):
        avgR = np.array([np.nanmean(Re[i - 20:i]) if np.isfinite(Re[i - 20:i]).any() else np.nan for i in ke])
        R = H[ks] - L[ks]
        real = np.isfinite(C[ks]) & np.isfinite(O[ks]) & (V[ks] > 0)
        pos = np.where(R > 0, (C[ks] - L[ks]) / np.where(R > 0, R, 1), np.nan)
        hv = real & (mean20 > 0) & (V[ks] >= 2 * mean20)
        efr_u = hv & (C[ks] > O[ks]) & (R >= 1.5 * avgR) & (pos >= 0.75)
        efr_d = hv & (C[ks] < O[ks]) & (R >= 1.5 * avgR) & (pos <= 0.25)
        narrow = hv & (R <= 0.75 * avgR)
        r15 = Ce[ke - 1] - Ce[ke - 16]
        r30 = Ce[ke - 1] - Ce[ke - 31]
        lv = real & (mean20 > 0) & (V[ks] <= 0.5 * mean20) & (R <= avgR)
        ns_u = lv & (r30 > 0) & (C[ks] > vw[ks]) & (C[ks] < O[ks])
        ns_d = lv & (r30 < 0) & (C[ks] < vw[ks]) & (C[ks] > O[ks])
    out[("V3 EFR", 1)] = refract(ks[efr_u]); out[("V3 EFR", -1)] = refract(ks[efr_d])
    out[("V3 ABS", 1)] = refract(ks[narrow & (r15 < 0)]); out[("V3 ABS", -1)] = refract(ks[narrow & (r15 > 0)])
    out[("V3 NS", 1)] = refract(ks[ns_u]); out[("V3 NS", -1)] = refract(ks[ns_d])
    return out


def s1_signals(Q):
    out = {}
    sp, b, a = Q[:, 0], Q[:, 1], Q[:, 2]
    with np.errstate(all="ignore"):
        imb = b / (b + a)
    nar = []
    for k in range(34, 380):
        w = sp[k - 30:k]
        if np.isfinite(sp[k]) and np.isfinite(w).sum() >= 20 and sp[k] <= 0.67 * np.nanmedian(w):
            nar.append(k)
    out[("S1a narrow", 1)] = refract(nar)
    out[("S1a narrow", -1)] = list(out[("S1a narrow", 1)])
    ks = np.arange(14, 379)
    for lab, th in (("S1b imb>=3x", 0.75), ("S1b imb>=5x", 5 / 6)):
        out[(lab, 1)] = refract(ks[np.isfinite(imb[ks]) & (imb[ks] >= th)])
        out[(lab, -1)] = refract(ks[np.isfinite(imb[ks]) & (imb[ks] <= 1 - th)])
    return out


def exit_k(ek, h):
    if h == "eod":
        return 379 if ek <= 364 else None
    x = ek + h
    return x if x <= 379 else None


def hour_of(ek):
    return (570 + ek + 1) // 60


def main():
    U = sys.argv[1]
    split = json.load(open(os.path.join(ROOT, "ai_reports", "edge2_split.json")))
    Dd = json.load(open(os.path.join(E4, "days.json")))
    if U in ("L", "S1"):
        import edge2_fetch as e2
        names_all = list(e2.U2)
        days_all = Dd["L_warm"] + [split["prior_day"]] + split["tune"] + split["validate"] + split["holdout"]
        part = {d: p for p in ("tune", "validate", "holdout") for d in split[p]}
        uni = {d: set(names_all) for d in part}
        sm = ec.SpreadModel(set(names_all))
    else:
        rows = pickle.load(open(os.path.join(ROOT, "ai_reports", "combined_stage_b_rows.pkl"), "rb"))
        names_all = Dd["C_names"]
        days_all = Dd["C_warm"] + [Dd["C_prior"]] + Dd["C_days"]
        part = {d: ("tune" if i < 40 else "holdout") for i, d in enumerate(Dd["C_days"])}
        uni = {d: {r["sym"] for r in rows[d]} for d in Dd["C_days"]}
        sm = ec.SpreadModel()
    TIGHT = bool(os.environ.get("E4_TIGHT"))   # post-hoc info cut: names whose model median spread <= 3 bp
    if TIGHT:
        uni = {d: {x for x in v if sm.sym.get(x, 99.0) <= 3.0} for d, v in uni.items()}
    feeds = ("sip",) if U == "S1" else ("sip", "iex")
    if U == "S1":
        import edge_fetch as ef
        qdays = {os.path.basename(p)[:-4] for p in os.listdir(os.path.join(E4, "quotes"))}
        part = {d: p for d, p in part.items() if d in qdays}
        names_all = ef.LIQUID[:30]
        uni = {d: set(names_all) for d in part}
    sipCf = {}                      # (sym, day) -> SIP Cf array (outcome prices)
    events = defaultdict(list)      # (feed, sig, dir) -> [(sym, day, dk, ek)]
    prevD = {f: {} for f in feeds}
    hist = {f: defaultdict(list) for f in feeds}
    for day in days_all:
        S = load("sip", day)
        Ds = {s: dense(B, day) for s, B in S.items() if s in names_all and len(B[0]) > 50}
        for s, D in Ds.items():
            if day in part:
                sipCf[(s, day)] = D["Cf"]
        for f in feeds:
            if U == "S1":
                if day in part:
                    Q = pickle.load(open(os.path.join(E4, "quotes", f"{day}.pkl"), "rb"))
                    for s in names_all:
                        if s in Q and s in Ds:
                            for (sig, dr), ks in s1_signals(Q[s]).items():
                                events[(f, sig, dr)] += [(s, day, k, k + 1) for k in ks]
                continue
            Df = Ds if f == "sip" else {s: dense(B, day) for s, B in load("iex", day).items()
                                        if s in names_all and len(B[0]) > 20}
            for s, D in Df.items():
                if day in part and s in uni[day] and s in Ds:
                    for (sig, dr), ks in v_signals(D, prevD[f].get(s), hist[f][s]).items():
                        events[(f, sig, dr)] += [(s, day, k, k) for k in ks]
                hist[f][s].append(D["cumV"]); hist[f][s] = hist[f][s][-20:]
            prevD[f] = Df
        print(f"  {day}: {sum(len(v) for v in events.values())} events", flush=True)
    # sanity: lookup tables for baselines
    days_by_part = defaultdict(list)
    for d, p in part.items():
        days_by_part[p].append(d)
    rng = random.Random(7)

    def net(s, d, ek, h, dr):
        Cf = sipCf.get((s, d))
        if Cf is None:
            return None
        x = exit_k(ek, h)
        if x is None or not (np.isfinite(Cf[ek]) and np.isfinite(Cf[x])):
            return None
        g = dr * (Cf[x] / Cf[ek] - 1) * 1e4
        return g, g - sm.get(s, d, ek + 1, Cf[ek])

    cc = {}

    def draws(s, d, ek, h, dr, same_day):
        hr = hour_of(ek)
        if (hr, h) not in cc:
            cc[(hr, h)] = [k for k in range(14, 380) if hour_of(k) == hr and exit_k(k, h) is not None]
        cand = cc[(hr, h)]
        vals = []
        for _ in range(5):
            if not cand:
                break
            dd = d if same_day else rng.choice(days_by_part[part[d]])
            if dd == d and not same_day:
                continue
            r = net(s, dd, rng.choice(cand), h, dr)
            if r is not None:
                vals.append(r[1])
        return float(np.mean(vals)) if vals else None

    di = {d: i for i, d in enumerate(sorted(part))}
    cells = []
    for (f, sig, dr), evs in sorted(events.items()):
        for h in HOLDS:
            per = defaultdict(list)
            for (s, d, dk, ek) in evs:
                r = net(s, d, ek, h, dr)
                if r is None:
                    continue
                br = draws(s, d, ek, h, dr, False)
                bs = draws(s, d, ek, h, dr, True)
                per[part[d]].append((di[d], r[0], r[1], br, bs, sm.get(s, d, ek + 1)))
            for p in list(per) + (["tune+validate"] if U != "C" else []):
                rr = per[p] if p in per else per.get("tune", []) + per.get("validate", [])
                if len(rr) < 5:
                    continue
                A = np.array([(x[0], x[1], x[2], x[5]) for x in rr])
                st = ec.stats(A[:, 2], A[:, 0], gross=A[:, 1], n_days_total=len(days_by_part.get(p, [])) or
                              len(days_by_part["tune"]) + len(days_by_part["validate"]), boot=500)
                ex = np.array([(x[0], x[2] - x[3]) for x in rr if x[3] is not None])
                sx = ec.stats(ex[:, 1], ex[:, 0], boot=200) if len(ex) >= 5 else None
                bsv = [x[4] for x in rr if x[4] is not None]
                cells.append({"U": U, "feed": f, "sig": sig, "dir": dr, "hold": str(h), "part": p, **st,
                              "spr": float(A[:, 3].mean()),
                              "rand": float(np.mean([x[3] for x in rr if x[3] is not None])) if len(ex) else None,
                              "excess": sx["net"] if sx else None, "t_excess": sx["t"] if sx else None,
                              "same_day": float(np.mean(bsv)) if bsv else None})
    json.dump(cells, open(os.path.join(E4, f"cells_{U}{'_tight' if TIGHT else ''}.json"), "w"))
    print(f"wrote {len(cells)} cells")


if __name__ == "__main__":
    main()
