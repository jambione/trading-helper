#!/usr/bin/env python3
"""Overnight book on 2000-2015 Yahoo data, exactly per docs/studies/overnight_pre2016_prereg.json (fa3bf43).

Phases (run on the mini from the repo root with .venv/bin/python):
  fetch   yfinance daily bars + actions for every lh_cache panel symbol and SPY, 1998-06-01..today (the split history
          to today is needed to undo Yahoo's backward split adjustment). Cached per batch in WORK; resumable.
  check   NO pre-2016 outcomes: coverage, ON==0 shares by year (picks vs universe), drops by rule, the 2017-2021
          Yahoo-vs-Alpaca data-source check, and the 2018-2026 survivorship calibration on the Alpaca panel.
  score   the pre-registered test on 2000-2015 (power first), plus information; refuses to give a verdict if the
          data-source check failed.
WORK default ~/repo/trading-helper/data/yahoo_pre2016 (git-ignored by data/).
"""
from __future__ import annotations

import json
import math
import os
import pickle
import sys
import time

import numpy as np
import pandas as pd

REPO = os.environ.get("REPO") or os.path.expanduser("~/repo/trading-helper")
WORK = os.environ.get("WORK") or os.path.join(REPO, "data/yahoo_pre2016")
LH = os.path.expanduser("~/lh_cache")
sys.path.insert(0, LH)

START, PRE_END = "1998-06-01", "2015-12-31"
P_FIRST, P1_END = pd.Timestamp("2000-01-03"), pd.Timestamp("2007-12-31")
CHK = (pd.Timestamp("2017-01-13"), pd.Timestamp("2021-12-31"))
COST, SPY_COST = 4e-4, 2.26e-4
TOP, MIN_ELIG, PX_MIN, ADV_MIN = 20, 30, 5.0, 50e6


def P_(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ statistics
def nw_se(X, u, L):
    n = len(u)
    XI = np.linalg.inv(X.T @ X)
    S = (X * u[:, None]).T @ (X * u[:, None])
    for l in range(1, L + 1):
        w = 1 - l / (L + 1)
        G = (X[l:] * u[l:, None]).T @ (X[:-l] * u[:-l, None])
        S += w * (G + G.T)
    V = XI @ S @ XI * n / (n - X.shape[1])
    return np.sqrt(np.diag(V))


def alpha(y, x):
    """Intercept and slope of y on [1, x] (bp, per night), SE = max(plain, NW5, NW9)."""
    ok = np.isfinite(y) & np.isfinite(x)
    y, x = y[ok] * 1e4, x[ok] * 1e4
    X = np.column_stack([np.ones(len(y)), x])
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    u = y - X @ b
    plain = np.sqrt(np.diag(np.linalg.inv(X.T @ X)) * (u @ u) / (len(y) - 2))
    se = np.maximum.reduce([plain, nw_se(X, u, 5), nw_se(X, u, 9)])
    return {"n": int(len(y)), "alpha_bp": round(float(b[0]), 2), "beta": round(float(b[1]), 3),
            "se_bp": round(float(se[0]), 2), "t": round(float(b[0] / se[0]), 2), "mde_bp": round(2.84 * float(se[0]), 2)}


# ------------------------------------------------------------------ fetch
def panel_syms():
    z = np.load("/tmp/lh/panel.npz", allow_pickle=False)
    return [str(s) for s in z["syms"]]


def fetch():
    import yfinance as yf
    os.makedirs(os.path.join(WORK, "raw"), exist_ok=True)
    syms = sorted(set(panel_syms()) | {"SPY"})
    fails = []
    for i in range(0, len(syms), 100):
        fp = os.path.join(WORK, "raw", f"b{i:05d}.pkl")
        if os.path.exists(fp):
            continue
        batch = syms[i:i + 100]
        ymap = {s.replace(".", "-"): s for s in batch}
        out = None
        for att in range(5):
            try:
                df = yf.download(list(ymap), start=START, end=None, auto_adjust=False, actions=True, repair=False,
                                 group_by="ticker", threads=True, progress=False, timeout=60)
                out = {}
                for ys, s in ymap.items():
                    try:
                        sub = df[ys].dropna(how="all")
                    except KeyError:
                        sub = None
                    out[s] = sub if sub is not None and len(sub) else None
                break
            except Exception as e:  # noqa: BLE001
                P_(f"  batch {i} try {att}: {type(e).__name__} {str(e)[:120]}")
                time.sleep(30 * (att + 1))
        if out is None:
            fails.append(i)
            P_(f"  batch {i} FAILED")
            continue
        pickle.dump(out, open(fp, "wb"))
        P_(f"fetch {i + len(batch)}/{len(syms)}: {sum(v is not None for v in out.values())} with data")
        time.sleep(2)
    P_(f"fetch done; failed batches {fails}")


# ------------------------------------------------------------------ build arrays
def build():
    cache = os.path.join(WORK, "arrays.pkl")
    if os.path.exists(cache):
        return pickle.load(open(cache, "rb"))
    frames = {}
    for fn in sorted(os.listdir(os.path.join(WORK, "raw"))):
        for s, df in pickle.load(open(os.path.join(WORK, "raw", fn), "rb")).items():
            if df is not None:
                frames[s] = df
    spy = frames.pop("SPY")
    dates = spy.index.tz_localize(None) if spy.index.tz is not None else spy.index
    syms = sorted(frames)
    T, N = len(dates), len(syms)
    A = {k: np.full((T, N), np.nan) for k in ("o", "h", "l", "c", "ac", "v", "div", "spl")}
    for j, s in enumerate(syms):
        df = frames[s]
        idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
        df = df.set_axis(idx).reindex(dates)
        for k, col in (("o", "Open"), ("h", "High"), ("l", "Low"), ("c", "Close"), ("ac", "Adj Close"), ("v", "Volume"),
                       ("div", "Dividends"), ("spl", "Stock Splits")):
            if col in df:
                A[k][:, j] = df[col].values
    A["div"] = np.nan_to_num(A["div"])
    A["spl"] = np.nan_to_num(A["spl"])
    # raw close = split-adjusted close x product of split ratios strictly AFTER t
    fac = np.ones((T, N))
    s = np.where(A["spl"] > 0, A["spl"], 1.0)
    fac[:-1] = np.flip(np.cumprod(np.flip(s[1:], 0), 0), 0)
    A["rc"] = A["c"] * fac
    A["split_near"] = np.zeros((T, N), bool)
    hit = A["spl"] > 0
    for sh in (-1, 0, 1):
        A["split_near"] |= np.roll(hit, sh, 0)
    sdf = spy.set_axis(dates)
    A["spy_o"], A["spy_c"] = sdf["Open"].values, sdf["Close"].values
    A["spy_div"] = np.nan_to_num(sdf["Dividends"].values) if "Dividends" in sdf else np.zeros(T)
    A["dates"], A["syms"] = dates, syms
    pickle.dump(A, open(cache, "wb"))
    return A


def derive(A):
    """ON with dividends and the bad-data rules (applied to all names and SPY), ADV20, momentum, bad name-years."""
    o, c, h, l, v = A["o"], A["c"], A["h"], A["l"], A["v"]
    T, N = c.shape
    drops = {}
    ON = np.full((T, N), np.nan)
    with np.errstate(all="ignore"):
        ON[1:] = (o[1:] + A["div"][1:]) / c[:-1] - 1
    bad_px = ~(np.isfinite(ON)) | (np.vstack([np.zeros((1, N), bool), (c[:-1] <= 0)]) | (o <= 0))
    stale = (o == h) & (h == l) & (l == c) & (v == 0)
    big = np.abs(ON) > 0.5
    spl = A["split_near"]
    had = np.isfinite(ON)
    drops["stale_bar"] = int((stale & had).sum())
    drops["abs_on_gt_50pct"] = int((big & had).sum())
    drops["split_pm1"] = int((spl & had).sum())
    ON[bad_px | stale | big | spl] = np.nan
    # name-years with > 25% ON == 0
    yr = pd.DatetimeIndex(A["dates"]).year.values
    badyr = np.zeros((T, N), bool)
    zshare = {}
    for y in np.unique(yr):
        r = yr == y
        f = np.isfinite(ON[r])
        z = (ON[r] == 0) & f
        share = z.sum(0) / np.maximum(f.sum(0), 1)
        bad = share > 0.25
        badyr[r] |= bad[None, :]
        zshare[int(y)] = share
    drops["bad_name_year_nights"] = int((badyr & np.isfinite(ON)).sum())
    ON[badyr] = np.nan
    spy_on = np.full(T, np.nan)
    with np.errstate(all="ignore"):
        spy_on[1:] = (A["spy_o"][1:] + A["spy_div"][1:]) / A["spy_c"][:-1] - 1
    spy_on[np.abs(spy_on) > 0.5] = np.nan
    adv = pd.DataFrame(c * v).rolling(20, min_periods=15).mean().values
    cf = pd.DataFrame(A["ac"]).ffill().values
    m = np.full((T, N), np.nan)
    with np.errstate(all="ignore"):
        m[252:] = cf[252 - 21:T - 21] / cf[:T - 252] - 1
    return {"ON": ON, "spy_on": spy_on, "adv": adv, "cf": cf, "m": m, "drops": drops, "zshare": zshare, "badyr": badyr}


def book_series(dates, rc, adv, m, cf, ON, spy_on, t_lo, t_hi, keep=None):
    """Nightly picks/universe overnight means for buy dates t in [t_lo, t_hi]; same rule as overnight_hedge_intraday."""
    D = pd.DatetimeIndex(dates)
    rows = []
    for t in range(252, len(D) - 1):
        if not (t_lo <= D[t] <= t_hi):
            continue
        ok = (rc[t] >= PX_MIN) & (adv[t] >= ADV_MIN) & np.isfinite(m[t]) & np.isfinite(cf[t])
        if keep is not None:
            ok &= keep
        idx = np.where(ok)[0]
        if len(idx) < MIN_ELIG:
            rows.append((D[t], np.nan, np.nan, np.nan, len(idx), None))
            continue
        pick = idx[np.argsort(-m[t, idx])][:TOP]
        on_p, on_u = ON[t + 1, pick], ON[t + 1, idx]
        rows.append((D[t], np.nanmean(on_p) if np.isfinite(on_p).any() else np.nan,
                     np.nanmean(on_u) if np.isfinite(on_u).any() else np.nan, spy_on[t + 1], len(idx), pick))
    df = pd.DataFrame(rows, columns=["date", "picks", "univ", "spy", "n_elig", "pick"]).set_index("date")
    return df


def s1_s2(df):
    book = df["picks"].values - COST
    a1 = alpha(book, df["spy"].values)
    a1["alpha_bp"] = round(a1["alpha_bp"] - SPY_COST * 1e4 * a1["beta"], 2)   # hedge leg cost at the fitted beta
    a1["t"] = round(a1["alpha_bp"] / a1["se_bp"], 2)
    a2 = alpha(df["picks"].values - df["univ"].values, df["spy"].values)
    return a1, a2


# ------------------------------------------------------------------ check (no pre-2016 outcomes)
def check():
    import lh_core as C
    import lh_strat as S
    A = build()
    X = derive(A)
    dates = pd.DatetimeIndex(A["dates"])
    res = {"symbols_with_yahoo_data": len(A["syms"]), "panel_symbols": len(panel_syms()), "drops": X["drops"]}
    pre = (dates >= P_FIRST) & (dates <= pd.Timestamp(PRE_END))
    # coverage: nights with >= 30 eligible (no outcomes)
    cnt = []
    for t in np.where(pre)[0]:
        ok = (A["rc"][t] >= PX_MIN) & (X["adv"][t] >= ADV_MIN) & np.isfinite(X["m"][t]) & np.isfinite(X["cf"][t])
        cnt.append(int(ok.sum()))
    cnt = np.array(cnt)
    res["coverage"] = {"nights": int(len(cnt)), "share_ge_30": round(float((cnt >= MIN_ELIG).mean()), 3),
                       "median_elig": int(np.median(cnt)), "min_elig": int(cnt.min()),
                       "median_elig_by_year": {int(y): int(np.median(cnt[dates[pre].year == y])) for y in range(2000, 2016)}}
    res["on_zero_share_by_year_universe_mean"] = {y: round(float(np.nanmean(s[s > 0])) if (s > 0).any() else 0.0, 3)
                                                 for y, s in X["zshare"].items() if 1999 <= y <= 2015}
    # data-source check 2017-01-13..2021-12-31: Yahoo book vs Alpaca-panel book, same symbols
    Pn = S.returns(C.load())
    psyms = [str(s) for s in Pn["syms"]]
    common = sorted(set(psyms) & set(A["syms"]))
    yj = [A["syms"].index(s) for s in common]
    pj = [psyms.index(s) for s in common]
    ydf = book_series(dates, A["rc"][:, yj], X["adv"][:, yj], X["m"][:, yj], X["cf"][:, yj], X["ON"][:, yj], X["spy_on"], *CHK)
    pd_ = pd.DatetimeIndex(Pn["dates"])
    pcf = Pn["cf"][:, pj]
    pm = np.full_like(pcf, np.nan)
    with np.errstate(all="ignore"):
        pm[252:] = pcf[252 - 21:len(pd_) - 21] / pcf[:len(pd_) - 252] - 1
    pspy = np.r_[np.nan, Pn["spy_o"][1:] / Pn["spy_c"][:-1] - 1]
    pdf = book_series(pd_, Pn["rc"][:, pj], Pn["adv20"][:, pj], pm, pcf, Pn["ON"][:, pj], pspy, *CHK)
    j = ydf.join(pdf, lsuffix="_y", rsuffix="_p", how="inner").dropna(subset=["picks_y", "picks_p"])
    dif = (j["picks_y"] - j["picks_p"]).values * 1e4
    ov = [len(set(np.array(common)[a]) & set(np.array(common)[b])) / TOP for a, b in zip(j["pick_y"], j["pick_p"])
          if a is not None and b is not None]
    corr = float(np.corrcoef(j["picks_y"], j["picks_p"])[0, 1])
    tdif = float(dif.mean() / (dif.std(ddof=1) / math.sqrt(len(dif))))
    res["data_source_check"] = {"nights": int(len(j)), "common_symbols": len(common), "mean_diff_bp": round(float(dif.mean()), 2),
                                "t_diff": round(tdif, 2), "corr": round(corr, 3), "pick_overlap_mean": round(float(np.mean(ov)), 3),
                                "pass": bool(abs(dif.mean()) <= 1 and abs(tdif) < 2 and corr >= 0.9)}
    # survivorship calibration on the Alpaca panel, 2018-2026: full vs survivors (bars in the panel's last month)
    surv = np.isfinite(Pn["c"][-21:]).any(0)
    pmf = np.full_like(Pn["cf"], np.nan)
    with np.errstate(all="ignore"):
        pmf[252:] = Pn["cf"][252 - 21:len(pd_) - 21] / Pn["cf"][:len(pd_) - 252] - 1
    lo, hi = pd.Timestamp("2018-01-01"), pd_[-2]
    full = book_series(pd_, Pn["rc"], Pn["adv20"], pmf, Pn["cf"], Pn["ON"], pspy, lo, hi)
    sv = book_series(pd_, Pn["rc"], Pn["adv20"], pmf, Pn["cf"], Pn["ON"], pspy, lo, hi, keep=surv)
    f1, f2 = s1_s2(full.dropna(subset=["picks"]))
    s1, s2 = s1_s2(sv.dropna(subset=["picks"]))
    res["survivorship_calibration_2018_2026"] = {"full": {"S1": f1, "S2": f2}, "survivors_only": {"S1": s1, "S2": s2},
                                                 "gap_S1_bp": round(s1["alpha_bp"] - f1["alpha_bp"], 2),
                                                 "gap_S2_bp": round(s2["alpha_bp"] - f2["alpha_bp"], 2),
                                                 "survivor_share_of_symbols": round(float(surv.mean()), 3)}
    json.dump(res, open(os.path.join(WORK, "check.json"), "w"), indent=1, default=str)
    P_(json.dumps(res, indent=1, default=str))


# ------------------------------------------------------------------ score (pre-2016 outcomes)
def score():
    ck = json.load(open(os.path.join(WORK, "check.json")))
    A = build()
    X = derive(A)
    dates = A["dates"]
    df = book_series(dates, A["rc"], X["adv"], X["m"], X["cf"], X["ON"], X["spy_on"], P_FIRST, pd.Timestamp(PRE_END))
    res = {"prereg": "docs/studies/overnight_pre2016_prereg.json (fa3bf43)", "check": ck,
           "nights": int(len(df)), "nights_skipped_lt30": int(df["picks"].isna().sum())}
    if ck["coverage"]["share_ge_30"] < 0.60:
        res["verdict"] = "NO VERDICT: coverage < 60% of nights with >= 30 eligible names"
    d = df.dropna(subset=["picks", "univ", "spy"])
    halves = {"all": d, "P1": d[d.index <= P1_END], "P2": d[d.index > P1_END]}
    T = {k: dict(zip(("S1", "S2"), s1_s2(v))) for k, v in halves.items()}
    res["tables"] = T
    pw = {s: {"mde_bp": T["all"][s]["mde_bp"], "powered": T["all"][s]["mde_bp"] <= 5.0} for s in ("S1", "S2")}
    res["power"] = pw
    surv = ck["survivorship_calibration_2018_2026"]

    def decided(s, gap):
        a = T["all"][s]
        voided = abs(gap) >= 0.5 * abs(a["alpha_bp"]) if a["alpha_bp"] else True
        return {"powered": pw[s]["powered"], "t>=2": a["t"] >= 2.0, "positive": a["alpha_bp"] > 0,
                "P1>0": T["P1"][s]["alpha_bp"] > 0, "P2>0": T["P2"][s]["alpha_bp"] > 0, "survivorship_ok": not voided}
    crit = {"EDGE_REPLICATES (S2)": decided("S2", surv["gap_S2_bp"]), "ALPHA (S1)": decided("S1", surv["gap_S1_bp"])}
    res["criteria"] = crit
    if "verdict" not in res:
        if not ck["data_source_check"]["pass"]:
            res["verdict"] = "NO VERDICT: the 2017-2021 data-source check failed"
        else:
            e, a = all(crit["EDGE_REPLICATES (S2)"].values()), all(crit["ALPHA (S1)"].values())
            res["verdict"] = {(True, True): "EDGE_REPLICATES + ALPHA", (True, False): "EDGE_REPLICATES only",
                              (False, True): "ALPHA only: UNEXPLAINED", (False, False): "NEITHER (or underpowered)"}[(e, a)]
    # information
    book = d["picks"].values - COST
    info = {"unhedged_net_bp": round(float(np.mean(book)) * 1e4, 2),
            "unhedged_t_plain": round(float(np.mean(book) / (np.std(book, ddof=1) / math.sqrt(len(book)))), 2)}
    h = d["spy"].values
    info["S1_fixed_h1.34_bp"] = round(float(np.mean(book - 1.34 * h - SPY_COST * 1.34)) * 1e4, 2)
    bt = pd.Series(book, index=d.index)
    hs = pd.Series(h, index=d.index)
    cov = bt.rolling(252, min_periods=200).cov(hs).shift(1)
    var = hs.rolling(252, min_periods=200).var().shift(1)
    beta_ex = (cov / var).values
    hed = book - beta_ex * h - SPY_COST * np.abs(beta_ex)
    hed = hed[np.isfinite(hed)]
    info["S1_trailing252_beta_hedge_bp"] = round(float(hed.mean()) * 1e4, 2)
    info["S1_trailing252_t_plain"] = round(float(hed.mean() / (hed.std(ddof=1) / math.sqrt(len(hed)))), 2)
    e02 = d[d.index.year <= 2002]
    info["S1_2000_2002_at_20bp"] = s1_s2(e02.assign(picks=e02["picks"] - (20e-4 - COST)))[0]
    x = book * 1e4
    k = max(1, len(x) // 100)
    cum = np.cumsum(x)
    info["tail_unhedged"] = {"worst_night_bp": round(float(x.min()), 1), "cvar1_bp": round(float(np.sort(x)[:k].mean()), 1),
                             "max_dd_bp": round(float((cum - np.maximum.accumulate(cum)).min()), 1)}
    info["per_year"] = {int(y): {"S1": s1_s2(g)[0]["alpha_bp"], "S2": s1_s2(g)[1]["alpha_bp"], "n": int(len(g))}
                        for y, g in d.groupby(d.index.year) if len(g) > 30}
    info["n_elig_median"] = int(df["n_elig"].median())
    res["information"] = info
    json.dump(res, open(os.path.join(WORK, "result.json"), "w"), indent=1, default=str)
    P_(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"fetch": fetch, "check": check, "score": score}.get(cmd, lambda: P_(__doc__))()
