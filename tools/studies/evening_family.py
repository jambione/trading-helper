#!/usr/bin/env python3
"""evening_family (docs/studies/evening_family_2026-11-07_prereg.json, skeptic rounds 1-2 + code review applied). Read-only, SIP warehouses.

S  surge continuation, stocks (R5 rule unchanged), VERDICT on warehouse_confirm; the 120-day read is a discovery re-score.
E  surge continuation on 12 ETFs (per-ETF costs from ai_reports/warehouse/etf/cost.json; stress = max(2x, actual NBBO spreads)).
L  sector-ETF lead-lag (stock residual 2-bar return in [-15, +2] bp after its mapped ETF jumps).
R  live pre-square arm with fast %R length 21 vs 10; scored as a head-to-head of primary-control lifts (R-H2H).

    .venv/bin/python tools/studies/evening_family.py count    # outcome-blind trigger counts only (decides E's threshold); writes count.json
    .venv/bin/python tools/studies/evening_family.py run      # ONE read: scores, runs canaries on a seeded day sample, pickles run.pkl (refuses a second run)
    .venv/bin/python tools/studies/evening_family.py report   # canaries must pass, then writes docs/EVENING_FAMILY.md
"""
from __future__ import annotations

import hashlib
import json
import math
import pickle
import sys
from collections import defaultdict, deque
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day, prev_weekday  # noqa: E402
from board_structure_family import universe, seeded, tstat, residual_stats  # noqa: E402
from bar_signal_family import wr_smoothed  # noqa: E402

WH_SIP = ROOT / "ai_reports" / "warehouse" / "sip"
WH_CONF = ROOT / "ai_reports" / "warehouse_confirm" / "sip"
WH_IEX = ROOT / "ai_reports" / "warehouse" / "iex"
WH_ETF = ROOT / "ai_reports" / "warehouse" / "etf"
OUT = ROOT / "ai_reports" / "evening_family"
COST, STRESS = 6.0, 10.0
ALPHA = 0.05 / 4
FIRST, LAST = 15, 360
REFRACT = 30
NDRAW = 5
CANARY_MOD = 5          # full-canary days: 1 in 5 by hash (widened from 1 in 8 on 11/07 after an OUTCOME-BLIND count showed 9 days per key; logged in the prereg)
CANARY_MIN_DAYS = 12    # the null canary needs >= 10 days with rows; 12 leaves margin (4th review)
SECTOR = ["SMH", "SOXX", "IBIT", "IGV", "XBI", "URA", "XLE", "XLF", "ARKK"]
ALL_ETF = SECTOR + ["QQQ", "IWM", "SPY"]
HOLD = {"S": 5, "E": 5, "L": 10, "R21": 10, "R10": 10}
KEYS = ("S", "E", "L", "R21", "R10")


# ---------------- generic pieces (pure) ----------------
def r1(c):
    out = np.full(len(c), np.nan)
    out[1:] = c[1:] / c[:-1] - 1
    return out


def vol_ratio(v, t, k=15, need=10):
    """v(t) / MEAN(v[t-k..t-1]) with >= need bars present (the R5 definition)."""
    if t < k:
        return np.nan
    w = v[t - k:t]
    n = int(np.sum(~np.isnan(w)))
    if n < need:
        return np.nan
    m = np.nanmean(w)
    return v[t] / m if m > 0 and not np.isnan(v[t]) else np.nan


def first_true(cond_ts, refract=REFRACT):
    """Keep the first trigger; restart the clock only at a kept trigger."""
    kept, last = [], -10 ** 9
    for t in sorted(cond_ts):
        if t - last >= refract:
            kept.append(t)
            last = t
    return kept


def trade(o, c, t, H, cost):
    """Buy open t+1, sell close t+H. None when a needed bar is missing (skipped and counted by the caller)."""
    if t + H >= 390 or t + 1 >= 390 or np.isnan(o[t + 1]) or np.isnan(c[t + H]) or o[t + 1] <= 0:
        return None
    return (c[t + H] / o[t + 1] - 1) * 1e4 - cost


def later_minutes(t, H, trig):
    """Secondary control candidates: t+H+2 .. t+H+62 inside 15..360, clear of [x, x+H+1] for every trigger x <= m (triggers after m
    are future information at m and are NOT used). Pass ALL of the instrument's kept trigger times."""
    return [m for m in range(max(FIRST, t + H + 2), min(LAST, t + H + 62) + 1) if all(not (x <= m <= x + H + 1) for x in trig if x <= m)]


def primary_pool(candidates, trig, t, me, exclude=()):
    """Same-minute control pool: other instruments that did NOT trigger in [t-15, t] (known at the decision minute). Triggers after t
    are future information and are never used (round-2 look-ahead fix)."""
    return [m for m in candidates if m != me and m not in exclude and not any(t - 15 <= x <= t for x in trig.get(m, []))]


def sample_k(rng, pool, k=NDRAW):
    """Without replacement."""
    pool = list(pool)
    return rng.sample(pool, min(k, len(pool)))


def residuals(hist, names, beta):
    """Residual 1-minute return vectors (09:35 on) per name over the history sessions, with the GIVEN beta."""
    R, S = {n: [] for n in names}, []
    for d in hist:
        si = d["idx"].get("SPY")
        if si is None:
            return {}
        spy = d["c"][si]
        S.append(spy[6:] / spy[5:-1] - 1)
        for n in names:
            i = d["idx"].get(n)
            R[n].append(np.full(384, np.nan) if i is None else d["c"][i][6:] / d["c"][i][5:-1] - 1)
    S = np.concatenate(S)
    return {n: np.concatenate(R[n]) - beta[n] * S for n in names if n in beta}


def etf_betas(hist, names):
    """Plain beta vs SPY for the ETFs over the history sessions (>= 500 points)."""
    S, R = [], {n: [] for n in names}
    for d in hist:
        si = d["idx"].get("SPY")
        if si is None:
            continue
        spy = d["c"][si]
        S.append(spy[6:] / spy[5:-1] - 1)
        for n in names:
            i = d["idx"].get(n)
            R[n].append(np.full(384, np.nan) if i is None else d["c"][i][6:] / d["c"][i][5:-1] - 1)
    if not S:
        return {}
    S = np.concatenate(S)
    out = {}
    for n in names:
        x = np.concatenate(R[n])
        m = ~np.isnan(x) & ~np.isnan(S)
        if m.sum() >= 500 and np.dot(S[m], S[m]) > 0:
            out[n] = float(np.dot(x[m], S[m]) / np.dot(S[m], S[m]))
    return out


def corr(a, b):
    m = ~np.isnan(a) & ~np.isnan(b)
    if m.sum() < 300:
        return np.nan
    x, y = a[m], b[m]
    sx, sy = x.std(), y.std()
    return float(((x - x.mean()) * (y - y.mean())).mean() / (sx * sy)) if sx > 0 and sy > 0 else np.nan


# ---------------- %R pre-square (R) ----------------
def minute_grid(prev_c, prev_h, prev_l, c, h, l):  # noqa: E741
    """Previous session's last 112 RTH bars + today's, short gaps filled flat. As signals._minute_grid_pr, gaps longer than
    MINUTE_GAP_FILL_MAX (a bar-time gap > 15 minutes, i.e. > 14 missing minutes) stay NaN (so no trigger can fire on a filled minute that has no real bar after a long silence)."""
    def fill(cc, hh, ll):
        cc, hh, ll = np.array(cc, float), np.array(hh, float), np.array(ll, float)
        last, gap = np.nan, 0
        for i in range(len(cc)):
            if np.isnan(cc[i]):
                gap += 1
                if gap <= 14 and not np.isnan(last):
                    cc[i] = hh[i] = ll[i] = last
            else:
                gap = 0
                last = cc[i]
                if np.isnan(hh[i]):
                    hh[i] = cc[i]
                if np.isnan(ll[i]):
                    ll[i] = cc[i]
        return cc, hh, ll
    pc, ph, pl = fill(prev_c[-112:], prev_h[-112:], prev_l[-112:])
    tc, th, tl = fill(c, h, l)
    return np.concatenate([pc, tc]), np.concatenate([ph, th]), np.concatenate([pl, tl]), len(pc)


def presquare_bars(cc, hh, ll, off, L):
    """Today's bar indexes where the live pre-square is true on this bar AND the previous one, and not the one before (trigger = the
    second). rte_direction_eps 0.05 (config.py:1139). Cites ai_entry_watch.py:13087-13148."""
    fast = wr_smoothed(hh, ll, cc, n=L, span=7)
    slow = wr_smoothed(hh, ll, cc, n=112, span=3)
    ok = np.zeros(len(cc), dtype=bool)
    for i in range(1, len(cc)):
        f, s, fp = fast[i], slow[i], fast[i - 1]
        if np.isnan(f) or np.isnan(s) or np.isnan(fp):
            continue
        ok[i] = f >= -35 and s >= -35 and abs(f - s) <= 15 and not (f >= -20 and s >= -20) and f - fp > 0.05
    return [i - off for i in range(off + 2, len(cc)) if ok[i] and ok[i - 1] and not ok[i - 2] and FIRST <= i - off <= LAST]


# ---------------- evaluation ----------------
def crit_t(days):
    from scipy import stats
    return float(stats.t.ppf(1 - ALPHA, max(1, days - 1)))


def evaluate(rows, min_days=60, min_half_days=25, min_n=300, min_half_n=100):
    """rows: [{d, net, net_s, ca (primary), cb (secondary)}] -> verdict dict per docs/TEST_STANDARD.md. crit uses scored days
    = days with at least one trigger that has a primary control."""
    if not rows:
        return {"status": "UNDERPOWERED", "n": 0}
    days = sorted({r["d"] for r in rows})
    mid = days[len(days) // 2]

    def dmean(rs, f):
        out = {}
        for r in rs:
            v = f(r)
            if v is not None:
                out.setdefault(r["d"], []).append(v)
        return {d: sum(v) / len(v) for d, v in out.items()}
    lift = dmean(rows, lambda r: None if r.get("ca") is None else r["net"] - r["ca"])
    liftb = dmean(rows, lambda r: None if r.get("cb") is None else r["net"] - r["cb"])
    net = dmean(rows, lambda r: r["net"])
    nets = dmean(rows, lambda r: r.get("net_s"))
    lifts = sorted(r["net"] - r["ca"] for r in rows if r.get("ca") is not None)
    cap = lifts[max(0, int(0.99 * len(lifts)) - 1)] if lifts else 0.0
    win = dmean(rows, lambda r: None if r.get("ca") is None else min(r["net"] - r["ca"], cap))

    def h(dd):
        a = [v for d, v in dd.items() if d < mid]
        b = [v for d, v in dd.items() if d >= mid]
        return (float(np.mean(a)) if a else float("nan"), float(np.mean(b)) if b else float("nan"))
    lv = list(lift.values())
    crit = crit_t(len(lv))
    t_l = tstat(lv)
    n_h = (sum(1 for r in rows if r["d"] < mid), sum(1 for r in rows if r["d"] >= mid))
    d_h = (sum(1 for d in net if d < mid), sum(1 for d in net if d >= mid))
    under = len(rows) < min_n or min(n_h) < min_half_n or len(net) < min_days or min(d_h) < min_half_days or not lv
    hl, hw, hn = h(lift), h(win), h(net)
    pos = lambda x: x == x and x > 0  # noqa: E731
    edge = (t_l >= crit and pos(hl[0]) and pos(hl[1]) and bool(liftb) and np.mean(list(liftb.values())) > 0 and pos(hw[0]) and pos(hw[1]))
    prof = edge and pos(hn[0]) and pos(hn[1]) and bool(nets) and np.mean(list(nets.values())) > 0
    status = "UNDERPOWERED" if under else ("PROFITABLE" if prof else ("EDGE" if edge else "FAIL"))
    cas = [r["ca"] for r in rows if r.get("ca") is not None]
    cbs = [r["cb"] for r in rows if r.get("cb") is not None]
    return {"status": status, "n": len(rows), "n_h": n_h, "days": len(lv), "d_h": d_h, "crit": crit,
            "lift": float(np.mean(lv)) if lv else float("nan"), "lift_t": t_l, "lift_h": hl, "liftb": float(np.mean(list(liftb.values()))) if liftb else float("nan"),
            "win_h": hw, "net": float(np.mean(list(net.values()))), "net_h": hn,
            "net_stress": float(np.mean(list(nets.values()))) if nets else float("nan"),
            "ca_mean": float(np.mean(cas)) if cas else float("nan"), "cb_mean": float(np.mean(cbs)) if cbs else float("nan")}


def void_status(ev, base_rows):
    """VOID when the primary control mean is more than 3 bp from the bar-of-day baseline, or the baseline/control is missing."""
    bm = float(np.mean([b["net"] for b in base_rows])) if base_rows else float("nan")
    if not base_rows or math.isnan(ev.get("ca_mean", float("nan"))) or math.isnan(bm):
        return True, bm
    return abs(ev["ca_mean"] - bm) > 3.0, bm


def h2h(res10, res21, min_days=60):
    """Per day, mean primary-control lift of L=10 minus L=21, days with both. -> dict."""
    def dl(rows):
        out = {}
        for r in rows:
            if r.get("ca") is not None:
                out.setdefault(r["d"], []).append(r["net"] - r["ca"])
        return {d: float(np.mean(v)) for d, v in out.items()}
    a10, a21 = dl(res10), dl(res21)
    both = sorted(set(a10) & set(a21))
    if len(both) < min_days:
        return {"status": "UNDERPOWERED", "days": len(both)}
    diff = [a10[d] - a21[d] for d in both]
    mid = both[len(both) // 2]
    h1 = float(np.mean([a10[d] - a21[d] for d in both if d < mid]))
    h2 = float(np.mean([a10[d] - a21[d] for d in both if d >= mid]))
    t, crit = tstat(diff), crit_t(len(both))
    return {"status": "L=10 BETTER" if (t >= crit and h1 > 0 and h2 > 0) else "NOT BETTER", "days": len(both),
            "mean": float(np.mean(diff)), "t": t, "crit": crit, "h": (h1, h2)}


# ---------------- E stress (actual NBBO spreads) ----------------
def e_stress_net(net, rt_bp, sp_entry, sp_exit):
    """Stress net = gross - max(2 x rt, spread at entry + spread at exit); net = gross - rt."""
    gross = net + rt_bp
    return gross - max(2 * rt_bp, sp_entry + sp_exit)


def attach_e_stress(rows, costs, spread_fn):
    """spread_fn(etf, day, minute_of_day) -> bp or None. Rows with a missing quote keep net_s None (PROFITABLE cannot fire for them)."""
    for r in rows:
        rt = costs[r["sym"]]["rt_bp"]
        a = spread_fn(r["sym"], r["d"], 570 + r["t"] + 1)
        b = spread_fn(r["sym"], r["d"], 570 + r["t"] + HOLD["E"])
        r["net_s"] = e_stress_net(r["net"], rt, a, b) if (a is not None and b is not None) else None
    return rows


# ---------------- per-day scoring ----------------
def score_day(day, prev, hist, names, d, etf_day=None, etf_hist=None, costs=None, e_thr=0.0020, which=("S", "E", "L", "R"), inject=None):
    """-> (rows by key + BASE baseline rows, stats). `inject` replaces the computed triggers (canaries): {"S": {sym:[t]}, "R21": {...},
    "R10": {...}, "E": {etf:[t]}, "L": [(etf, t)]}."""
    out = {k: [] for k in KEYS + ("BASE",)}
    out["TRIG"] = {}                      # COMPLETE trigger sets (including triggers whose trade was skipped) - needed to reproduce control pools in canary passes
    st = defaultdict(int)
    beta, _peers = residual_stats(hist, names)             # R5's beta, unchanged (present >= 15, >= 500 points)
    names = [n for n in names if n in beta and n in day["idx"]]
    spy = day["c"][day["idx"]["SPY"]]
    spy1 = r1(spy)
    A = {}
    for n in names:
        i = day["idx"][n]
        c, o, v = day["c"][i], day["o"][i], day["v"][i]
        A[n] = {"i": i, "o": o, "c": c, "v": v, "res1": r1(c) - beta[n] * spy1}

    def base_row(key, arrays, who, t, H, cost_of=lambda m: COST):
        if not arrays:
            return
        m0 = seeded("baseline", key, d, who, t).choice(sorted(arrays))
        b = trade(arrays[m0]["o"], arrays[m0]["c"], t, H, cost_of(m0))
        if b is not None:
            out["BASE"].append({"key": key, "net": b})

    def controls(key, who, arrays, t, H, trig_all, own_trig, cost_of, cand, exclude=(), flt=None):
        """-> (primary mean, secondary mean) with removal counts in st."""
        full = [m for m in cand if m != who]
        pool = primary_pool(full, trig_all, t, who, exclude)
        if flt is not None:
            pool = [m for m in pool if flt(m, t)]
        st[f"{key}_removed_primary"] += len(full) - len(pool)
        xs = [trade(arrays[m]["o"], arrays[m]["c"], t, H, cost_of(m)) for m in sample_k(seeded(who, d, f"{key}-a", t), pool)]
        xs = [x for x in xs if x is not None]
        raw = list(range(max(FIRST, t + H + 2), min(LAST, t + H + 62) + 1))
        lm = later_minutes(t, H, own_trig)
        if flt is not None:
            lm = [m for m in lm if flt(who, m)]
        st[f"{key}_removed_secondary"] += len(raw) - len(lm)
        ls_ = [trade(arrays[who]["o"], arrays[who]["c"], m, H, cost_of(who)) for m in sample_k(seeded(who, d, f"{key}-b", t), lm)]
        ls_ = [x for x in ls_ if x is not None]
        return (float(np.mean(xs)) if xs else None), (float(np.mean(ls_)) if ls_ else None)

    # ---- S ----
    if "S" in which:
        if inject and "S" in inject:
            trig = {n: first_true(ts) for n, ts in inject["S"].items() if n in A}
        else:
            trig = {n: first_true([t for t in range(FIRST, LAST + 1) if not np.isnan(A[n]["res1"][t]) and A[n]["res1"][t] >= 0.0020 and vol_ratio(A[n]["v"], t) >= 2.0]) for n in names}
        out["TRIG"]["S"] = {n: list(ts) for n, ts in trig.items() if ts}
        for n in names:
            for t in trig.get(n, []):
                net = trade(A[n]["o"], A[n]["c"], t, 5, COST)
                if net is None:
                    st["S_skipped"] += 1
                    continue
                ca, cb = controls("S", n, A, t, 5, trig, trig[n], lambda m: COST, names)
                out["S"].append({"d": d, "sym": n, "t": t, "net": net, "net_s": net + COST - STRESS, "ca": ca, "cb": cb})
                base_row("S", A, n, t, 5)
    # ---- R (both lengths) ----
    if "R" in which and prev is not None:
        trigR = {10: {}, 21: {}}
        elig = [n for n in names if prev["idx"].get(n) is not None]       # control candidates: same in normal and injected paths
        injected = bool(inject) and any(f"R{L}" in inject for L in (10, 21))
        if injected:
            for L in (10, 21):
                trigR[L] = {n: first_true(ts) for n, ts in inject.get(f"R{L}", {}).items() if n in A}
        else:
            for n in names:
                j = prev["idx"].get(n)
                if j is None:
                    continue
                i = A[n]["i"]
                cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], day["c"][i], day["h"][i], day["l"][i])
                if off < 112:
                    continue
                for L in (10, 21):
                    trigR[L][n] = first_true(presquare_bars(cc, hh, ll, off, L))
        for L in (10, 21):
            key = f"R{L}"
            out["TRIG"][key] = {n: list(ts) for n, ts in trigR[L].items() if ts}
            for n, ts in trigR[L].items():
                for t in ts:
                    net = trade(A[n]["o"], A[n]["c"], t, 10, COST)
                    if net is None:
                        st[f"{key}_skipped"] += 1
                        continue
                    ca, cb = controls(key, n, A, t, 10, trigR[L], ts, lambda m: COST, elig)
                    out[key].append({"d": d, "sym": n, "t": t, "net": net, "net_s": net + COST - STRESS, "ca": ca, "cb": cb})
                    base_row(key, A, n, t, 10)
    # ---- E and L need ETF bars aligned to the stock history ----
    if etf_day is not None and "SPY" not in etf_day["idx"] and ("E" in which or "L" in which):
        st["etf_history_incomplete_days"] += 1
    elif etf_day is not None and etf_hist is not None and len(etf_hist) == len(hist) and ("E" in which or "L" in which):
        ebeta = etf_betas(etf_hist, [x for x in ALL_ETF if x != "SPY"])
        ebeta["SPY"] = 1.0
        espy = etf_day["c"][etf_day["idx"]["SPY"]]
        E = {}
        for x in ALL_ETF:
            k = etf_day["idx"].get(x)
            if k is None or x not in ebeta:
                continue
            c = etf_day["c"][k]
            E[x] = {"o": etf_day["o"][k], "c": c, "v": etf_day["v"][k], "res1": r1(c) if x == "SPY" else r1(c) - ebeta[x] * r1(espy)}
        if "E" in which:
            if inject and "E" in inject:
                trigE = {x: first_true(ts) for x, ts in inject["E"].items() if x in E}
            else:
                trigE = {x: first_true([t for t in range(FIRST, LAST + 1) if not np.isnan(a["res1"][t]) and a["res1"][t] >= e_thr and vol_ratio(a["v"], t) >= 2.0]) for x, a in E.items()}
            if "SMH" in trigE and "SOXX" in trigE:
                trigE["SOXX"] = [t for t in trigE["SOXX"] if t not in set(trigE["SMH"])]
            out["TRIG"]["E"] = {x: list(ts) for x, ts in trigE.items() if ts}
            rt = lambda m: costs[m]["rt_bp"]  # noqa: E731 (a missing cost raises: no silent 2 bp)
            for x, ts in trigE.items():
                for t in ts:
                    net = trade(E[x]["o"], E[x]["c"], t, 5, rt(x))
                    if net is None:
                        st["E_skipped"] += 1
                        continue
                    dup = {"SMH": "SOXX", "SOXX": "SMH"}.get(x)
                    ca, cb = controls("E", x, E, t, 5, trigE, ts, rt, list(E), exclude=(dup,))
                    out["E"].append({"d": d, "sym": x, "t": t, "net": net, "net_s": None, "ca": ca, "cb": cb})
                    base_row("E", E, x, t, 5, rt)
        if "L" in which:
            eres = residuals(etf_hist, [x for x in SECTOR if x in ebeta], ebeta)
            sres = residuals(hist, names, beta)
            etf_corr = {}
            for x in SECTOR:
                for y in SECTOR:
                    if x < y and x in eres and y in eres:
                        etf_corr[(x, y)] = etf_corr[(y, x)] = corr(eres[x], eres[y])
            mapped = {}
            for n in names:
                if n not in sres:
                    continue
                best, bc = None, 0.30
                for x in SECTOR:
                    if x in eres:
                        cr = corr(sres[n], eres[x])
                        if not np.isnan(cr) and cr >= bc:
                            best, bc = x, cr
                if best:
                    mapped[n] = best
            with np.errstate(invalid="ignore"):
                r2 = {n: (A[n]["c"] / np.roll(A[n]["c"], 2) - 1) - beta[n] * (spy / np.roll(spy, 2) - 1) for n in names}
            ok_f = {n: (r2[n] >= -0.0015) & (r2[n] <= 0.0002) for n in names}
            if inject and "L" in inject:
                ev = list(inject["L"])
            else:
                ev = []
                for x in SECTOR:
                    if x not in E:
                        continue
                    a = E[x]
                    with np.errstate(invalid="ignore"):
                        er2 = (a["c"] / np.roll(a["c"], 2) - 1) - ebeta[x] * (espy / np.roll(espy, 2) - 1)
                    ev += [(x, t) for t in range(FIRST, LAST + 1) if not np.isnan(er2[t]) and er2[t] >= 0.0015 and vol_ratio(a["v"], t) >= 2.0]
            order = {x: i for i, x in enumerate(SECTOR)}
            ev = sorted(set(ev), key=lambda z: (z[1], order[z[0]]))        # same order in the normal and injected paths
            out["TRIG"]["L"] = list(ev)
            ltrig = defaultdict(list)                       # per-stock kept L triggers
            kept = []                                       # PASS 1: every kept trigger (refractory + a usable trade), no controls yet
            event_hit = set()
            for x, t in ev:
                for n, mx in mapped.items():
                    if mx != x or not ok_f[n][t] or (ltrig[n] and t - ltrig[n][-1] < REFRACT):
                        continue
                    net = trade(A[n]["o"], A[n]["c"], t, 10, COST)
                    if net is None:
                        st["L_skipped"] += 1
                        continue
                    ltrig[n].append(t)
                    kept.append((n, x, t, net))
                    event_hit.add((x, t))
            st["L_events"] += len(event_hit)
            st["L_mapped_stocks"] += len(mapped)
            if not sres or not eres:
                st["L_no_residual_days"] += 1
            for n, x, t, net in kept:                       # PASS 2: controls see ALL kept triggers (only those <= t are used)
                excl = {x} | {y for y in SECTOR if etf_corr.get((x, y), 0) >= 0.5}
                cand = [m for m in names if mapped.get(m) not in excl]
                flt = lambda m, tt: bool(ok_f[m][tt])  # noqa: E731
                ca, cb = controls("L", n, A, t, 10, ltrig, ltrig[n], lambda m: COST, cand, flt=flt)
                out["L"].append({"d": d, "sym": n, "t": t, "etf": x, "net": net, "net_s": net + COST - STRESS, "ca": ca, "cb": cb})
                base_row("L", {m: A[m] for m in names if ok_f[m][t]}, n, t, 10)
    elif ("E" in which or "L" in which) and etf_day is not None:
        st["etf_history_incomplete_days"] += 1
    return out, dict(st)


# ---------------- canaries (through the real scoring path) ----------------
def triggers_of(out):
    """The COMPLETE trigger set of a pass (not only the triggers that produced a trade): a trigger whose trade was skipped is still excluded from
    other triggers' control pools in the real pass, so the injected pass must exclude it too."""
    T = out.get("TRIG") or {}
    res = {k: {sym: list(ts) for sym, ts in (T.get(k) or {}).items()} for k in ("S", "R21", "R10", "E")}
    res["L"] = list(T.get("L") or [])
    return res


def plant_copy(day, etf_day, out, bp, keys=KEYS):
    """Copies of the day arrays with +bp (basis points) added ONCE to the exit-bar close of every trigger row of the given keys
    (a bar shared by two rows is planted once, not twice)."""
    d2 = dict(day, c=day["c"].copy())
    e2 = dict(etf_day, c=etf_day["c"].copy()) if etf_day is not None else None
    done = set()
    for k in keys:
        for r in out[k]:
            t, H = r["t"], HOLD[k]
            if t + H >= 390:
                continue
            if k == "E":
                if e2 is None or r["sym"] not in e2["idx"]:
                    continue
                key = ("E", e2["idx"][r["sym"]], t + H)
                if key not in done:
                    e2["c"][key[1], t + H] *= (1 + bp / 1e4)
                    done.add(key)
            else:
                key = ("S", d2["idx"][r["sym"]], t + H)
                if key not in done:
                    d2["c"][key[1], t + H] *= (1 + bp / 1e4)
                    done.add(key)
    return d2, e2


def null_injection(out, names, etf_names, d):
    """Re-draw each trigger's instrument at random keeping each day's trigger count and bar-of-day mix."""
    inj = {"S": defaultdict(list), "R21": defaultdict(list), "R10": defaultdict(list), "E": defaultdict(list)}
    for k in ("S", "R21", "R10"):
        for r in out[k]:
            inj[k][seeded("null", k, d, r["sym"], r["t"]).choice(sorted(names))].append(r["t"])
    for r in out["E"]:
        inj["E"][seeded("null", "E", d, r["sym"], r["t"]).choice(sorted(etf_names))].append(r["t"])
    res = {k: dict(v) for k, v in inj.items()}
    res["L"] = sorted({(seeded("null", "L", d, x, t).choice(SECTOR), t) for x, t in {(r["etf"], r["t"]) for r in out["L"]}})
    return res


FAMILY = {"S": ("S",), "E": ("E",), "L": ("L",), "R21": ("R",), "R10": ("R",)}


def canary_day(day, prev, hist, names, d, etf_day, etf_hist, costs, e_thr, out1, which=("S", "E", "L", "R")):
    """-> {key: {"planted": per-trigger change in net (expect +8), "ctrl_shift": |change| of each primary control, "null_day_mean": [mean lift
    of the day's null rows] (one value per day, if any), "row_mismatch": rows present in one planted/real pass but not the other}}.
    ONE key is planted per pass (a planted exit bar of one family can be another family's control bar), the real triggers are injected for
    that family so detection is unaffected."""
    res = {k: {"planted": [], "ctrl_shift": [], "null_day_mean": [], "row_mismatch": 0} for k in KEYS}
    inj = triggers_of(out1)
    for k in KEYS:
        fam = FAMILY[k]
        if not set(fam) & set(which) or not out1[k]:
            continue
        d2, e2 = plant_copy(day, etf_day, out1, 8.0, keys=(k,))
        out2, _ = score_day(d2, prev, hist, names, d, e2, etf_hist, costs, e_thr, which=fam, inject=inj)
        a = {(r["sym"], r["t"]): r for r in out1[k]}
        b = {(r["sym"], r["t"]): r for r in out2[k]}
        res[k]["row_mismatch"] = len(set(a) ^ set(b))
        both = [x for x in a if x in b]
        res[k]["planted"] = [b[x]["net"] - a[x]["net"] for x in both]
        res[k]["ctrl_shift"] = [abs(b[x]["ca"] - a[x]["ca"]) for x in both if a[x]["ca"] is not None and b[x]["ca"] is not None]
    nul = null_injection(out1, names, ALL_ETF, d)
    out3, _ = score_day(day, prev, hist, names, d, etf_day, etf_hist, costs, e_thr, which=which, inject=nul)
    for k in KEYS:
        nl = [r["net"] - r["ca"] for r in out3[k] if r.get("ca") is not None]
        if nl:
            res[k]["null_day_mean"] = [float(np.mean(nl))]
    return res


def canary_verdict(acc):
    """acc: {key: {"planted": [...], "ctrl_shift": [...], "null_day_mean": [...], "row_mismatch": int}} accumulated over the sample days.
    The null test is on per-DAY mean lifts (rows within a day are clustered): |mean| <= 2 x the day-level SE, with >= 10 days."""
    bad = []
    for k in KEYS:
        a = acc.get(k) or {}
        pl, cs, nd = a.get("planted", []), a.get("ctrl_shift", []), a.get("null_day_mean", [])
        if a.get("row_mismatch", 0):
            bad.append(f"{k}: {a['row_mismatch']} rows differ between the real and planted passes")
        if len(pl) < 5:
            bad.append(f"{k}: too few canary triggers ({len(pl)}) to test")
            continue
        if abs(float(np.mean(pl)) - 8.0) > 0.5:
            bad.append(f"{k}: planted +8 bp came back as {np.mean(pl):+.2f}")
        if cs and max(cs) > 0.5:
            bad.append(f"{k}: a control moved by up to {max(cs):.2f} bp when only exits were changed")
        if len(nd) >= 10:
            se = float(np.std(nd, ddof=1) / math.sqrt(len(nd)))
            if abs(float(np.mean(nd))) > 2 * se:
                bad.append(f"{k}: null day-mean lift {np.mean(nd):+.2f} bp is outside 2 SE ({se:.2f}, {len(nd)} days)")
        else:
            bad.append(f"{k}: null canary has only {len(nd)} days (need 10)")
    return (not bad), bad


# ---------------- driver ----------------
def iter_days(wh, need_hist=20):
    from ticker_filters import is_not_stock
    from day_hold import asset_names
    nm = asset_names() or {}
    if sum(1 for k in nm if k != "__cached__") < 1000:
        raise SystemExit("asset names unavailable: refusing to run without the ETF/ETN exclusion")
    files = sorted(Path(wh).glob("20??-??-??.npz"))
    hist, prev, pd_, excl = deque(maxlen=need_hist), None, None, set()
    for f in files:
        dd = date.fromisoformat(f.stem)
        day = load_day(f)
        day["_date"] = f.stem
        if "SPY" not in day["idx"] or prev is None or pd_ != prev_weekday(dd):
            prev, pd_ = day, dd
            hist.append(day)
            continue
        for s in prev["syms"]:
            if s not in excl and is_not_stock(s, nm.get(s, "") or ""):
                excl.add(s)
        if len(hist) >= need_hist:
            yield f.stem, day, prev, list(hist), universe(day, prev, excl)
        prev, pd_ = day, dd
        hist.append(day)


def load_costs():
    p = WH_ETF / "cost.json"
    if not p.exists():
        raise SystemExit("ETF costs missing: run etf_cost_sample.py first")
    c = json.loads(p.read_text())
    miss = [x for x in ALL_ETF if not (c.get(x) or {}).get("rt_bp")]
    if miss:
        raise SystemExit(f"ETF costs missing for {miss}")
    return c


def etf_context(etf_files, cache, d, hist):
    """ETF day + the ETF sessions on the SAME dates as the stock history (None when any is missing)."""
    if d not in etf_files or any(h["_date"] not in etf_files for h in hist):
        return None, None

    def get(s):
        if s not in cache:
            cache[s] = load_day(etf_files[s])
        return cache[s]
    return get(d), [get(h["_date"]) for h in hist]


def run(mode="run"):
    OUT.mkdir(parents=True, exist_ok=True)
    if mode == "run":
        if (OUT / "run.pkl").exists():
            raise SystemExit("run.pkl exists: the prereg allows ONE read (delete it only with the operator's say)")
        if not (OUT / "count.json").exists():
            raise SystemExit("run the outcome-blind 'count' first")
        cj = json.loads((OUT / "count.json").read_text())
        if not cj.get("canary_plan_ok"):
            raise SystemExit(f"canary sample too thin for the one read ({cj.get('canary_plan')}): widen the (outcome-blind) sampling rule and log it first")
        e_thr = cj["E_threshold"]
        costs = load_costs()
    else:
        e_thr, costs = 0.0020, {x: {"rt_bp": 2.0} for x in ALL_ETF}    # counts do not depend on costs
    etf_files = {p.stem: p for p in WH_ETF.glob("20??-??-??.npz")}
    etf_cache = {}
    res = {k: [] for k in KEYS + ("BASE",)}
    counts = defaultdict(int)
    stats = defaultdict(int)
    acc = {k: {"planted": [], "ctrl_shift": [], "null_day_mean": [], "row_mismatch": 0} for k in KEYS}
    n_days = 0
    plan = {k: {"triggers": 0, "days": 0} for k in KEYS}                     # outcome-blind: triggers/days the canary sample will contain
    for d, day, prev, hist, names in iter_days(WH_SIP):
        ed, eh = etf_context(etf_files, etf_cache, d, hist)
        if ed is None:
            stats["etf_missing_days"] += 1
        o, st = score_day(day, prev, hist, names, d, ed, eh, costs, e_thr)
        for k, v in st.items():
            stats[k] += v
        for k in KEYS:
            counts[k] += len(o[k])
        if mode == "run":
            for k in res:
                res[k] += o[k]
            full = int(hashlib.sha256(f"canary|{d}".encode()).hexdigest(), 16) % CANARY_MOD == 0     # seeded 1 in CANARY_MOD days: all hypotheses
            if full or o["E"]:                                                              # plus E-only canary on every day with ETF triggers
                cd = canary_day(day, prev, hist, names, d, ed, eh, costs, e_thr, o, which=("S", "E", "L", "R") if full else ("E",))
                for k in KEYS:
                    if not full and k != "E":
                        continue
                    for f in ("planted", "ctrl_shift", "null_day_mean"):
                        acc[k][f] += cd[k][f]
                    acc[k]["row_mismatch"] += cd[k]["row_mismatch"]
        full_day = int(hashlib.sha256(f"canary|{d}".encode()).hexdigest(), 16) % CANARY_MOD == 0
        for k in KEYS:
            if full_day or (k == "E" and o["E"]):
                plan[k]["triggers"] += len(o[k])
                plan[k]["days"] += 1 if o[k] else 0
        n_days += 1
        print(d, dict(counts), flush=True)
    conf = {"S": [], "BASE": []}
    conf_days, conf_count = 0, 0
    for d, day, prev, hist, names in iter_days(WH_CONF):
        o, _ = score_day(day, prev, hist, names, d, which=("S",))
        conf_count += len(o["S"])
        if mode == "run":
            conf["S"] += o["S"]
            conf["BASE"] += [b for b in o["BASE"] if b["key"] == "S"]
        conf_days += 1
    if mode == "count":
        cnt = {k: counts[k] for k in KEYS} | {"S_confirm": conf_count, "L_events": stats.get("L_events", 0), "L_mapped_stock_days": stats.get("L_mapped_stocks", 0),
                                              "L_no_residual_days": stats.get("L_no_residual_days", 0), "scored_days": n_days,
                                              "confirm_scored_days": conf_days, "etf_history_incomplete_days": stats.get("etf_history_incomplete_days", 0)}
        cnt["E_threshold"] = 0.0020 if cnt["E"] >= 300 else 0.0010
        cnt["canary_plan"] = plan
        cnt["canary_plan_ok"] = all(v["triggers"] >= 5 and v["days"] >= CANARY_MIN_DAYS for v in plan.values())
        (OUT / "count.json").write_text(json.dumps(cnt, indent=1))
        print(cnt)
        return 0
    iex = {"R21": [], "R10": []}
    for d, day, prev, hist, names in iter_days(WH_IEX):
        o, _ = score_day(day, prev, hist, names, d, which=("R",))
        iex["R21"] += o["R21"]
        iex["R10"] += o["R10"]
    import etf_cost_sample as ecs                          # E stress from actual NBBO spreads (Alpaca historical quotes; mini)
    client = ecs.make_client()
    qcache = {}

    def spread_fn(etf, day_, minute):
        k = (etf, day_, minute)
        if k not in qcache:
            qcache[k] = ecs.spread_at(client, etf, day_, minute)
        return qcache[k]
    attach_e_stress(res["E"], costs, spread_fn)
    pickle.dump({"res": res, "conf": conf, "iex": iex, "stats": dict(stats), "e_thr": e_thr, "canary": acc, "scored_days": n_days, "conf_days": conf_days},
                open(OUT / "run.pkl", "wb"))
    return report()


def report():
    z = pickle.load(open(OUT / "run.pkl", "rb"))
    ok, bad = canary_verdict(z["canary"])
    lines = ["# evening_family (docs/studies/evening_family_2026-11-07_prereg.json)", ""]
    if not ok:
        lines += ["## CANARIES FAILED - nothing below is trusted or read", ""] + [f"- {b}" for b in bad]
        out = "\n".join(lines) + "\n"
        (ROOT / "docs" / "EVENING_FAMILY.md").write_text(out)
        print(out)
        return 2
    res, conf, iex, st = z["res"], z["conf"], z["iex"], z["stats"]
    lines += ["SIP bars; nothing here is shown on IEX except the R information cell. 4 scored tests (S on the confirm sample, E, L, R-H2H), alpha 0.0125. "
              "Canaries passed (planted +8 bp recovered, controls unchanged, null lift within 2 SE) for S, E, L, R21, R10.",
              f"Scored days: discovery {z['scored_days']}, confirm {z['conf_days']}. Information cells (H = 10/30, thresholds, SPY-hedged, per-ETF, L = 14) were NOT implemented; "
              "S bars 15-360 form one refractory list (R5 ran 15-54 and 60-360 separately); R minute grid fills gaps up to 15 minutes only (signals.MINUTE_GAP_FILL_MAX).", ""]

    def block(name, rows, base, min_days=60, min_half=25):
        ev = evaluate(rows, min_days, min_half) if rows else {"status": "UNDERPOWERED", "n": 0}
        void, bm = void_status(ev, base) if rows else (False, float("nan"))
        status = "VOID" if void else ev["status"]
        return ev, status, bm

    def put(name, ev, status, bm):
        lines.append(f"## {name}: {status}")
        if ev.get("n"):
            lines += [f"- n {ev['n']} (halves {ev['n_h']}), scored days {ev['days']} (halves {ev['d_h']}); primary control mean {ev['ca_mean']:+.2f}, secondary {ev['cb_mean']:+.2f}, "
                      f"bar-of-day baseline {bm:+.2f} bp",
                      f"- lift vs primary {ev['lift']:+.2f} bp, day-t {ev['lift_t']:.2f} vs crit {ev['crit']:.2f}, halves {ev['lift_h'][0]:+.2f}/{ev['lift_h'][1]:+.2f}; winsorized halves "
                      f"{ev['win_h'][0]:+.2f}/{ev['win_h'][1]:+.2f}; lift vs secondary {ev['liftb']:+.2f}",
                      f"- net {ev['net']:+.2f} bp (halves {ev['net_h'][0]:+.2f}/{ev['net_h'][1]:+.2f}); at stress {ev['net_stress']:+.2f}"]
        lines.append("")

    def sk(key):
        return f"skipped trades {st.get(key + '_skipped', 0)}; control candidates removed: primary {st.get(key + '_removed_primary', 0)}, secondary {st.get(key + '_removed_secondary', 0)}"

    ev, s, bm = block("S", conf["S"], conf["BASE"], 40, 15)
    put("S surge continuation, stocks - CONFIRM sample (verdict)", ev, s, bm)
    ev, s, bm = block("S", res["S"], [b for b in res["BASE"] if b["key"] == "S"])
    put("S - discovery-sample re-score (information only)", ev, s, bm)
    lines += [f"({sk('S')})", ""]
    ev, s, bm = block("E", res["E"], [b for b in res["BASE"] if b["key"] == "E"], 60, 25)
    miss = sum(1 for r in res["E"] if r.get("net_s") is None)
    if s == "PROFITABLE" and res["E"] and miss / len(res["E"]) > 0.10:
        s = "EDGE"                                          # stress cost missing for > 10% of rows: PROFITABLE vetoed
    put("E surge continuation, ETFs", ev, s, bm)
    lines += [f"(E rows without a stress quote: {miss} of {len(res['E'])}; PROFITABLE is vetoed above 10%)"]
    lines += [f"(E threshold used: {z['e_thr'] * 1e4:.0f} bp; {sk('E')}; ETF history incomplete days {st.get('etf_history_incomplete_days', 0)})", ""]
    ev, s, bm = block("L", res["L"], [b for b in res["BASE"] if b["key"] == "L"])
    days = sorted({r["d"] for r in res["L"]})
    mid = days[len(days) // 2] if days else ""
    evs = {(r["d"], r["etf"], r["t"]) for r in res["L"]}
    h1, h2 = sum(1 for e in evs if e[0] < mid), sum(1 for e in evs if e[0] >= mid)
    if s not in ("VOID",) and (len(evs) < 60 or min(h1, h2) < 25):
        s = "UNDERPOWERED"
    put("L sector-ETF lead-lag", ev, s, bm)
    ep, lastk = 0, {}
    for e in sorted(evs, key=lambda z: (z[1], z[2], z[0])):
        k = (e[1], e[0])
        if k not in lastk or e[2] - lastk[k] > 5:
            ep += 1
        lastk[k] = e[2]
    lines += [f"(distinct ETF x minute events {len(evs)}, merged into {ep} episodes (consecutive minutes within 5), halves {h1}/{h2}; need >= 60 and >= 25 per half; "
              f"mapped-stock-days {st.get('L_mapped_stocks', 0)}, days without residuals {st.get('L_no_residual_days', 0)}; {sk('L')})", ""]
    e21, s21, b21 = block("R21", res["R21"], [b for b in res["BASE"] if b["key"] == "R21"])
    put("R21 live pre-square (descriptive)", e21, s21, b21)
    e10, s10, b10 = block("R10", res["R10"], [b for b in res["BASE"] if b["key"] == "R10"])
    put("R10 pre-square with fast length 10 (descriptive)", e10, s10, b10)
    lines += [f"({sk('R21')}) ({sk('R10')})", ""]
    hh = h2h(res["R10"], res["R21"])
    if "VOID" in (s10, s21):
        lines += ["## R-H2H (scored): VOID (an arm's primary control is more than 3 bp from its baseline)", ""]
    elif hh["status"] == "UNDERPOWERED":
        lines += [f"## R-H2H (scored): UNDERPOWERED ({hh['days']} days with both, need 60)", ""]
    else:
        lines += [f"## R-H2H (scored): {hh['status']}", f"- {hh['days']} days with both; mean lift difference {hh['mean']:+.2f} bp, day-t {hh['t']:.2f} vs crit {hh['crit']:.2f}, halves {hh['h'][0]:+.2f}/{hh['h'][1]:+.2f}", ""]
    ih = h2h(iex["R10"], iex["R21"], min_days=20)
    lines.append(f"- IEX information cell, same head-to-head: {ih['status']}" + (f", {ih['days']} days, mean difference {ih['mean']:+.2f} bp, halves {ih['h'][0]:+.2f}/{ih['h'][1]:+.2f}" if ih.get("mean") is not None else f" ({ih['days']} days)"))
    out = "\n".join(lines) + "\n"
    (ROOT / "docs" / "EVENING_FAMILY.md").write_text(out)
    print(out)
    return 0


def main(argv=None):
    a = (argv if argv is not None else sys.argv[1:]) or ["count"]
    if a[0] == "report":
        return report()
    return run(a[0])


if __name__ == "__main__":
    raise SystemExit(main())
