#!/usr/bin/env python3
"""daily_wr_swing - raw daily Williams %R(3/5/10) <= -90 above the 200-day average, bought at the next open and sold
on the snap-back (%R > -20 or close > SMA5, at most 5 sessions), against other uptrend names held over the same days.
Operator 11/20 ('Yes, pre-register A'). Prereg + amended_1: docs/studies/daily_wr_swing_prereg.json.

    .venv/bin/python tools/studies/daily_wr_swing.py run        # on the mini (~/lh_cache/panel.npz); no API, any hour

Verdict window 2018-07-02..2022-12-30 (halves split 2020-10-01); 2017-01..2018-06 and 2023+ are information.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "ai_reports" / "daily_wr_swing"
PANEL = Path.home() / "lh_cache" / "panel.npz"

VERDICT = ("2018-07-02", "2022-12-30")
SPLIT = "2020-10-01"
INFO_WINDOWS = {"pre_2018_07": ("2017-01-03", "2018-06-29"), "post_2023": ("2023-01-03", "2026-09-25")}
MAX_HOLD = 5
CLIP = (-0.90, 3.00)
COST_MONEY, COST_INFO = 30.0, 10.0
T_CRIT, MDE_MAX, MIN_N = 2.39, 30.0, 1000
ETF_SCAN = {"SPY", "QQQ", "IWM", "DIA", "TQQQ", "SQQQ", "SOXL", "UVXY", "TLT", "GLD", "XLF", "SPXL", "TNA", "ARKK",
            "SMH", "LABU", "NUGT", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC", "EEM", "EFA",
            "HYG", "LQD", "SLV", "USO", "VXX", "SPXU", "SDS", "SSO", "UPRO", "TZA", "FAS", "FAZ", "KRE", "XBI", "IBB"}


# ---------------------------------------------------------------- indicators (pure, T x N arrays)

def williams_r(H, L, C, n):
    """-100 x (HH_n - C) / (HH_n - LL_n); every bar in the window finite (amended_1 m2); zero range -> NaN. Pure."""
    hh = pd.DataFrame(H).rolling(n, min_periods=n).max().to_numpy()
    ll = pd.DataFrame(L).rolling(n, min_periods=n).min().to_numpy()
    rng = hh - ll
    with np.errstate(invalid="ignore", divide="ignore"):
        wr = -100.0 * (hh - C) / rng
    wr[~(rng > 0)] = np.nan
    wr[~np.isfinite(C)] = np.nan
    return wr


def sma(C, n):
    return pd.DataFrame(C).rolling(n, min_periods=n).mean().to_numpy()


def rsi2(C, min_after_seed=20):
    """Wilder RSI-2 (alpha 1/2) per column on contiguous finite closes: the first change seeds, a NaN close restarts the
    seed, a reading needs >= min_after_seed bars after the seed; avg loss 0 -> 100; gain = loss = 0 -> NaN. Pure."""
    T, N = C.shape
    out = np.full((T, N), np.nan)
    up, dn = np.zeros(N), np.zeros(N)
    cnt = np.zeros(N, int)
    prev = np.full(N, np.nan)
    for t in range(T):
        c = C[t]
        ok = np.isfinite(c) & np.isfinite(prev)
        d = np.where(ok, c - prev, 0.0)
        g, lo = np.maximum(d, 0.0), np.maximum(-d, 0.0)
        first = ok & (cnt == 0)
        up = np.where(first, g, np.where(ok, 0.5 * g + 0.5 * up, up))
        dn = np.where(first, lo, np.where(ok, 0.5 * lo + 0.5 * dn, dn))
        cnt = np.where(ok, cnt + 1, np.where(np.isfinite(c), 0, 0))
        with np.errstate(invalid="ignore", divide="ignore"):
            r = np.where(dn == 0, 100.0, 100.0 - 100.0 / (1.0 + up / np.where(dn == 0, 1, dn)))
        r = np.where((up == 0) & (dn == 0), np.nan, r)
        out[t] = np.where(ok & (cnt >= min_after_seed), r, np.nan)
        prev = np.where(np.isfinite(c), c, np.nan)
    return out


def ffill(a):
    return pd.DataFrame(a).ffill().to_numpy()


# ---------------------------------------------------------------- trades (pure)

def hkey(*p):
    return int(hashlib.sha256("|".join(str(x) for x in p).encode()).hexdigest(), 16)


def clip(r):
    return float(min(max(r, CLIP[0]), CLIP[1]))


def exit_bar(t, exitm, C):
    """First close s in t+1..t+5 with the exit condition -> ('open', s+1); none -> ('open', t+6). A NaN close at s
    during the hold exits at the last close before it -> ('close', s-1); a NaN close at t+1 (no close after entry)
    -> None (skipped, counted). Pure."""
    for s in range(t + 1, t + MAX_HOLD + 1):
        if s >= len(C):
            return ("close", len(C) - 1) if len(C) - 1 > t else None
        if not np.isfinite(C[s]):
            return ("close", s - 1) if s - 1 > t else None
        if exitm[s]:
            return "open", s + 1
    return "open", t + MAX_HOLD + 1


def leg_returns(O, C, Cf, cols, ent, kind, x, entry_close=False):
    """Vectorised over names `cols`: bought at O[ent] (or C[ent-1] when entry_close), sold at O[x] ('open') or C[x]
    ('close'). The same NaN rule as the trade (amended_1 m2, review #4): a NaN close in the hold sells at the last close
    before it; a missing exit open (delisting) sells at the last close. -> (returns with NaN where invalid, fallback
    mask). Pure."""
    cols = np.asarray(cols, int)
    T = len(O)
    e = C[ent - 1, cols] if entry_close else O[ent, cols]
    last = min(x - 1 if kind == "open" else x, T - 1)
    xp = np.full(len(cols), np.nan)
    fb = np.zeros(len(cols), bool)
    done = np.zeros(len(cols), bool)
    for b in range(ent, last + 1):
        cb = C[b, cols]
        bad = ~done & ~np.isfinite(cb)
        if bad.any():
            prev = C[b - 1, cols] if b - 1 >= ent else np.full(len(cols), np.nan)
            xp[bad] = prev[bad]
            fb[bad] = True
            done |= bad
    rest = ~done
    if kind == "open":
        ox = O[x, cols] if x < T else np.full(len(cols), np.nan)
        okx = rest & np.isfinite(ox)
        xp[okx] = ox[okx]
        miss = rest & ~np.isfinite(ox)
        xp[miss] = Cf[last, cols][miss]
        fb |= miss
    else:
        xp[rest] = C[last, cols][rest]
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(np.isfinite(e) & (e > 0) & np.isfinite(xp), xp / e - 1, np.nan)
    return np.clip(r, CLIP[0], CLIP[1]), fb


def leg_return(O, C, Cf, j, ent, kind, x, entry_close=False):
    r, fb = leg_returns(O, C, Cf, [j], ent, kind, x, entry_close)
    return (None, False) if not np.isfinite(r[0]) else (float(r[0]), bool(fb[0]))


def simulate(D, sig, exitm, ctrl, lo, hi, entry_close=False, tag="v"):
    """Trades of one variant with entry dates in [lo, hi]. sig/exitm/ctrl: T x N bool. One position per name.
    hold = x - (t+1) for both exit kinds. -> (trades, counts). Pure."""
    O, C, Cf, days = D["O"], D["C"], D["Cf"], D["days"]
    T, N = C.shape
    ti = [i for i, d in enumerate(days) if lo <= d <= hi and i + MAX_HOLD + 2 < T]
    trades, counts = [], defaultdict(int)
    busy = np.full(N, -1)
    cache = {}
    for t in ti:
        cols = np.where(sig[t])[0]
        if not len(cols):
            continue
        if D["tradeable"][t].sum() < 100:
            raise AssertionError(f"fewer than 100 tradeable names on {days[t]}")
        for j in cols:
            counts["signals"] += 1
            if busy[j] >= t:
                counts["skip_open_position"] += 1
                continue
            if not entry_close and not np.isfinite(O[t + 1, j]):
                counts["skip_nan_open"] += 1
                continue
            eb = exit_bar(t, exitm[:, j], C[:, j])
            if eb is None:
                counts["skip_no_close_after_entry"] += 1
                continue
            kind, x = eb
            r, fb = leg_return(O, C, Cf, j, t + 1, kind, x, entry_close)
            if r is None:
                counts["no_return"] += 1
                continue
            if kind == "close":
                counts["close_exit_nan_gap_or_end"] += 1
            if fb:
                counts["exit_fell_back_to_last_close"] += 1
            if r in CLIP:
                counts["clipped"] += 1
            if np.isfinite(D["sma5"][t, j]) and C[t, j] >= D["sma5"][t, j]:
                counts["signal_at_or_above_sma5"] += 1
            busy[j] = x
            key = (t, kind, x)
            if key not in cache:
                cj = np.where(ctrl[t] & ~sig[t])[0]
                rr, _ = leg_returns(O, C, Cf, cj, t + 1, kind, x, entry_close)
                ok = np.isfinite(rr)
                cache[key] = (cj[ok], rr[ok])
            cj, rr = cache[key]
            if not len(rr):
                counts["no_control"] += 1
                continue
            c_all = float(rr.mean())
            q = D["volq"][t]
            sq = rr[q[cj] == q[j]] if np.isfinite(q[j]) else np.array([])
            c_vol = float(sq.mean()) if len(sq) else np.nan
            spy_e = D["spy_c"][t] if entry_close else D["spy_o"][t + 1]
            spy_x = D["spy_o"][x] if (kind == "open" and x < T) else D["spy_c"][min(x - (kind == "open"), T - 1)]
            trades.append({"day": days[t], "t": int(t), "j": int(j), "kind": kind, "x": int(x), "hold": int(x - t - 1),
                           "r": r, "c": c_all, "c_vol": c_vol, "spy": float(spy_x / spy_e - 1),
                           "lift": (1 + r) / (1 + c_all) - 1,
                           "lift_vol": (1 + r) / (1 + c_vol) - 1 if np.isfinite(c_vol) else np.nan})
    return trades, dict(counts)


def same_name_controls(D, trades, ctrl_later, tag):
    """amended_1 M4: same name, an entry day u in t+hold+2 .. t+hold+62 where ctrl_later holds, chosen uniformly among
    the qualifying days by hash (review #1), held the same way (entry O[u+1], same exit kind, same hold).
    -> list of (trade index, control return). Pure."""
    O, C, Cf = D["O"], D["C"], D["Cf"]
    T = len(O)
    out = []
    for i, tr in enumerate(trades):
        t, j, h, kind = tr["t"], tr["j"], tr["hold"], tr["kind"]
        lo = t + h + 2
        cand = [u for u in range(lo, lo + 61) if u + 2 + h < T and ctrl_later[u, j] and np.isfinite(O[u + 1, j])]
        if not cand:
            continue
        u = cand[hkey("dwr-later", tag, tr["day"], j) % len(cand)]
        rr, _ = leg_return(O, C, Cf, j, u + 1, kind, u + 1 + h)
        if rr is not None:
            out.append((i, rr))
    return out


def unconditional(D, trades, ctrl_up, tag):
    """A random uptrend tradeable name-day near each trade (+/-20 sessions), held the same way. -> list of
    (trade index, return). Pure."""
    O, C, Cf = D["O"], D["C"], D["Cf"]
    T, N = C.shape
    out = []
    for i, tr in enumerate(trades):
        h, kind = tr["hold"], tr["kind"]
        for k in range(50):
            z = hkey("dwr-unc", tag, i, k)
            t = tr["t"] + (z % 41) - 20
            j = (z >> 16) % N
            if 0 <= t < T - h - 3 and ctrl_up[t, j] and np.isfinite(O[t + 1, j]):
                rr, _ = leg_return(O, C, Cf, j, t + 1, kind, t + 1 + h)
                if rr is not None:
                    out.append((i, rr))
                    break
    return out


# ---------------------------------------------------------------- statistics (pure)

def month_t(vals, months):
    """Mean, cluster-robust SE by month, t. -> (mean, se, t). Pure."""
    x = np.asarray(vals, float)
    ok = np.isfinite(x)
    x, m = x[ok], np.asarray(months)[ok]
    n = len(x)
    if n < 10:
        return None, None, None
    mu = x.mean()
    g = defaultdict(float)
    for v, k in zip(x - mu, m):
        g[k] += v
    G = len(g)
    if G < 3:
        return float(mu), None, None
    se = math.sqrt(sum(s * s for s in g.values()) * G / (G - 1)) / n
    return float(mu), se, (float(mu / se) if se > 0 else None)


def resid(y, x):
    y, x = np.asarray(y, float), np.asarray(x, float)
    ok = np.isfinite(y) & np.isfinite(x)
    if ok.sum() < 10 or np.var(x[ok]) == 0:
        return np.full(len(y), np.nan), None
    b = float(np.cov(y[ok], x[ok])[0, 1] / np.var(x[ok], ddof=1))
    return y - b * x, b


def bp(x):
    return None if x is None or not np.isfinite(x) else round(float(x) * 1e4, 2)


def winsor(x, top=0.01):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(np.minimum(x, np.quantile(x, 1 - top)).mean()) if len(x) else None


def half_stats(tr):
    if not tr:
        return {"n": 0}
    months = [r["day"][:7] for r in tr]
    lift = [r["lift"] for r in tr]
    mu, se, t = month_t(lift, months)
    res_c, b_c = resid([r["r"] for r in tr], [r["c"] for r in tr])
    res_s, b_s = resid([r["r"] for r in tr], [r["spy"] for r in tr])
    _, _, t_rc = month_t(res_c, months)
    _, _, t_rs = month_t(res_s, months)
    per_day = defaultdict(list)
    for r in tr:
        per_day[r["day"]].append(r["lift"])
    dayw = float(np.mean([np.mean(v) for v in per_day.values()]))
    lv = [r["lift_vol"] for r in tr if np.isfinite(r["lift_vol"])]
    q = defaultdict(list)
    for r in tr:
        q[r["day"][:4] + "Q" + str((int(r["day"][5:7]) - 1) // 3 + 1)].append(r["lift"])
    best_q = max(q, key=lambda k: np.mean(q[k])) if q else None
    no_best_q = [r["lift"] for r in tr if best_q is None or (r["day"][:4] + "Q" + str((int(r["day"][5:7]) - 1) // 3 + 1)) != best_q]
    srt = np.sort(np.asarray(lift))
    no_top = srt[: max(1, int(len(srt) * 0.99))]
    raw = np.array([r["r"] for r in tr])
    return {"n": len(tr), "days": len(per_day), "lift_bp": bp(mu), "se_bp": bp(se), "t": None if t is None else round(t, 2),
            "mde_bp": bp(2.8 * se) if se else None,
            "beta_c": None if b_c is None else round(b_c, 3), "t_resid_c": None if t_rc is None else round(t_rc, 2),
            "beta_spy": None if b_s is None else round(b_s, 3), "t_resid_spy": None if t_rs is None else round(t_rs, 2),
            "lift_volmatched_bp": bp(float(np.mean(lv))) if lv else None, "lift_dayweighted_bp": bp(dayw),
            "winsor_lift_bp": bp(winsor(lift)), "lift_drop_top1pct_bp": bp(float(no_top.mean())),
            "lift_drop_best_quarter_bp": bp(float(np.mean(no_best_q))) if no_best_q else None, "best_quarter": best_q,
            "raw_r_bp": bp(float(raw.mean())), "raw_net30_bp": bp(float(raw.mean()) - COST_MONEY / 1e4),
            "raw_net10_bp": bp(float(raw.mean()) - COST_INFO / 1e4), "lift_minus10_bp": bp(mu - COST_INFO / 1e4) if mu is not None else None,
            "win_pct": round(100 * float((raw > 0).mean()), 1),
            "median_hold": float(np.median([r["hold"] for r in tr])),
            "control_mean_bp": bp(float(np.mean([r["c"] for r in tr]))),
            "top1pct_share": round(float(np.sort(raw)[::-1][: max(1, len(raw) // 100)].sum() / raw.sum()), 3) if raw.sum() else None}


def verdict(trades):
    """amended_1 pass bars on the verdict window. -> (verdict, per-half stats, checks). Pure."""
    a = [r for r in trades if r["day"] < SPLIT]
    b = [r for r in trades if r["day"] >= SPLIT]
    A, B = half_stats(a), half_stats(b)
    P = half_stats(trades)
    if a and b:     # amended prereg: beta per half; residuals pooled for the t (review #6)
        months = [r["day"][:7] for r in a + b]
        for key, xk in (("t_resid_c", "c"), ("t_resid_spy", "spy")):
            ra, _ = resid([r["r"] for r in a], [r[xk] for r in a])
            rb, _ = resid([r["r"] for r in b], [r[xk] for r in b])
            _, _, tt = month_t(np.concatenate([ra, rb]), months)
            P[key] = None if tt is None else round(tt, 2)
    if A["n"] == 0 or B["n"] == 0:
        return "UNPROVEN (no trades in a half)", A, B, P, {}
    if (A.get("mde_bp") or 999) > MDE_MAX or (B.get("mde_bp") or 999) > MDE_MAX:
        return "UNDERPOWERED -> UNPROVEN", A, B, P, {}
    g = lambda s, k: s.get(k) if s.get(k) is not None else -999
    checks = {
        "lift > 0 both halves": g(A, "lift_bp") > 0 and g(B, "lift_bp") > 0,
        "pooled t >= 2.39": g(P, "t") >= T_CRIT,
        "beta-residual t >= 2.39": g(P, "t_resid_c") >= T_CRIT,
        "SPY-residual t >= 2.39": g(P, "t_resid_spy") >= T_CRIT,
        "vol-matched lift > 0 both halves": g(A, "lift_volmatched_bp") > 0 and g(B, "lift_volmatched_bp") > 0,
        "day-weighted lift > 0 both halves": g(A, "lift_dayweighted_bp") > 0 and g(B, "lift_dayweighted_bp") > 0,
        "money: lift - 30 bp > 0 both halves": g(A, "lift_bp") > COST_MONEY and g(B, "lift_bp") > COST_MONEY,
        "winsorized lift - 30 > 0 both halves": g(A, "winsor_lift_bp") > COST_MONEY and g(B, "winsor_lift_bp") > COST_MONEY,
        "drop top 1% - 30 > 0 both halves": g(A, "lift_drop_top1pct_bp") > COST_MONEY and g(B, "lift_drop_top1pct_bp") > COST_MONEY,
        "drop best quarter - 30 > 0 both halves": g(A, "lift_drop_best_quarter_bp") > COST_MONEY and g(B, "lift_drop_best_quarter_bp") > COST_MONEY,
        ">= 1000 trades per half": A["n"] >= MIN_N and B["n"] >= MIN_N}
    edge_keys = list(checks)[:6]
    v = "PASS" if all(checks.values()) else ("EDGE only (fails money/robust)" if all(checks[k] for k in edge_keys) else "FAIL")
    return v, A, B, P, checks


def account(trades, days, years, seeds=200, slots=10, cost=COST_MONEY):
    """Information: 10 equal slots, trades taken in entry order (random tie order per seed) when a slot is free; the
    book is marked at each exit (slot mean). -> median and range of yearly return, worst drawdown and monthly Sharpe
    after cost. Pure."""
    res, dds, shs = [], [], []
    for s in range(seeds):
        rng = np.random.default_rng(s)
        order = sorted(range(len(trades)), key=lambda i: (trades[i]["t"], rng.random()))
        free = [-1] * slots
        eq = [1.0] * slots
        marks = []
        for i in order:
            tr = trades[i]
            k = next((k for k in range(slots) if free[k] <= tr["t"] + 1), None)
            if k is None:
                continue
            eq[k] *= 1 + tr["r"] - cost / 1e4
            free[k] = tr["x"]
            marks.append((min(tr["x"], len(days) - 1), float(np.mean(eq))))
        marks.sort()
        res.append(float(np.mean(eq)) ** (1 / years) - 1)
        if marks:
            ser = pd.Series([m for _, m in marks], index=pd.to_datetime([days[x] for x, _ in marks]))
            ser = ser.groupby(level=0).last()
            peak = ser.cummax()
            dds.append(float((ser / peak - 1).min()))
            mo = ser.resample("ME").last().ffill().pct_change().dropna()
            shs.append(float(mo.mean() / mo.std() * math.sqrt(12)) if len(mo) > 2 and mo.std() > 0 else np.nan)
    return {"seeds": seeds, "median_yearly_pct": round(100 * float(np.median(res)), 2),
            "range_pct": [round(100 * min(res), 2), round(100 * max(res), 2)],
            "median_max_drawdown_pct": round(100 * float(np.median(dds)), 1) if dds else None,
            "median_sharpe": round(float(np.nanmedian(shs)), 2) if shs else None}


# ---------------------------------------------------------------- data + run (I/O)

def load(path=PANEL):
    z = np.load(path, allow_pickle=True)
    days = [str(d) for d in z["dates"]]
    syms = [str(s) for s in z["syms"]]
    D = {"days": days, "syms": syms, "O": z["o"].astype(float), "C": z["c"].astype(float),
         "spy_o": z["spy_o"].astype(float), "spy_c": z["spy_c"].astype(float)}
    D = prepare(D, z["h"].astype(float), z["l"].astype(float), z["rc"].astype(float), z["v"].astype(float), z["status"])
    del z
    return D


def prepare(D, H, L, rc, v, status=None):
    C = D["C"]
    D["Cf"] = ffill(C)
    D["sma200"], D["sma5"] = sma(C, 200), sma(C, 5)
    adv = pd.DataFrame(rc * v).rolling(20, min_periods=20).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        D["tradeable"] = (rc >= 10) & (adv >= 20e6) & np.isfinite(D["sma200"]) & np.isfinite(C)
        D["up"] = D["tradeable"] & (C > D["sma200"])
    D["wr"] = {n: williams_r(H, L, C, n).astype(np.float32) for n in (3, 5, 10)}
    D["rsi2"] = rsi2(C).astype(np.float32)
    del D["sma200"]
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = np.log(C[1:] / C[:-1])
    vol = np.vstack([np.full((1, C.shape[1]), np.nan), pd.DataFrame(ret).rolling(20, min_periods=20).std().to_numpy()])
    vol[~D["tradeable"]] = np.nan
    q = np.full(C.shape, np.nan)
    for t in range(len(C)):
        x = vol[t]
        ok = np.isfinite(x)
        if ok.sum() >= 50:
            edges = np.quantile(x[ok], [0.2, 0.4, 0.6, 0.8])
            q[t, ok] = np.searchsorted(edges, x[ok])
    D["volq"] = q.astype(np.float32)
    D["etf_like"] = sorted(set(D["syms"]) & ETF_SCAN)
    if status is not None:
        stat = np.asarray(status)
        last = [D["days"][int(np.where(np.isfinite(C[:, j]))[0][-1])][:4] if np.isfinite(C[:, j]).any() else None
                for j in range(C.shape[1])]
        liq_last = [D["days"][int(np.where(np.isfinite(C[:, j]))[0][-1])][:4] if np.isfinite(C[:, j]).any()
                    and D["tradeable"][int(np.where(np.isfinite(C[:, j]))[0][-1]) - 1, j] else None for j in range(C.shape[1])]
        end = D["days"][-1][:4]
        D["stops_per_year"] = {y: sum(1 for x in last if x == y and x != end) for y in sorted({x for x in last if x})}
        D["stops_liquid_per_year"] = {y: sum(1 for x in liq_last if x == y and x != end) for y in sorted({x for x in liq_last if x})}
        D["status_counts"] = {str(k): int((stat == k).sum()) for k in set(stat.tolist())}
    return D


VARIANTS = ["WR3", "WR5", "WR10", "info_WR10_le_-95", "info_WR10_no_sma200", "info_WR10_exit_wr_only",
            "info_WR10_exit_sma5_only", "info_WR10_close_entry", "info_RSI2"]


def variant(D, name):
    """-> (sig, exit, control, later-control, entry_close, kind). Built one at a time (memory). Kind 'hyp' = one of the
    three hypotheses; 'info' never carries a verdict. info_RSI2's same-name control uses RSI-2 >= 50 as the analogue of
    %R > -50 (not in the prereg text; stated here and in the output)."""
    up, tr, C, s5 = D["up"], D["tradeable"], D["C"], D["sma5"]
    with np.errstate(invalid="ignore"):
        if name in ("WR3", "WR5", "WR10"):
            wr = D["wr"][int(name[2:])]
            return up & (wr <= -90), (wr > -20) | (C > s5), up & (wr > -90), up & (wr > -50), False, "hyp"
        wr = D["wr"][10]
        if name == "info_WR10_le_-95":
            return up & (wr <= -95), (wr > -20) | (C > s5), up & (wr > -95), up & (wr > -50), False, "info"
        if name == "info_WR10_no_sma200":
            return tr & (wr <= -90), (wr > -20) | (C > s5), tr & (wr > -90), tr & (wr > -50), False, "info"
        if name == "info_WR10_exit_wr_only":
            return up & (wr <= -90), wr > -20, up & (wr > -90), up & (wr > -50), False, "info"
        if name == "info_WR10_exit_sma5_only":
            return up & (wr <= -90), C > s5, up & (wr > -90), up & (wr > -50), False, "info"
        if name == "info_WR10_close_entry":
            return up & (wr <= -90), (wr > -20) | (C > s5), up & (wr > -90), up & (wr > -50), True, "info"
        r = D["rsi2"]
        return up & (r < 10), (r > 70) | (C > s5), up & (r >= 10), up & (r >= 50), False, "info"


def variants(D):
    return {n: variant(D, n) for n in VARIANTS}


def ztest(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    x, y = x[np.isfinite(x)], y[np.isfinite(y)]
    if len(x) < 10 or len(y) < 10:
        return None
    se = math.sqrt(x.var(ddof=1) / len(x) + y.var(ddof=1) / len(y))
    return round(float((x.mean() - y.mean()) / se), 2) if se > 0 else None


def run_variant(D, name, spec, lo, hi, seeds=200):
    sig, ex, ctl, later, eclose, kind = spec
    tr, cn = simulate(D, sig, ex, ctl, lo, hi, entry_close=eclose, tag=name)
    a = [r for r in tr if r["day"] < SPLIT]
    b = [r for r in tr if r["day"] >= SPLIT]
    if lo == VERDICT[0]:
        v, A, B, P, checks = verdict(tr)
        if kind == "info":
            v = "INFO: " + v
    else:
        v, A, B, P, checks = "INFORMATION", half_stats(a), half_stats(b), half_stats(tr), {}
    sn = same_name_controls(D, tr, later, name)
    un = unconditional(D, tr, D["up"], name)
    un_r = [c for _, c in un]
    sn_lift = [(1 + tr[i]["r"]) / (1 + c) - 1 for i, c in sn]
    rep = {"verdict": v, "half1": A, "half2": B, "pooled": P, "checks": checks, "counts": cn,
           "same_name_later_lift_bp": bp(float(np.mean(sn_lift))) if sn_lift else None,
           "same_name_control_mean_bp": bp(float(np.mean([c for _, c in sn]))) if sn else None,
           "unconditional_mean_bp": bp(float(np.mean(un_r))) if un_r else None,
           "unconditional_mean_h1_bp": bp(float(np.mean([c for i, c in un if tr[i]["day"] < SPLIT]))) if any(tr[i]["day"] < SPLIT for i, _ in un) else None,
           "unconditional_mean_h2_bp": bp(float(np.mean([c for i, c in un if tr[i]["day"] >= SPLIT]))) if any(tr[i]["day"] >= SPLIT for i, _ in un) else None}
    if name == "info_RSI2":
        rep["note"] = "same-name control gate RSI-2 >= 50 is the analogue of %R > -50 (not in the prereg text)"
    # amended_1 M4 / TEST_STANDARD: EACH control's mean vs the unconditional mean; |z| > 2 blocks the verdict (review #2)
    zs = {"primary_control_vs_unconditional_z": ztest([r["c"] for r in tr], un_r),
          "volmatched_control_vs_unconditional_z": ztest([r["c_vol"] for r in tr], un_r),
          "same_name_control_vs_unconditional_z": ztest([c for _, c in sn], un_r)}
    rep.update(zs)
    bad = [k for k, z in zs.items() if z is not None and abs(z) > 2]
    if bad and (v.endswith("PASS") or "EDGE only" in v):
        rep["verdict"] = f"BLOCKED (control mismatch: {', '.join(bad)}) - was {v}"
    if tr:
        yrs = (pd.Timestamp(hi) - pd.Timestamp(lo)).days / 365.25
        rep["account_10slot_info"] = account(tr, D["days"], yrs, seeds=seeds)
        i0 = D["days"].index(next(d for d in D["days"] if d >= lo))
        i1 = max(i for i, d in enumerate(D["days"]) if d <= hi)
        rep["spy_yearly_pct"] = round(100 * ((D["spy_c"][i1] / D["spy_o"][i0]) ** (1 / yrs) - 1), 2)
    return rep


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    D = load()
    out = {"prereg": "docs/studies/daily_wr_swing_prereg.json (amended_1)", "verdict_window": VERDICT, "split": SPLIT,
           "names": len(D["syms"]), "stops_per_year": D.get("stops_per_year"),
           "stops_liquid_per_year": D.get("stops_liquid_per_year"), "status_counts": D.get("status_counts"),
           "etf_like_symbols_in_panel": D["etf_like"], "variants": {}}
    for name in VARIANTS:
        spec = variant(D, name)
        rep = {"VERDICT_WINDOW": run_variant(D, name, spec, *VERDICT, seeds=200 if spec[5] == "hyp" else 50)}
        if spec[5] == "hyp" or name == "info_RSI2":
            for w, (lo, hi) in INFO_WINDOWS.items():
                rep[w] = run_variant(D, name, spec, lo, hi, seeds=50)
        del spec
        out["variants"][name] = rep
        print(name, rep["VERDICT_WINDOW"]["verdict"], json.dumps(rep["VERDICT_WINDOW"]["pooled"]), flush=True)
        (OUT / "result.json").write_text(json.dumps(out, indent=1, default=str))
    lines = ["# Daily %R swing (prereg docs/studies/daily_wr_swing_prereg.json, amended_1)", "",
             f"Verdict window {VERDICT[0]}..{VERDICT[1]}, halves split {SPLIT}. Lift = (1+r)/(1+control)-1, bp per trade; money line 30 bp.", "",
             "| variant | verdict | n | lift h1 | lift h2 | t | t resid c | t resid SPY | vol-matched h1/h2 | MDE h1/h2 | raw net30 | win % |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, rep in out["variants"].items():
        V = rep["VERDICT_WINDOW"]
        A, B, P = V["half1"], V["half2"], V["pooled"]
        lines.append(f"| {name} | {V['verdict']} | {P.get('n')} | {A.get('lift_bp')} | {B.get('lift_bp')} | {P.get('t')} | "
                     f"{P.get('t_resid_c')} | {P.get('t_resid_spy')} | {A.get('lift_volmatched_bp')} / {B.get('lift_volmatched_bp')} | "
                     f"{A.get('mde_bp')} / {B.get('mde_bp')} | {P.get('raw_net30_bp')} | {P.get('win_pct')} |")
    (OUT / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    {"run": run}[sys.argv[1]]()
