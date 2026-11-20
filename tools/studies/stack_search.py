#!/usr/bin/env python3
"""stack_search - 150 fixed MOVEMENT x DIRECTION x TIMING stacks on 1-minute SIP bars: is the best stack better than the
best of the same search on matched random entries, does it hold later, and does it pay the spread? Operator 11/20.
Prereg + amended_1: docs/studies/stack_search_prereg.json.

    .venv/bin/python tools/studies/stack_search.py build     # CPU, warehouse only: per-day events + pools (no API)
    .venv/bin/python tools/studies/stack_search.py search    # gross: stack table, choice, luck null, canaries
    .venv/bin/python tools/studies/stack_search.py run       # + quotes for the winner (after 16:05 ET), PROFITABLE, CONFIRM

Lift = beta-adjusted 30-minute gross of the event minus the mean beta-adjusted gross of up to 5 other names at bar t
that pass the name filter AND the stack's M condition (amended_1 #2).
"""
from __future__ import annotations

import hashlib
import json
import math
import pickle
import sys
from collections import defaultdict, deque
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

OUT = ROOT / "ai_reports" / "stack_search"
CACHE = OUT / "days"
SEARCH = ("2026-04-28", "2026-08-31")
SPLIT = "2026-07-15"
CONFIRM = ("2026-09-01", "2026-11-19")
T_LO, T_HI, HOLD, REFRACT = 15, 359, 30, 30          # 09:45..15:29; exit = close of t+30 <= bar 389
PX_LO, PX_HI = 20.0, 400.0
CAP, MDE_MAX, EDGE_T, N_NULL, POOL_PER_DAY = 1000, 10.0, 3.0, 200, 3000
BETA_DAYS, BETA_MIN_DAYS = 20, 10
MS = ("M0", "M1", "M2", "M3", "M4")
DS = ("D0", "D1", "D2", "D3", "D4", "D5")
TS = ("T0", "T1", "T2", "T3", "T4")
STACKS = [(m, d, t) for m in MS for d in DS for t in TS]
PLANT_STACK = None                                   # canary (c): set at search time from event COUNTS only


def sid(s):
    return "".join(s)


def hkey(*p):
    return int(hashlib.sha256("|".join(str(x) for x in p).encode()).hexdigest(), 16)


# ---------------------------------------------------------------- layer conditions (pure, one name-day, 390 bars)

def last_finite(a):
    v = a[np.isfinite(a)]
    return float(v[-1]) if len(v) else np.nan


def layers(o, h, l, c, v, pc_, ph, pl, pv, spy_c):
    """-> dict name -> bool[390] for every M, D, T value, using only bars <= t at bar t. pc_/ph/pl/pv: the prior
    session's RTH arrays. Pure."""
    from evening_family import minute_grid
    from bar_signal_family import wr_smoothed
    from rsi2_colour_timing import rsi_state
    n = len(c)
    out = {}
    pclose = last_finite(pc_)                         # prior close = last RTH close (15:59 bar)
    with np.errstate(invalid="ignore", divide="ignore"):
        chg = c / pclose - 1
        cv, pcv = np.nancumsum(np.nan_to_num(v)), np.nancumsum(np.nan_to_num(pv))
        out["M0"] = np.ones(n, bool)
        out["M1"] = (pcv > 0) & (cv / np.where(pcv > 0, pcv, 1) >= 2.0)
        o0 = o[np.isfinite(o)][0] if np.isfinite(o).any() else np.nan
        out["M2"] = np.full(n, bool(np.isfinite(o0) and abs(o0 / pclose - 1) >= 0.03))
        rng = np.full(n, np.nan)
        prng = []
        for t in range(29, n):
            w_h, w_l = h[t - 29:t + 1], l[t - 29:t + 1]
            if np.isfinite(w_h).any() and np.isfinite(c[t]):
                rng[t] = (np.nanmax(w_h) - np.nanmin(w_l)) / c[t]
            pw_h, pw_l = ph[t - 29:t + 1], pl[t - 29:t + 1]
            if np.isfinite(pw_h).any() and np.isfinite(pc_[t]):
                prng.append((np.nanmax(pw_h) - np.nanmin(pw_l)) / pc_[t])
        pmed = float(np.median(prng)) if len(prng) >= 100 else np.nan
        out["M3"] = np.isfinite(rng) & np.isfinite(pmed) & (rng >= 2 * pmed)
        out["M4"] = np.abs(chg) >= 0.03
        out["D0"] = np.ones(n, bool)
        out["D1"] = chg > 0
        tp = np.nan_to_num(c) * np.nan_to_num(v)
        vw = np.nancumsum(tp) / np.where(cv > 0, cv, np.nan)
        out["D2"] = c > vw
        c15 = np.concatenate([np.full(15, np.nan), c[:-15]])
        out["D3"] = c / c15 - 1 > 0
        cc, hh, ll, off = minute_grid(pc_, ph, pl, c, h, l)
        fast = wr_smoothed(hh, ll, cc, n=21, span=7)[off:]
        slow = wr_smoothed(hh, ll, cc, n=112, span=3)[off:]
        s5 = np.concatenate([np.full(5, np.nan), slow[:-5]])
        out["D4"] = slow > s5
        s15 = np.concatenate([np.full(15, np.nan), spy_c[:-15]])
        out["D5"] = spy_c / s15 - 1 > 0
        out["T0"] = np.ones(n, bool)
        c1 = np.concatenate([[np.nan], c[:-1]])
        l1 = np.concatenate([[np.nan], l[:-1]])
        out["T1"] = (c > c1) & (l > l1)
        hmax5 = np.full(n, np.nan)
        for t in range(5, n):
            w = h[t - 5:t]
            if np.isfinite(w).any():
                hmax5[t] = np.nanmax(w)
        out["T2"] = c > hmax5
        full = np.concatenate([pc_, c])
        rsi, _, _ = rsi_state(full)
        r = rsi[len(pc_):]
        r1 = np.concatenate([[rsi[len(pc_) - 1]], r[:-1]])
        out["T3"] = (r > 10) & (r1 <= 10)
        f1 = np.concatenate([[np.nan], fast[:-1]])
        out["T4"] = (fast >= -20) & (f1 < -20)
    for k in out:
        out[k] = np.asarray(out[k], bool) & True
    return out, chg


def refractory_pick(mask_row):
    """Greedy: the first True bar, then the next True bar >= 30 bars later, ... Pure."""
    idx = np.where(mask_row)[0]
    out, nxt = [], -1
    for t in idx:
        if t >= nxt:
            out.append(int(t))
            nxt = t + REFRACT
    return out


def beta_gross(o, c, spy_o, spy_c, beta):
    """bp: (c[t+30]/o[t+1]-1) - beta*(SPY same), for t in 0..389 (NaN where unavailable). Pure."""
    n = len(c)
    g = np.full(n, np.nan)
    t = np.arange(n - HOLD)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = c[t + HOLD] / o[t + 1] - 1
        s = spy_c[t + HOLD] / spy_o[t + 1] - 1
    g[: n - HOLD] = (r - beta * s) * 1e4
    return g


def control_lifts(G, M, order, pxok):
    """lift[k,t] = G[k,t] - mean G of the first 5 OTHER names in order[t] with M[j,t] & pxok[j,t] & finite G. Pure."""
    U, n = G.shape
    L = np.full((U, n), np.nan)
    ok = M & pxok & np.isfinite(G)
    for t in range(n):
        if not ok[:, t].any():
            continue
        first = [j for j in order[t] if ok[j, t]][:6]
        if len(first) < 2:
            continue
        g6 = G[first, t]
        for k in range(U):
            if not np.isfinite(G[k, t]):
                continue
            peers = [x for j, x in zip(first, g6) if j != k][:5]
            if peers:
                L[k, t] = G[k, t] - float(np.mean(peers))
    return L


# ---------------------------------------------------------------- build (per day, CPU)

def beta_update(hist, syms, c, spy_c):
    """Append today's per-name sums for 1-minute return regressions on SPY. hist: sym -> deque of (n, sx, sy, sxy, syy)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        y = np.diff(np.log(spy_c))
    for k, s in enumerate(syms):
        with np.errstate(invalid="ignore", divide="ignore"):
            x = np.diff(np.log(c[k]))
        ok = np.isfinite(x) & np.isfinite(y)
        if ok.sum() < 30:
            continue
        xx, yy = x[ok], y[ok]
        hist.setdefault(s, deque(maxlen=BETA_DAYS)).append((len(xx), xx.sum(), yy.sum(), (xx * yy).sum(), (yy * yy).sum()))


def beta_of(hist, s):
    d = hist.get(s)
    if not d or len(d) < BETA_MIN_DAYS:
        return 1.0
    n, sx, sy, sxy, syy = (sum(z[i] for z in d) for i in range(5))
    var = syy / n - (sy / n) ** 2
    return float((sxy / n - (sx / n) * (sy / n)) / var) if var > 0 else 1.0


def build_day(day, d, pday, betas):
    """-> dict with per-stack events (name idx, bar) and per-(m,d) pool samples, and lift matrices per M."""
    U = len(d["syms"])
    spy_o, spy_c = d["spy_o"], d["spy_c"]
    L_ = {}
    lays = []
    pxok = np.zeros((U, 390), bool)
    G = np.full((U, 390), np.nan)
    for k in range(U):
        lay, _ = layers(d["o"][k], d["h"][k], d["l"][k], d["c"][k], d["v"][k],
                        pday["c"][k], pday["h"][k], pday["l"][k], pday["v"][k], spy_c)
        lays.append(lay)
        with np.errstate(invalid="ignore"):
            pxok[k] = (d["c"][k] >= PX_LO) & (d["c"][k] <= PX_HI)
        G[k] = beta_gross(d["o"][k], d["c"][k], spy_o, spy_c, betas[k])
    win = np.zeros(390, bool)
    win[T_LO:T_HI + 1] = True
    order = [np.random.default_rng(hkey("stack-ctl", day, t) % 2 ** 32).permutation(U) for t in range(390)]
    Mmat = {m: np.array([lay[m] for lay in lays]) for m in MS}
    for m in MS:
        L_[m] = control_lifts(G, Mmat[m], order, pxok).astype(np.float32)
    events = {}
    pools = {}
    skips = {}
    for m in MS:
        for dd in DS:
            md = Mmat[m] & np.array([lay[dd] for lay in lays]) & pxok & win[None, :] & np.isfinite(L_[m])
            pos = np.argwhere(md)
            rng = np.random.default_rng(hkey("stack-pool", day, m, dd) % 2 ** 32)
            take = pos[rng.choice(len(pos), size=min(POOL_PER_DAY, len(pos)), replace=False)] if len(pos) else pos
            pools[(m, dd)] = (int(len(pos)), take.astype(np.int16))
            for tt in TS:
                mask = md & np.array([lay[tt] for lay in lays])
                ev = [(k, t) for k in range(U) for t in refractory_pick(mask[k])]
                events[(m, dd, tt)] = np.array(ev, np.int16).reshape(-1, 2)
                skips[(m, dd, tt)] = int(mask.sum()) - len(ev)
    return {"day": day, "syms": d["syms"], "L": L_, "events": events, "pools": pools, "G": G.astype(np.float32),
            "M4": Mmat["M4"], "px": pxok, "Mmat": Mmat, "skips": skips,
            "missing_bars": int(np.isnan(d["c"][:, T_LO:T_HI + 1]).sum()), "beta_default": int(sum(b == 1.0 for b in betas))}


def load_days_pairs(lo, hi):
    from evening_family import WH_SIP
    from opening_grid import CONFIRM as _  # noqa: F401  (same warehouse as opening_grid)
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    return [(files[i], files[i - 1]) for i in range(1, len(files)) if lo <= files[i].stem <= hi]


def day_arrays(fp, prev):
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import ALL_ETF
    day, pday = load_day(fp), load_day(prev)
    if day["idx"].get("SPY") is None or pday["idx"].get("SPY") is None:
        return None, None
    syms = [s for s in universe(day, pday, set(ALL_ETF)) if s in pday["idx"]]
    ix = np.array([day["idx"][s] for s in syms])
    px = np.array([pday["idx"][s] for s in syms])
    k, pk = day["idx"]["SPY"], pday["idx"]["SPY"]
    d = {"syms": syms, **{a: day[a][ix].astype(float) for a in "ohlcv"},
         "spy_o": day["o"][k].astype(float), "spy_c": day["c"][k].astype(float)}
    p = {**{a: pday[a][px].astype(float) for a in "ohlcv"}, "spy_c": pday["c"][pk].astype(float)}
    return d, p


def build():
    CACHE.mkdir(parents=True, exist_ok=True)
    hist = {}
    pairs = load_days_pairs("2026-03-01", CONFIRM[1])
    for fp, prev in pairs:
        d, p = day_arrays(fp, prev)
        if d is None:
            continue
        dst = CACHE / f"{fp.stem}.pkl"
        if fp.stem >= SEARCH[0] and not dst.exists():
            betas = [beta_of(hist, s) for s in d["syms"]]
            res = build_day(fp.stem, d, p, betas)
            pickle.dump(res, open(dst, "wb"))
            print(fp.stem, len(d["syms"]), sum(len(v) for v in res["events"].values()), flush=True)
        beta_update(hist, d["syms"], d["c"], d["spy_c"])     # betas use only PRIOR sessions


# ---------------------------------------------------------------- scoring (pure on gathered arrays)

def day_stats(lifts, days):
    """Day-clustered mean, SE, t from per-event lifts and day labels. Pure."""
    lifts, days = np.asarray(lifts, float), np.asarray(days)
    ok = np.isfinite(lifts)
    lifts, days = lifts[ok], days[ok]
    if len(lifts) < 3:
        return None, None, None, len(lifts)
    u, inv = np.unique(days, return_inverse=True)
    if len(u) < 3:
        return float(lifts.mean()), None, None, len(lifts)
    m = np.bincount(inv, lifts) / np.bincount(inv)
    se = m.std(ddof=1) / math.sqrt(len(m))
    return float(m.mean()), float(se), (float(m.mean() / se) if se > 0 else None), len(lifts)


def cap_rows(rows, cap=CAP):
    """rows: list of (day, sym, t, lift) -> the cap lowest sha256('stack'|day|sym|t). Pure."""
    return sorted(rows, key=lambda r: hkey("stack", r[0], r[1], r[2]))[:cap]


def stack_stats(rows):
    ra, rb = [r for r in rows if r[0] < SPLIT], [r for r in rows if r[0] >= SPLIT]
    a, b = cap_rows(ra), cap_rows(rb)
    ma, sea, ta, na = day_stats([r[3] for r in a], [r[0] for r in a])
    mb, seb, tb, nb = day_stats([r[3] for r in b], [r[0] for r in b])
    mp, sep, tp, npp = day_stats([r[3] for r in a + b], [r[0] for r in a + b])
    return {"n_raw_h1": len(ra), "n_raw_h2": len(rb), "n_h1": na, "n_h2": nb, "lift_h1": ma, "lift_h2": mb, "t": tp, "lift": mp,
            "mde_h1": 3.84 * sea if sea else None, "mde_h2": 3.84 * seb if seb else None, "rows": a + b}


def choose(stats):
    ok = [(s["t"], k) for k, s in stats.items() if s["t"] is not None and (s["mde_h1"] or 99) <= MDE_MAX
          and (s["mde_h2"] or 99) <= MDE_MAX and (s.get("n_h1") or 0) >= 300 and (s.get("n_h2") or 0) >= 300]
    return max(ok)[1] if ok else None


def null_winner(pools, counts, rng):
    """One luck-null search: per stack, counts[stack] = (n_h1, n_h2) random draws from its (m, d) pool per half
    (days weighted by pool size), same choice rule -> the winner's mean lift. pools[(m,d)][half] = (lifts, days, w)."""
    st = {}
    for s, (n1, n2) in counts.items():
        hv = {0: (np.array([]), np.array([])), 1: (np.array([]), np.array([]))}
        for half, n_ in ((0, n1), (1, n2)):
            lifts, days, w = pools[(s[0], s[1])][half]
            if n_ and len(lifts):
                idx = rng.choice(len(lifts), size=n_, replace=len(lifts) < n_, p=w)
                hv[half] = (lifts[idx], days[idx])
        la, lb = hv[0], hv[1]
        if not len(la[0]) and not len(lb[0]):
            continue
        _, sea, _, _ = day_stats(*la)
        _, seb, _, _ = day_stats(*lb)
        mp, _, tp, _ = day_stats(np.concatenate([la[0], lb[0]]), np.concatenate([la[1], lb[1]]))
        st[s] = {"t": tp, "lift": mp, "mde_h1": 3.84 * sea if sea else None, "mde_h2": 3.84 * seb if seb else None,
                 "n_h1": n1, "n_h2": n2}
    w = choose(st)
    return st[w]["lift"] if w else None


# ---------------------------------------------------------------- search (gather + choose + null + canaries)

def gather(lo, hi, variant="base", plant=None):
    """-> (rows per stack: list of (day, sym, t, lift)), pools per (m,d) per half. variant: base | permute | plantM4 |
    plantStack (canaries)."""
    rows = defaultdict(list)
    praw = defaultdict(lambda: ([[], [], []], [[], [], []]))
    for p in sorted(CACHE.glob("*.pkl")):
        if not (lo <= p.stem <= hi):
            continue
        r = pickle.load(open(p, "rb"))
        day, syms = r["day"], r["syms"]
        L = {m: r["L"][m].astype(float) for m in MS}
        if variant == "permute":
            perm = np.random.default_rng(hkey("stack-canary", day) % 2 ** 32).permutation(len(syms))
            L = {m: L[m][perm] for m in MS}
        if variant == "plantM4":     # plant +15 bp on the GROSS of every M4 bar, then rebuild the matched lifts
            Gp = r["G"].astype(float) + 15.0 * r["M4"]
            order = [np.random.default_rng(hkey("stack-ctl", day, t) % 2 ** 32).permutation(len(syms)) for t in range(390)]
            L = {m: control_lifts(Gp, r["Mmat"][m], order, r["px"]) for m in MS}
        half = 0 if day < SPLIT else 1
        for s, ev in r["events"].items():
            for k, t in ev:
                x = L[s[0]][k, t]
                if variant == "plantStack" and s == plant:
                    x = x + 15.0
                if np.isfinite(x):
                    rows[s].append((day, syms[k], int(t), float(x)))
        for (m, dd), (n_pool, take) in r["pools"].items():
            if not len(take):
                continue
            lv = L[m][take[:, 0], take[:, 1]]
            ok = np.isfinite(lv)
            P = praw[(m, dd)][half]
            P[0].append(lv[ok])
            P[1].append(np.full(ok.sum(), day))
            P[2].append(np.full(ok.sum(), n_pool / max(len(take), 1)))
    pools = {}
    for key, halves in praw.items():
        out = []
        for P in halves:
            if P[0]:
                lifts, days, w = np.concatenate(P[0]), np.concatenate(P[1]), np.concatenate(P[2])
                out.append((lifts, days, w / w.sum()))
            else:
                out.append((np.array([]), np.array([]), np.array([])))
        pools[key] = out
    return rows, pools


def chosen_controls(rows):
    """TEST_STANDARD controls for the chosen stack, on beta-adjusted gross G: (a) same name, an entry in exit+2..exit+62
    (bars t+32..t+92, <= 359); (b) unconditional: a random filtered (name, minute) on the same day. Pure given cache."""
    by_day = defaultdict(list)
    for r in rows:
        by_day[r[0]].append(r)
    later, unc, ev_g, unc_g, later_g = [], [], [], [], []
    for day, rr in by_day.items():
        c = pickle.load(open(CACHE / f"{day}.pkl", "rb"))
        G, px, syms = c["G"].astype(float), c["px"], c["syms"]
        ix = {s: i for i, s in enumerate(syms)}
        ok = np.argwhere(px & np.isfinite(G))
        ok = ok[(ok[:, 1] >= T_LO) & (ok[:, 1] <= T_HI)]
        for d_, sym, t, _ in rr:
            k = ix[sym]
            g = G[k, t]
            if not np.isfinite(g):
                continue
            cand = [u for u in range(t + 32, min(t + 92, T_HI) + 1) if np.isfinite(G[k, u])]
            if cand:
                u = cand[hkey("stack-later", day, sym, t) % len(cand)]
                later.append((day, g - G[k, u]))
                later_g.append(G[k, u])
            if len(ok):
                j, u = ok[hkey("stack-unc", day, sym, t) % len(ok)]
                unc.append((day, g - G[j, u]))
                ev_g.append((day, g))
                unc_g.append((day, G[j, u]))
    ml, _, tl, nl = day_stats([x[1] for x in later], [x[0] for x in later])
    mu, _, tu, nu = day_stats([x[1] for x in unc], [x[0] for x in unc])
    return {"lift_same_name_later": ml, "t_later": tl, "n_later": nl, "lift_unconditional": mu, "t_unc": tu, "n_unc": nu,
            "event_gross_mean": float(np.mean([x[1] for x in ev_g])) if ev_g else None,
            "unconditional_gross_mean": float(np.mean([x[1] for x in unc_g])) if unc_g else None,
            "later_gross_mean": float(np.mean(later_g)) if later_g else None,
            "later_coverage": round(nl / max(len(rows), 1), 3)}


def search_once(rows, pools, n_null=N_NULL, seed=20261120):
    stats = {s: stack_stats(rows.get(s, [])) for s in STACKS}
    win = choose(stats)
    counts = {s: (st["n_h1"], st["n_h2"]) for s, st in stats.items()}
    rng = np.random.default_rng(seed)
    nulls = [w for w in (null_winner(pools, counts, rng) for _ in range(n_null)) if w is not None]
    p95 = float(np.percentile(nulls, 95)) if nulls else None
    return stats, win, p95


def edge(st, p95):
    return (st["lift_h1"] or -1) > 0 and (st["lift_h2"] or -1) > 0 and (st["t"] or 0) >= EDGE_T and \
        p95 is not None and (st["lift"] or -99) > p95


def search():
    import time
    OUT.mkdir(parents=True, exist_ok=True)
    rows, pools = gather(*SEARCH)
    stats, win, p95 = search_once(rows, pools)
    table = {sid(s): {k: (round(v, 2) if isinstance(v, float) else v) for k, v in st.items() if k != "rows"}
             for s, st in stats.items()}
    (OUT / "choice.json").write_text(json.dumps({"chosen": sid(win) if win else None, "null_p95": p95,
                                                 "written": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1))
    out = {"prereg": "docs/studies/stack_search_prereg.json (amended_1)", "chosen": sid(win) if win else None,
           "null_winner_lift_p95": p95, "stacks": table}
    out["verdict"] = "UNPROVEN (no stack with MDE <= 10 bp and n >= 300 in both halves)" if not win else None
    counts = {"refractory_skips": defaultdict(int), "missing_bars": 0, "beta_default_name_days": 0}
    for p in sorted(CACHE.glob("*.pkl")):
        if SEARCH[0] <= p.stem <= SEARCH[1]:
            r = pickle.load(open(p, "rb"))
            for k, v in r["skips"].items():
                counts["refractory_skips"][sid(k)] += v
            counts["missing_bars"] += r["missing_bars"]
            counts["beta_default_name_days"] += r["beta_default"]
    out["counts"] = {k: (dict(v) if isinstance(v, defaultdict) else v) for k, v in counts.items()}
    if win:
        ctl = chosen_controls(stats[win]["rows"])
        out["chosen_controls"] = ctl
        out["EDGE"] = edge(stats[win], p95) and (ctl["lift_same_name_later"] or -1) > 0 and (ctl["lift_unconditional"] or -1) > 0
        out["verdict"] = "EDGE on SEARCH (quotes, CONFIRM, FORWARD next)" if out["EDGE"] else "NO EDGE"
    # marginal layer table (information)
    marg = {}
    for i, vals in enumerate((MS, DS, TS)):
        for v in vals:
            ls = [stats[s]["lift"] for s in STACKS if s[i] == v and stats[s]["lift"] is not None]
            marg[v] = round(float(np.mean(ls)), 2) if ls else None
    out["marginal_layers"] = marg
    # canaries (amended_1 #3, code-review fixes)
    del rows, pools
    can = {}
    r_, p_ = gather(*SEARCH, variant="permute")
    st_, w_, p95_ = search_once(r_, p_, n_null=100)
    can["a_permuted_does_not_beat_null"] = not (w_ and (st_[w_]["lift"] or -99) > (p95_ if p95_ is not None else 99))
    can["a_detail"] = {"winner": sid(w_) if w_ else None, "lift": st_[w_]["lift"] if w_ else None,
                       "t": st_[w_]["t"] if w_ else None, "p95": p95_}
    del r_, p_
    r_, p_ = gather(*SEARCH, variant="plantM4")
    st_, w_, p95_ = search_once(r_, p_, n_null=20)
    bad = []
    for s_ in STACKS:
        z = (s_[0], s_[1], "T0")
        if s_[2] == "T0" or None in (st_[s_]["lift"], stats[s_]["lift"], st_[z]["lift"], stats[z]["lift"]):
            continue
        if (st_[s_]["lift"] - stats[s_]["lift"]) - (st_[z]["lift"] - stats[z]["lift"]) > 5.0:
            bad.append(sid(s_))
    can["b_selection_plant_not_credited_to_timing"] = not bad
    can["b_flagged"] = bad
    del r_, p_
    elig = [(min(st["n_h1"], st["n_h2"]), sid(s_), s_) for s_, st in stats.items()
            if "0" not in "".join(x[1] for x in s_) and (st["mde_h1"] or 99) <= MDE_MAX and (st["mde_h2"] or 99) <= MDE_MAX]
    plant = max(elig)[2] if elig else None      # most events; ties broken by the larger stack id (lexicographic)
    if plant:
        r_, p_ = gather(*SEARCH, variant="plantStack", plant=plant)
        st_, w_, p95_ = search_once(r_, p_, n_null=100)
        can["c_planted_stack"] = sid(plant)
        can["c_planted_stack_found"] = bool(w_ == plant and edge(st_[w_], p95_))
    else:
        can["c_planted_stack_found"] = None
    out["canaries"] = can
    (OUT / "search.json").write_text(json.dumps(out, indent=1, default=str))
    top = sorted(((st["t"] or -99, s) for s, st in stats.items()), reverse=True)[:20]
    lines = ["# Stack search (gross, beta-adjusted lift vs M-matched same-minute control)", "",
             f"verdict {out['verdict']}  chosen {out['chosen']}  null p95 {p95}", f"canaries {json.dumps(can, default=str)}",
             f"chosen controls {json.dumps(out.get('chosen_controls'), default=str)}", "",
             "| stack | n h1/h2 | lift h1 | lift h2 | lift | t | MDE h1/h2 |", "|---|---|---|---|---|---|---|"]
    for _, s in top:
        t_ = table[sid(s)]
        lines.append(f"| {sid(s)} | {t_['n_h1']}/{t_['n_h2']} | {t_['lift_h1']} | {t_['lift_h2']} | {t_['lift']} | {t_['t']} | "
                     f"{None if t_['mde_h1'] is None else round(t_['mde_h1'], 1)}/{None if t_['mde_h2'] is None else round(t_['mde_h2'], 1)} |")
    lines += ["", f"marginal layer lifts: {json.dumps(marg)}"]
    (OUT / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return out


if __name__ == "__main__":
    {"build": build, "search": search}[sys.argv[1]]()
