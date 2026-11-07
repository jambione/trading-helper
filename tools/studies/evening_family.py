#!/usr/bin/env python3
"""evening_family (docs/studies/evening_family_2026-11-07_prereg.json, skeptic rounds applied). Read-only, SIP warehouses.

S  surge continuation, stocks (R5 rule unchanged), VERDICT on warehouse_confirm; the 120-day read is a discovery re-score.
E  surge continuation on 12 ETFs (per-ETF costs from ai_reports/warehouse/etf/cost.json; stress = max(2x, actual spreads)).
L  sector-ETF lead-lag (stock residual 2-bar return in [-15, +2] bp after its mapped ETF jumps).
R  live pre-square arm with fast %R length 21 vs 10; scored as a head-to-head of primary-control lifts (R-H2H).

    .venv/bin/python tools/studies/evening_family.py count            # outcome-blind trigger counts (decides E's threshold)
    .venv/bin/python tools/studies/evening_family.py run              # canaries, then the one read -> docs/EVENING_FAMILY.md
"""
from __future__ import annotations

import json
import math
import pickle
import random
import sys
from collections import deque
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day, prev_weekday  # noqa: E402
from board_structure_family import universe, seeded, tstat  # noqa: E402
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
SECTOR = ["SMH", "SOXX", "IBIT", "IGV", "XBI", "URA", "XLE", "XLF", "ARKK"]
ALL_ETF = SECTOR + ["QQQ", "IWM", "SPY"]


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
    """Transition-free list of trigger bars -> keep the first, restart the clock only at a kept trigger."""
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
    are future information at m and are NOT used)."""
    return [m for m in range(max(FIRST, t + H + 2), min(LAST, t + H + 62) + 1) if all(not (x <= m <= x + H + 1) for x in trig if x <= m)]


def primary_pool(candidates, trig, t, me, exclude=()):
    """Same-minute control pool: other instruments that did NOT trigger in [t-15, t] (known at the decision minute). Triggers after t are
    future information and are never used (round-2 look-ahead fix)."""
    return [m for m in candidates if m != me and m not in exclude and not any(t - 15 <= x <= t for x in trig.get(m, []))]


def betas_and_residuals(hist, names):
    """beta vs SPY over the previous sessions (09:35 on) and the residual return matrix rows, keyed by name. hist days must carry SPY."""
    R, S = {n: [] for n in names}, []
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
        return {}, {}
    S = np.concatenate(S)
    beta, res = {}, {}
    for n in names:
        x = np.concatenate(R[n])
        m = ~np.isnan(x) & ~np.isnan(S)
        if m.sum() < 500:
            continue
        b = float(np.dot(x[m], S[m]) / np.dot(S[m], S[m])) if np.dot(S[m], S[m]) > 0 else 1.0
        beta[n] = b
        e = np.full_like(x, np.nan)
        e[m] = x[m] - b * S[m]
        res[n] = e
    return beta, res


def corr(a, b):
    m = ~np.isnan(a) & ~np.isnan(b)
    if m.sum() < 300:
        return np.nan
    x, y = a[m], b[m]
    sx, sy = x.std(), y.std()
    return float(((x - x.mean()) * (y - y.mean())).mean() / (sx * sy)) if sx > 0 and sy > 0 else np.nan


# ---------------- %R pre-square (R) ----------------
def minute_grid(prev_c, prev_h, prev_l, c, h, l):  # noqa: E741
    """Previous session's last 112 RTH bars + today's, missing minutes filled flat (close carried, high = low = close)."""
    def fill(cc, hh, ll):
        cc, hh, ll = np.array(cc, float), np.array(hh, float), np.array(ll, float)
        last = np.nan
        for i in range(len(cc)):
            if np.isnan(cc[i]):
                cc[i] = hh[i] = ll[i] = last
            else:
                last = cc[i]
                if np.isnan(hh[i]):
                    hh[i] = cc[i]
                if np.isnan(ll[i]):
                    ll[i] = cc[i]
        return cc, hh, ll
    pc, ph, pl = fill(prev_c[-112:], prev_h[-112:], prev_l[-112:])
    tc, th, tl = fill(c, h, l)
    if np.isnan(tc[0]) and len(pc) and not np.isnan(pc[-1]):          # carry the prior close into a late-starting session
        tc, th, tl = fill(np.concatenate([[pc[-1]], c]), np.concatenate([[pc[-1]], h]), np.concatenate([[pc[-1]], l]))
        tc, th, tl = tc[1:], th[1:], tl[1:]
    return np.concatenate([pc, tc]), np.concatenate([ph, th]), np.concatenate([pl, tl]), len(pc)


def presquare_bars(cc, hh, ll, off, L):
    """Today's bar indexes where the live pre-square is true on this bar AND the previous one (trigger = the second)."""
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
def evaluate(rows, crit, min_days=60, min_half_days=25):
    """rows: [{d, net, net_s, ca (primary control), cb (secondary)}] -> verdict dict per docs/TEST_STANDARD.md."""
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
    h = lambda dd: (np.mean([v for d, v in dd.items() if d < mid]) if any(d < mid for d in dd) else float("nan"),  # noqa: E731
                    np.mean([v for d, v in dd.items() if d >= mid]) if any(d >= mid for d in dd) else float("nan"))
    lv = list(lift.values())
    t_l = tstat(lv)
    n_h = (sum(1 for r in rows if r["d"] < mid), sum(1 for r in rows if r["d"] >= mid))
    d_h = (sum(1 for d in net if d < mid), sum(1 for d in net if d >= mid))
    under = len(rows) < 300 or min(n_h) < 100 or len(net) < min_days or min(d_h) < min_half_days
    hl, hw, hn, hs = h(lift), h(win), h(net), h(nets) if nets else (float("nan"), float("nan"))
    edge = (t_l >= crit and hl[0] > 0 and hl[1] > 0 and (np.mean(list(liftb.values())) if liftb else -1) > 0 and hw[0] > 0 and hw[1] > 0)
    prof = edge and hn[0] > 0 and hn[1] > 0 and (np.mean(list(nets.values())) if nets else -1) > 0
    status = "UNDERPOWERED" if under else ("PROFITABLE" if prof else ("EDGE" if edge else "FAIL"))
    return {"status": status, "n": len(rows), "n_h": n_h, "days": len(net), "d_h": d_h, "crit": crit,
            "lift": float(np.mean(lv)) if lv else float("nan"), "lift_t": t_l, "lift_h": hl, "liftb": float(np.mean(list(liftb.values()))) if liftb else float("nan"),
            "win_h": hw, "net": float(np.mean(list(net.values()))), "net_h": hn, "net_stress": float(np.mean(list(nets.values()))) if nets else float("nan"),
            "ca_mean": float(np.mean([r["ca"] for r in rows if r.get("ca") is not None])) if any(r.get("ca") is not None for r in rows) else float("nan")}


def canary_planted(rows, bp=8.0):
    a = [r["net"] - r["ca"] for r in rows if r.get("ca") is not None]
    b = [(r["net"] + bp) - r["ca"] for r in rows if r.get("ca") is not None]
    return (np.mean(b) - np.mean(a)) if a else float("nan")


def crit_t(days):
    from scipy import stats
    return float(stats.t.ppf(1 - ALPHA, max(1, days - 1)))


# ---------------- per-day scoring ----------------
def _draw_xs(rng, pool, k=NDRAW):
    return [rng.choice(pool) for _ in range(k)] if pool else []


def score_day(day, prev, hist, names, d, etf_day=None, etf_hist=None, costs=None, e_thr=0.0020, which=("S", "E", "L", "R")):
    out = {k: [] for k in ("S", "E", "L", "R21", "R10", "BASE")}
    stats = {"skipped": 0}
    beta, res_hist = betas_and_residuals(hist, names)
    names = [n for n in names if n in beta and n in day["idx"]]
    spy = day["c"][day["idx"]["SPY"]]
    spy1 = r1(spy)
    # per-name arrays
    A = {}
    for n in names:
        i = day["idx"][n]
        c, o, v = day["c"][i], day["o"][i], day["v"][i]
        res1 = r1(c) - beta[n] * spy1
        A[n] = {"i": i, "o": o, "c": c, "v": v, "res1": res1}
    # ---- S ----
    if "S" in which:
        trig = {}
        for n in names:
            a = A[n]
            ts = [t for t in range(FIRST, LAST + 1) if not np.isnan(a["res1"][t]) and a["res1"][t] >= 0.0020 and vol_ratio(a["v"], t) >= 2.0]
            trig[n] = first_true(ts)
        for n in names:
            for t in trig[n]:
                net = trade(A[n]["o"], A[n]["c"], t, 5, COST)
                if net is None:
                    stats["skipped"] += 1
                    continue
                pool = primary_pool(names, trig, t, n)
                xs = [trade(A[m]["o"], A[m]["c"], t, 5, COST) for m in _draw_xs(seeded(n, d, "S-a", t), pool)]
                xs = [x for x in xs if x is not None]
                lm = later_minutes(t, 5, trig[n])
                ls_ = [trade(A[n]["o"], A[n]["c"], m, 5, COST) for m in _draw_xs(seeded(n, d, "S-b", t), lm)]
                ls_ = [x for x in ls_ if x is not None]
                out["S"].append({"d": d, "sym": n, "t": t, "net": net, "net_s": net + COST - STRESS,
                                 "ca": float(np.mean(xs)) if xs else None, "cb": float(np.mean(ls_)) if ls_ else None})
                _b = trade(A[m0]["o"], A[m0]["c"], t, 5, COST) if (m0 := seeded("baseline", "S", d, n, t).choice(names)) else None
                if _b is not None:
                    out["BASE"].append({"key": "S", "net": _b})
    # ---- R (both lengths) ----
    if "R" in which and prev is not None:
        trig = {10: {}, 21: {}}
        for n in names:
            j = prev["idx"].get(n)
            if j is None:
                continue
            i = A[n]["i"]
            cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], day["c"][i], day["h"][i], day["l"][i])
            if off < 112:
                continue
            for L in (10, 21):
                trig[L][n] = first_true(presquare_bars(cc, hh, ll, off, L))
        for L in (10, 21):
            key = f"R{L}"
            for n, ts in trig[L].items():
                for t in ts:
                    net = trade(A[n]["o"], A[n]["c"], t, 10, COST)
                    if net is None:
                        stats["skipped"] += 1
                        continue
                    pool = primary_pool(list(trig[L]), trig[L], t, n)
                    xs = [trade(A[m]["o"], A[m]["c"], t, 10, COST) for m in _draw_xs(seeded(n, d, f"{key}-a", t), pool)]
                    xs = [x for x in xs if x is not None]
                    lm = later_minutes(t, 10, ts)
                    ls_ = [trade(A[n]["o"], A[n]["c"], m, 10, COST) for m in _draw_xs(seeded(n, d, f"{key}-b", t), lm)]
                    ls_ = [x for x in ls_ if x is not None]
                    out[key].append({"d": d, "sym": n, "t": t, "net": net, "net_s": net + COST - STRESS,
                                     "ca": float(np.mean(xs)) if xs else None, "cb": float(np.mean(ls_)) if ls_ else None})
                    _b = trade(A[m0]["o"], A[m0]["c"], t, 10, COST) if (m0 := seeded("baseline", key, d, n, t).choice(names)) else None
                    if _b is not None:
                        out["BASE"].append({"key": key, "net": _b})
    # ---- E and L need ETF bars ----
    if etf_day is not None and etf_hist:
        ebeta, eres = betas_and_residuals(etf_hist, [x for x in ALL_ETF if x != "SPY"])
        ebeta["SPY"] = 1.0
        espy = etf_day["c"][etf_day["idx"]["SPY"]]
        E = {}
        for x in ALL_ETF:
            k = etf_day["idx"].get(x)
            if k is None or x not in ebeta:
                continue
            c = etf_day["c"][k]
            rr = r1(c) if x == "SPY" else r1(c) - ebeta[x] * r1(espy)
            E[x] = {"o": etf_day["o"][k], "c": c, "v": etf_day["v"][k], "res1": rr}
        if "E" in which:
            trig = {}
            for x, a in E.items():
                ts = [t for t in range(FIRST, LAST + 1) if not np.isnan(a["res1"][t]) and a["res1"][t] >= e_thr and vol_ratio(a["v"], t) >= 2.0]
                trig[x] = first_true(ts)
            if "SMH" in trig and "SOXX" in trig:                       # near-duplicates: keep SMH when both fire the same minute
                trig["SOXX"] = [t for t in trig["SOXX"] if t not in set(trig["SMH"])]
            for x, ts in trig.items():
                cx = (costs or {}).get(x, {}).get("rt_bp") or 2.0
                for t in ts:
                    net = trade(E[x]["o"], E[x]["c"], t, 5, cx)
                    if net is None:
                        stats["skipped"] += 1
                        continue
                    dup = {'SMH': 'SOXX', 'SOXX': 'SMH'}.get(x)
                    pool = primary_pool(list(E), trig, t, x, exclude=(dup,))
                    xs = [trade(E[y]["o"], E[y]["c"], t, 5, (costs or {}).get(y, {}).get("rt_bp") or 2.0) for y in _draw_xs(seeded(x, d, "E-a", t), pool)]
                    xs = [z for z in xs if z is not None]
                    lm = later_minutes(t, 5, ts)
                    ls_ = [trade(E[x]["o"], E[x]["c"], m, 5, cx) for m in _draw_xs(seeded(x, d, "E-b", t), lm)]
                    ls_ = [z for z in ls_ if z is not None]
                    out["E"].append({"d": d, "sym": x, "t": t, "net": net, "net_s": None, "cost": cx,
                                     "ca": float(np.mean(xs)) if xs else None, "cb": float(np.mean(ls_)) if ls_ else None})
                    y0 = seeded("baseline", "E", d, x, t).choice(sorted(E))
                    _b = trade(E[y0]["o"], E[y0]["c"], t, 5, (costs or {}).get(y0, {}).get("rt_bp") or 2.0)
                    if _b is not None:
                        out["BASE"].append({"key": "E", "net": _b})
        if "L" in which:
            # mapping from the previous 20 sessions only: stock residuals (stock hist) vs ETF residuals (ETF hist), aligned by day
            mapped, etf_corr = {}, {}
            for x in SECTOR:
                for y in SECTOR:
                    if x < y and x in eres and y in eres:
                        etf_corr[(x, y)] = etf_corr[(y, x)] = corr(eres[x], eres[y])
            for n in names:
                if n not in res_hist:
                    continue
                best, bc = None, 0.30
                for x in SECTOR:
                    if x in eres and len(eres[x]) == len(res_hist[n]):
                        cr = corr(res_hist[n], eres[x])
                        if not np.isnan(cr) and cr >= bc:
                            best, bc = x, cr
                if best:
                    mapped[n] = best
            r2 = {n: (A[n]["c"] / np.roll(A[n]["c"], 2) - 1) - beta[n] * (spy / np.roll(spy, 2) - 1) for n in names}
            ok_f = {n: (lambda arr: (arr >= -0.0015) & (arr <= 0.0002))(r2[n]) for n in names}
            ev = []
            for x in SECTOR:
                if x not in E:
                    continue
                a = E[x]
                er2 = (a["c"] / np.roll(a["c"], 2) - 1) - ebeta[x] * (espy / np.roll(espy, 2) - 1)
                for t in range(FIRST, LAST + 1):
                    if not np.isnan(er2[t]) and er2[t] >= 0.0015 and vol_ratio(a["v"], t) >= 2.0:
                        ev.append((x, t))
            last = {}
            for x, t in sorted(ev, key=lambda z: z[1]):
                hit = False
                for n, mx in mapped.items():
                    if mx != x or not ok_f[n][t] or t - last.get(n, -10 ** 9) < REFRACT:
                        continue
                    net = trade(A[n]["o"], A[n]["c"], t, 10, COST)
                    if net is None:
                        stats["skipped"] += 1
                        continue
                    last[n] = t
                    hit = True
                    excl = {x} | {y for y in SECTOR if etf_corr.get((x, y), 0) >= 0.5}
                    pool = [m for m in names if m != n and ok_f[m][t] and mapped.get(m) not in excl]
                    xs = [trade(A[m]["o"], A[m]["c"], t, 10, COST) for m in _draw_xs(seeded(n, d, "L-a", t), pool)]
                    xs = [z for z in xs if z is not None]
                    lm = [m for m in later_minutes(t, 10, [t]) if ok_f[n][m]]
                    ls_ = [trade(A[n]["o"], A[n]["c"], m, 10, COST) for m in _draw_xs(seeded(n, d, "L-b", t), lm)]
                    ls_ = [z for z in ls_ if z is not None]
                    out["L"].append({"d": d, "sym": n, "t": t, "etf": x, "net": net, "net_s": net + COST - STRESS,
                                     "ca": float(np.mean(xs)) if xs else None, "cb": float(np.mean(ls_)) if ls_ else None})
                    bp_ = [m for m in names if ok_f[m][t]]
                    if bp_:
                        m0 = seeded("baseline", "L", d, n, t).choice(bp_)
                        _b = trade(A[m0]["o"], A[m0]["c"], t, 10, COST)
                        if _b is not None:
                            out["BASE"].append({"key": "L", "net": _b})
                stats.setdefault("L_events", 0)
                stats["L_events"] += int(hit)
    return out, stats


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


def run(mode="run"):
    OUT.mkdir(parents=True, exist_ok=True)
    costs = json.loads((WH_ETF / "cost.json").read_text()) if (WH_ETF / "cost.json").exists() else {}
    etf_files = {p.stem: p for p in WH_ETF.glob("20??-??-??.npz")}
    etf_hist = deque(maxlen=20)
    res = {k: [] for k in ("S", "E", "L", "R21", "R10", "BASE")}
    st_all = {"skipped": 0, "L_events": 0}
    e_thr = 0.0020
    if mode == "run" and (OUT / "count.json").exists():
        e_thr = json.loads((OUT / "count.json").read_text()).get("E_threshold", 0.0020)
    for d, day, prev, hist, names in iter_days(WH_SIP):
        ed = load_day(etf_files[d]) if d in etf_files else None
        o, st = score_day(day, prev, hist, names, d, ed, list(etf_hist), costs, e_thr)
        if ed is not None:
            etf_hist.append(ed)
        for k in res:
            res[k] += o[k]
        for k, v in st.items():
            st_all[k] = st_all.get(k, 0) + v
        print(d, {k: len(v) for k, v in res.items()}, flush=True)
    conf = {"S": [], "BASE": []}
    for d, day, prev, hist, names in iter_days(WH_CONF):
        o, _ = score_day(day, prev, hist, names, d, which=("S",))
        conf["S"] += o["S"]
        conf["BASE"] += [b for b in o["BASE"] if b["key"] == "S"]
        print("confirm", d, len(conf["S"]), flush=True)
    iex = {"R21": [], "R10": []}
    for d, day, prev, hist, names in iter_days(WH_IEX):
        o, _ = score_day(day, prev, hist, names, d, which=("R",))
        iex["R21"] += o["R21"]
        iex["R10"] += o["R10"]
    pickle.dump({"res": res, "conf": conf, "iex": iex, "stats": st_all, "e_thr": e_thr}, open(OUT / f"{mode}.pkl", "wb"))
    if mode == "count":
        cnt = {k: len(v) for k, v in res.items()} | {"S_confirm": len(conf["S"]), "L_events": st_all.get("L_events", 0)}
        cnt["E_threshold"] = 0.0020 if cnt["E"] >= 300 else 0.0010
        (OUT / "count.json").write_text(json.dumps(cnt, indent=1))
        print(cnt)
        return 0
    return report()


def report():
    z = pickle.load(open(OUT / "run.pkl", "rb"))
    res, conf, iex = z["res"], z["conf"], z["iex"]
    lines = ["# evening_family (docs/studies/evening_family_2026-11-07_prereg.json)", "",
             "SIP bars; nothing here is shown on IEX except the R information cell. Family of 4 scored tests (S on the confirm sample, E, L, R-H2H), alpha 0.0125.", ""]

    def block(name, rows, base, min_days=60, min_half=25):
        days = len({r["d"] for r in rows})
        ev = evaluate(rows, crit_t(days), min_days, min_half) if rows else {"status": "UNDERPOWERED", "n": 0}
        bm = float(np.mean([b["net"] for b in base])) if base else float("nan")
        void = rows and not math.isnan(ev.get("ca_mean", float("nan"))) and abs(ev["ca_mean"] - bm) > 3.0
        status = "VOID" if void else ev["status"]
        lines.append(f"## {name}: {status}")
        if ev.get("n"):
            lines += [f"- n {ev['n']} (halves {ev['n_h']}), days {ev['days']} (halves {ev['d_h']}); primary control mean {ev['ca_mean']:+.2f} vs bar-of-day baseline {bm:+.2f} bp",
                      f"- lift vs primary {ev['lift']:+.2f} bp, day-t {ev['lift_t']:.2f} vs crit {ev['crit']:.2f}, halves {ev['lift_h'][0]:+.2f}/{ev['lift_h'][1]:+.2f}; winsorized halves {ev['win_h'][0]:+.2f}/{ev['win_h'][1]:+.2f}; vs secondary {ev['liftb']:+.2f}",
                      f"- net {ev['net']:+.2f} bp (halves {ev['net_h'][0]:+.2f}/{ev['net_h'][1]:+.2f}); at stress {ev['net_stress']:+.2f}",
                      f"- canary: planted +8 bp moves the lift by {canary_planted(rows):+.2f}"]
        lines.append("")
        return ev

    bS = [b for b in conf["BASE"]]
    block("S surge continuation, stocks - CONFIRM sample (verdict)", conf["S"], bS, 40, 15)
    block("S - discovery-sample re-score (information only)", res["S"], [b for b in res["BASE"] if b["key"] == "S"])
    block("E surge continuation, ETFs", res["E"], [b for b in res["BASE"] if b["key"] == "E"], 60, 25)
    lines.append(f"(E threshold used: {z['e_thr'] * 1e4:.0f} bp)")
    lines.append("")
    block("L sector-ETF lead-lag", res["L"], [b for b in res["BASE"] if b["key"] == "L"])
    lines.append(f"(L distinct ETF events with a trade: {z['stats'].get('L_events', 0)}; needs >= 60 and >= 25 per half)")
    lines.append("")
    e21 = block("R21 live pre-square (descriptive)", res["R21"], [b for b in res["BASE"] if b["key"] == "R21"])
    e10 = block("R10 pre-square with fast length 10 (descriptive)", res["R10"], [b for b in res["BASE"] if b["key"] == "R10"])
    # R-H2H: per day, mean primary lift of L=10 minus that of L=21, days with both
    def dl(rows):
        out = {}
        for r in rows:
            if r.get("ca") is not None:
                out.setdefault(r["d"], []).append(r["net"] - r["ca"])
        return {d: float(np.mean(v)) for d, v in out.items()}
    a10, a21 = dl(res["R10"]), dl(res["R21"])
    both = sorted(set(a10) & set(a21))
    diff = [a10[d] - a21[d] for d in both]
    if len(both) >= 60:
        mid = both[len(both) // 2]
        h1 = np.mean([a10[d] - a21[d] for d in both if d < mid]); h2 = np.mean([a10[d] - a21[d] for d in both if d >= mid])
        t = tstat(diff); crit = crit_t(len(both))
        st = "L=10 BETTER" if (t >= crit and h1 > 0 and h2 > 0) else "NOT BETTER"
        lines += [f"## R-H2H (scored): {st}", f"- {len(both)} days with both; mean lift difference {np.mean(diff):+.2f} bp, day-t {t:.2f} vs crit {crit:.2f}, halves {h1:+.2f}/{h2:+.2f}", ""]
    else:
        lines += [f"## R-H2H (scored): UNDERPOWERED ({len(both)} days with both, need 60)", ""]
    for L in ("R21", "R10"):
        r = iex[L]
        lines.append(f"- IEX information cell {L}: n {len(r)}, mean lift {np.mean([x['net'] - x['ca'] for x in r if x.get('ca') is not None]) if r else float('nan'):+.2f} bp")
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
