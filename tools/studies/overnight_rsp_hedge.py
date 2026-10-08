#!/usr/bin/env python3
"""Overnight book hedged with RSP (equal weight) vs SPY, exactly per docs/studies/overnight_rsp_hedge_prereg.json.

Phases (run on the mini from the repo root with .venv/bin/python, after hours):
  fetch   RSP and SPY daily from Yahoo (auto_adjust=False, actions=True) from 2003-01-01, and RSP daily from Alpaca
          (SIP, adjustment=all) 2016-01-01..2026-09-26. Two Yahoo symbols and one Alpaca symbol; cached in WORK.
  check   NO book outcomes: the 2017-2021 Yahoo-vs-Alpaca data-source check for RSP and SPY overnight returns,
          RSP bad-data drops and the RSP ON == 0 share by year.
  score   period A (2004-2015, Yahoo book from overnight_pre2016.py) decides; period B (2017-10..2026-09, Alpaca
          panel book as overnight_hedge_intraday.py) must be > 0. Power first; information after the verdict.
WORK default ~/repo/trading-helper/data/rsp_hedge (git-ignored by data/).
"""
from __future__ import annotations

import json
import math
import os
import pickle
import sys

import numpy as np
import pandas as pd

REPO = os.environ.get("REPO") or os.path.expanduser("~/repo/trading-helper")
WORK = os.environ.get("WORK") or os.path.join(REPO, "data/rsp_hedge")
LH = os.path.expanduser("~/lh_cache")
HERE = os.path.dirname(os.path.abspath(__file__))

COST = 4e-4            # book round trip at the auctions (frozen book)
RSP_COST = 3e-4        # per unit of hedge per round trip (primary)
RSP_COST_SENS = 6e-4   # decision-gating sensitivity
RSP_COST_INFO = 10e-4  # information
SPY_COST = 2.26e-4     # baseline SPY hedge, per unit
WIN, MINP = 252, 200   # trailing beta window (nights t-252..t-1) and minimum valid pairs
A_LO, A_HI = pd.Timestamp("2003-04-30"), pd.Timestamp("2015-12-30")   # buy dates used for period A (warm-up from RSP's listing)
B_HI = pd.Timestamp("2026-09-24")
CHK = (pd.Timestamp("2017-01-01"), pd.Timestamp("2021-12-31"))
RSP_MAX_ABS_ON = 0.15
ZERO_YEAR_MAX = 0.25
MDE_MAX_BP = 5.0

RESOLUTIONS = {
    "night_label": "a night is labelled by its BUY date t (close t -> open t+1); period ends compare the buy date",
    "period_A_scored": "buy dates <= 2015-12-30 with a finite trailing beta for BOTH hedges (>= 200 valid pairs in t-252..t-1); "
                       "beta rows start at RSP's first Yahoo night, so the first scored night falls ~2004-05 as the prereg expects",
    "period_B_scored": "buy dates <= 2026-09-24, book nights from panel index 260 (as overnight_hedge_intraday.py), scored once both betas exist",
    "beta_window": "the previous 252 rows of the nightly series (dropped nights stay in the row count); pairs counted only where the "
                   "book and the hedge ON are both finite",
    "zero_year": "the RSP ON == 0 share is computed per calendar year of the buy date; a year above 25% is dropped from both hedges",
    "rsp_bad_night": "Yahoo/Alpaca open <= 0 or missing, prior close <= 0 or missing, or |ON| > 15% -> RSP ON is NaN (counted)",
    "common_set": "nights with finite book, RSP ON, SPY ON, h_RSP and h_SPY, outside dropped years; every primary statistic uses it",
    "hedge_cost": "cost x |h| per night, with h the night's own trailing beta",
    "tracking_r2": "R^2 of the simple OLS of the book's net return on the hedge ON over the period's common set",
    "decomposition": "per night: hedged = (picks - univ) + (univ - h x RSP_ON) - 4 bp - 3 bp x |h|; S2 term = mean(picks - univ); "
                     "the survivorship rule tests mean(S2) - 4 bp - 3 bp x mean|h| <= 0",
    "verdict_order": "data-source check failed -> UNPROVEN; A underpowered -> UNPROVEN; A criteria not all met -> FAIL; "
                     "A passes only through universe alpha -> UNPROVEN; B <= 0 -> UNPROVEN; else PASS",
    "sharpe": "mean / sd x sqrt(252) on nightly net bp",
}


def P_(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ pure statistics
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


def mean_stat(x):
    """Mean of a nightly series (fractions in, bp out), SE = max(plain, NW5, NW9), t and MDE = 2.84 x SE."""
    x = np.asarray(x, float)
    x = x[np.isfinite(x)] * 1e4
    n = len(x)
    if n < 20:
        return {"n": n, "mean_bp": float("nan"), "se_bp": float("nan"), "t": float("nan"), "mde_bp": float("nan")}
    X = np.ones((n, 1))
    u = x - x.mean()
    plain = x.std(ddof=1) / math.sqrt(n)
    se = max(plain, float(nw_se(X, u, 5)[0]), float(nw_se(X, u, 9)[0]))
    return {"n": n, "mean_bp": round(float(x.mean()), 3), "se_bp": round(se, 3), "t": round(float(x.mean() / se), 2),
            "mde_bp": round(2.84 * se, 3)}


def on_series(o_next, c_prev, div_next=None):
    """Overnight return close(t) -> open(t+1) (+ dividend paid at t+1); bad legs and |ON| > 15% -> NaN.
    Inputs are aligned arrays for the night (o and div of the open day, c of the prior day)."""
    o_next, c_prev = np.asarray(o_next, float), np.asarray(c_prev, float)
    d = np.zeros_like(o_next) if div_next is None else np.nan_to_num(np.asarray(div_next, float))
    with np.errstate(all="ignore"):
        on = (o_next + d) / c_prev - 1
    bad = ~np.isfinite(on) | ~(o_next > 0) | ~(c_prev > 0) | (np.abs(on) > RSP_MAX_ABS_ON)
    on[bad] = np.nan
    return on, int(bad.sum())


def trailing_beta(y, x, win=WIN, minp=MINP):
    """beta[t] = OLS slope of y on x over rows t-win..t-1 only (point-in-time), >= minp pairs with both finite."""
    y, x = np.asarray(y, float), np.asarray(x, float)
    ok = np.isfinite(y) & np.isfinite(x)
    ys, xs = pd.Series(np.where(ok, y, np.nan)), pd.Series(np.where(ok, x, np.nan))
    cov = ys.rolling(win, min_periods=minp).cov(xs)
    var = xs.rolling(win, min_periods=minp).var()
    return (cov / var).shift(1).values


def r2(y, x):
    y, x = np.asarray(y, float), np.asarray(x, float)
    return float(np.corrcoef(y, x)[0, 1] ** 2)


def tails(x):
    x = np.asarray(x, float) * 1e4
    k = max(1, len(x) // 100)
    cum = np.cumsum(x)
    return {"mean_bp": round(float(x.mean()), 2), "sd_bp": round(float(x.std(ddof=1)), 2),
            "worst_bp": round(float(x.min()), 1), "cvar1_bp": round(float(np.sort(x)[:k].mean()), 1),
            "max_dd_bp": round(float((cum - np.maximum.accumulate(cum)).min()), 1),
            "sharpe": round(float(x.mean() / x.std(ddof=1) * math.sqrt(252)), 2)}


def zero_years(on, years):
    """RSP ON == 0 share by calendar year (finite nights only) and the set of years above the 25% rule."""
    on, years = np.asarray(on, float), np.asarray(years)
    share = {}
    for y in np.unique(years):
        f = (years == y) & np.isfinite(on)
        share[int(y)] = round(float((on[f] == 0).sum() / max(f.sum(), 1)), 4)
    return share, {y for y, s in share.items() if s > ZERO_YEAR_MAX}


def hedge_frame(df, cost_rsp=RSP_COST):
    """df columns: book (net of COST), picks, univ, rsp, spy (ON fractions), year. Adds betas, hedged series and the
    common-set flag. Betas use every row (point-in-time); dropped years are excluded only from scoring."""
    df = df.copy()
    df["h_rsp"] = trailing_beta(df["book"].values, df["rsp"].values)
    df["h_spy"] = trailing_beta(df["book"].values, df["spy"].values)
    df["hed_rsp"] = df["book"] - df["h_rsp"] * df["rsp"] - cost_rsp * df["h_rsp"].abs()
    df["hed_spy"] = df["book"] - df["h_spy"] * df["spy"] - SPY_COST * df["h_spy"].abs()
    cols = ["book", "picks", "univ", "rsp", "spy", "h_rsp", "h_spy"]
    df["common"] = np.isfinite(df[cols].values).all(1) & ~df["drop_year"].values
    return df


def evaluate(c):
    """The pre-registered numbers on one period's common set (c = rows with common True)."""
    hr = c["hed_rsp"].values
    out = {"nights": int(len(c)), "first": str(c.index.min().date()) if len(c) else None,
           "last": str(c.index.max().date()) if len(c) else None,
           "rsp_hedged": mean_stat(hr),
           "rsp_hedged_6bp": mean_stat(c["book"] - c["h_rsp"] * c["rsp"] - RSP_COST_SENS * c["h_rsp"].abs()),
           "spy_hedged": mean_stat(c["hed_spy"].values),
           "paired_rsp_minus_spy": mean_stat((c["hed_rsp"] - c["hed_spy"]).values),
           "sd_rsp_hedged_bp": round(float(np.std(hr, ddof=1) * 1e4), 3),
           "sd_spy_hedged_bp": round(float(np.std(c["hed_spy"].values, ddof=1) * 1e4), 3),
           "r2_book_on_rsp": round(r2(c["book"], c["rsp"]), 4), "r2_book_on_spy": round(r2(c["book"], c["spy"]), 4),
           "mean_abs_h_rsp": round(float(c["h_rsp"].abs().mean()), 3), "mean_abs_h_spy": round(float(c["h_spy"].abs().mean()), 3)}
    s2 = float((c["picks"] - c["univ"]).mean() * 1e4)
    ua = float((c["univ"] - c["h_rsp"] * c["rsp"]).mean() * 1e4)
    out["decomposition_bp"] = {"S2": round(s2, 3), "universe_alpha_vs_rsp": round(ua, 3), "book_cost": -COST * 1e4,
                               "hedge_cost": round(-RSP_COST * 1e4 * out["mean_abs_h_rsp"], 3),
                               "sum": round(s2 + ua - COST * 1e4 - RSP_COST * 1e4 * out["mean_abs_h_rsp"], 3),
                               "S2_minus_costs": round(s2 - COST * 1e4 - RSP_COST * 1e4 * out["mean_abs_h_rsp"], 3)}
    return out


def verdict(A, B, source_ok):
    """Decision per the prereg's pass rule. A and B are evaluate() outputs. Returns (verdict, criteria)."""
    a = A["rsp_hedged"]
    crit = {"source_check_pass": bool(source_ok), "A_powered": bool(a["mde_bp"] <= MDE_MAX_BP),
            "A_rsp_hedged_gt0": bool(a["mean_bp"] > 0), "A_t_ge_2": bool(a["t"] >= 2.0),
            "A_gt0_at_6bp": bool(A["rsp_hedged_6bp"]["mean_bp"] > 0),
            "A_lower_sd_than_spy": bool(A["sd_rsp_hedged_bp"] < A["sd_spy_hedged_bp"]),
            "A_higher_r2_than_spy": bool(A["r2_book_on_rsp"] > A["r2_book_on_spy"]),
            "A_not_survivorship_only": bool(A["decomposition_bp"]["S2_minus_costs"] > 0),
            "B_rsp_hedged_gt0": bool(B["rsp_hedged"]["mean_bp"] > 0)}
    if not crit["source_check_pass"]:
        return "UNPROVEN (data-source check failed: period A gets no verdict)", crit
    if not crit["A_powered"]:
        return "UNPROVEN (period A underpowered: MDE > 5 bp)", crit
    core = ("A_rsp_hedged_gt0", "A_t_ge_2", "A_gt0_at_6bp", "A_lower_sd_than_spy", "A_higher_r2_than_spy")
    if not all(crit[k] for k in core):
        return "FAIL (closes the hedge-instrument question)", crit
    if not crit["A_not_survivorship_only"]:
        return "UNPROVEN (period A passes only through universe alpha vs RSP: survivorship rule)", crit
    if not crit["B_rsp_hedged_gt0"]:
        return "UNPROVEN (period B <= 0)", crit
    return "PASS (input to gate O1 only)", crit


# ------------------------------------------------------------------ fetch
def _yahoo(sym):
    import yfinance as yf
    df = yf.Ticker(sym).history(start="2003-01-01", auto_adjust=False, actions=True, repair=False, raise_errors=True, timeout=60)
    df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index
    return df[["Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"]]


def _alpaca_rsp():
    import requests
    sys.path.insert(0, REPO)
    from config import load_config
    c = load_config() or {}
    H = {"APCA-API-KEY-ID": c.get("api_key"), "APCA-API-SECRET-KEY": c.get("secret_key")}
    rows, tok = [], None
    while True:
        prm = {"symbols": "RSP", "timeframe": "1Day", "start": "2016-01-01", "end": "2026-09-26", "limit": 10000,
               "feed": "sip", "adjustment": "all"}
        if tok:
            prm["page_token"] = tok
        r = requests.get("https://data.alpaca.markets/v2/stocks/bars", params=prm, headers=H, timeout=90)
        r.raise_for_status()
        j = r.json()
        rows += (j.get("bars") or {}).get("RSP", [])
        tok = j.get("next_page_token")
        if not tok:
            break
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["t"]).dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
    return df.set_index("date")[["o", "h", "l", "c", "v"]]


def fetch():
    now = pd.Timestamp.now(tz="America/New_York")
    if now.weekday() < 5 and 9 <= now.hour + now.minute / 60 < 16.5:
        raise SystemExit("fetch refused: run after 16:30 ET")
    os.makedirs(WORK, exist_ok=True)
    out = {"yahoo_RSP": _yahoo("RSP"), "yahoo_SPY": _yahoo("SPY"), "alpaca_RSP": _alpaca_rsp()}
    pickle.dump(out, open(os.path.join(WORK, "raw.pkl"), "wb"))
    P_({k: (len(v), str(v.index.min().date()), str(v.index.max().date())) for k, v in out.items()})


def _raw():
    return pickle.load(open(os.path.join(WORK, "raw.pkl"), "rb"))


def yahoo_on(df):
    """ON indexed by the OPEN date t+1: (Open(t+1) + Div(t+1)) / Close(t) - 1, bad nights NaN."""
    on, nbad = on_series(df["Open"].values[1:], df["Close"].values[:-1], df["Dividends"].values[1:])
    return pd.Series(on, index=df.index[1:]), nbad


def alpaca_on(df):
    on, nbad = on_series(df["o"].values[1:], df["c"].values[:-1])
    return pd.Series(on, index=df.index[1:]), nbad


def _panel():
    sys.path.insert(0, LH)
    import lh_core as C
    import lh_strat as S
    if not os.path.exists(os.path.join(C.OUT, "panel.npz")):
        C.OUT = LH                       # /tmp/lh was cleaned; the same panel.npz lives in ~/lh_cache
    return S.returns(C.load())


# ------------------------------------------------------------------ check (no book outcomes)
def check():
    R = _raw()
    P = _panel()
    pd_ = pd.DatetimeIndex(P["dates"])
    spy_al = pd.Series(np.r_[np.nan, P["spy_o"][1:] / P["spy_c"][:-1] - 1], index=pd_)
    ry, nby = yahoo_on(R["yahoo_RSP"])
    sy, _ = yahoo_on(R["yahoo_SPY"])
    ra, nba = alpaca_on(R["alpaca_RSP"])
    res = {"rsp_bad_nights": {"yahoo": nby, "alpaca": nba}}
    for name, y, a in (("RSP", ry, ra), ("SPY", sy, spy_al)):
        j = pd.concat([y.rename("y"), a.rename("a")], axis=1, join="inner").dropna()
        j = j[(j.index >= CHK[0]) & (j.index <= CHK[1])]
        d = (j["y"] - j["a"]) * 1e4
        res[f"source_{name}"] = {"nights": int(len(j)), "corr": round(float(j["y"].corr(j["a"])), 4),
                                 "mean_diff_bp": round(float(d.mean()), 3), "pass": bool(j["y"].corr(j["a"]) >= 0.99 and abs(d.mean()) <= 0.5)}
    res["source_check_pass"] = bool(res["source_RSP"]["pass"] and res["source_SPY"]["pass"])
    res["rsp_on_zero_share_by_year_yahoo"] = zero_years(ry.values, ry.index.year)[0]
    res["rsp_on_zero_share_by_year_alpaca"] = zero_years(ra.values, ra.index.year)[0]
    json.dump(res, open(os.path.join(WORK, "check.json"), "w"), indent=1)
    P_(json.dumps(res, indent=1))


# ------------------------------------------------------------------ book series
def period_a(R):
    sys.path.insert(0, HERE)
    import overnight_pre2016 as Y
    A = Y.build()
    X = Y.derive(A)
    dates = pd.DatetimeIndex(A["dates"])
    df = Y.book_series(dates, A["rc"], X["adv"], X["m"], X["cf"], X["ON"], X["spy_on"], A_LO, A_HI, badyr=X["badyr"])
    pos = dates.get_indexer(df.index)
    nxt = dates[pos + 1]
    ry, _ = yahoo_on(R["yahoo_RSP"])
    out = pd.DataFrame({"book": df["picks"].values - COST, "picks": df["picks"].values, "univ": df["univ"].values,
                        "spy": df["spy"].values, "rsp": ry.reindex(nxt).values}, index=df.index)
    return out


def period_b(R, P):
    cf, ON, rc, adv = P["cf"], P["ON"], P["rc"], P["adv20"]
    dates = pd.DatetimeIndex(P["dates"])
    T = cf.shape[0]
    spy_on = np.r_[np.nan, P["spy_o"][1:] / P["spy_c"][:-1] - 1]
    m = np.full_like(cf, np.nan)
    with np.errstate(all="ignore"):
        m[252:] = cf[252 - 21:T - 21] / cf[:T - 252] - 1
    ra, _ = alpaca_on(R["alpaca_RSP"])
    rows = []
    for t in range(260, T - 1):
        if dates[t] > B_HI:
            break
        ok = (rc[t] >= 5) & (adv[t] >= 50e6) & np.isfinite(m[t]) & np.isfinite(cf[t])
        idx = np.where(ok)[0]
        b = u = np.nan
        if len(idx) >= 30:
            pick = idx[np.argsort(-m[t, idx])][:20]
            on, onu = ON[t + 1, pick], ON[t + 1, idx]
            if np.isfinite(on).any():
                b = np.nanmean(on)
            if np.isfinite(onu).any():
                u = np.nanmean(onu)
        rows.append((dates[t], b - COST, b, u, spy_on[t + 1], ra.get(dates[t + 1], np.nan)))
    return pd.DataFrame(rows, columns=["date", "book", "picks", "univ", "spy", "rsp"]).set_index("date")


def _prep(df):
    share, bad = zero_years(df["rsp"].values, df.index.year)
    df = df.assign(drop_year=np.isin(df.index.year, list(bad)))
    return hedge_frame(df), share, sorted(bad)


# ------------------------------------------------------------------ score
def score():
    ck = json.load(open(os.path.join(WORK, "check.json")))
    R = _raw()
    P = _panel()
    fa, za, ba = _prep(period_a(R))
    fb, zb, bb = _prep(period_b(R, P))
    ca, cb = fa[fa["common"]], fb[fb["common"]]
    A, B = evaluate(ca), evaluate(cb)
    P_("POWER (read first): period A MDE %.2f bp (<= 5 required), n %d; period B MDE %.2f bp (report only), n %d"
       % (A["rsp_hedged"]["mde_bp"], A["nights"], B["rsp_hedged"]["mde_bp"], B["nights"]))
    v, crit = verdict(A, B, ck["source_check_pass"])
    res = {"prereg": "docs/studies/overnight_rsp_hedge_prereg.json", "resolutions": RESOLUTIONS, "check": ck,
           "zero_share_by_year": {"A": za, "B": zb}, "dropped_years": {"A": ba, "B": bb},
           "A": A, "B": B, "criteria": crit, "verdict": v}
    info = {}
    for name, f, c in (("A", fa, ca), ("B", fb, cb)):
        fixed = c["book"] - 1.0 * c["rsp"] - RSP_COST
        hr = c["hed_rsp"]
        yr = c.index.year
        info[name] = {
            "unhedged": mean_stat(c["book"].values), "fixed_h1_rsp": mean_stat(fixed.values),
            "fixed_h_gap_bp": round(float((hr - fixed).mean() * 1e4), 3),
            "rsp_hedged_10bp": mean_stat((c["book"] - c["h_rsp"] * c["rsp"] - RSP_COST_INFO * c["h_rsp"].abs()).values),
            "beta_rsp_hedged_to_spy": round(float(np.polyfit(c["spy"], hr, 1)[0]), 3),
            "tails": {"unhedged": tails(c["book"].values), "spy_hedged": tails(c["hed_spy"].values), "rsp_hedged": tails(hr.values)},
            "per_year_bp": {int(y): {"n": int((yr == y).sum()),
                                     "unhedged": round(float(c["book"][yr == y].mean() * 1e4), 2),
                                     "spy_hedged": round(float(c["hed_spy"][yr == y].mean() * 1e4), 2),
                                     "rsp_hedged": round(float(hr[yr == y].mean() * 1e4), 2),
                                     "h_rsp_mean": round(float(c["h_rsp"][yr == y].mean()), 3),
                                     "realised_beta_rsp_hedged_to_rsp": round(float(np.polyfit(c["rsp"][yr == y], hr[yr == y], 1)[0]), 3)}
                            for y in np.unique(yr) if (yr == y).sum() >= 30},
            "nights_total_rows": int(len(f)), "nights_not_common": int((~f["common"]).sum()),
        }
    res["information"] = info
    json.dump(res, open(os.path.join(WORK, "result.json"), "w"), indent=1, default=str)
    P_(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"fetch": fetch, "check": check, "score": score}.get(cmd, lambda: P_(__doc__))()
