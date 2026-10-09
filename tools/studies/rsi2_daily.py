#!/usr/bin/env python3
"""rsi2_daily.py — pre-registered in docs/studies/rsi2_daily_prereg.json. Nothing here may change it.

Connors RSI-2 on DAILY bars as a day-trading name picker, on the lh_cache liquid panel (SIP daily, adjustment=all).
Signal at the close of p = t-1: cf > SMA200 (>= 200 finite closes) AND cf < SMA5 AND RSI2 (Wilder, period 2) < 10.
Universe: rc >= $10, adv20 >= $20M, cf > SMA200, finite o and c on t. Trade: open(t) -> close(t). Control: universe minus
signal names. Per day: mean signal ID - mean control ID; t = overnight_rsp_hedge.mean_stat; halves 2016-2020 / 2021-2026.
USAGE (mini, after hours): .venv/bin/python tools/studies/rsi2_daily.py   -> ai_reports/rsi2_daily/report.md
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.getcwd()
sys.path[:0] = [os.path.join(ROOT, "tools", "studies"), os.path.join(ROOT, "tools"), ROOT]
OUT = os.path.join(ROOT, "ai_reports", "rsi2_daily")
COST = 10e-4
MIN_CTL = 30


def wilder_rsi(cf: pd.DataFrame, period: int = 2) -> pd.DataFrame:
    d = cf.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn)
    rsi = rsi.where(dn != 0, 100.0).where(~((up == 0) & (dn != 0)), 0.0)
    return rsi.where(cf.notna())


def load():
    sys.path.insert(0, os.path.expanduser("~/lh_cache"))
    import lh_core as C
    if not os.path.exists(os.path.join(C.OUT, "panel.npz")):
        C.OUT = os.path.expanduser("~/lh_cache")
    return C.load()


def build(P):
    c, o, cf, rc, adv = P["c"], P["o"], P["cf"], P["rc"], P["adv20"]
    CF = pd.DataFrame(cf)
    nfin = pd.DataFrame(np.isfinite(c).astype(float)).rolling(200, min_periods=200).sum().values
    sma200 = CF.rolling(200, min_periods=200).mean().values
    sma5 = CF.rolling(5, min_periods=5).mean().values
    rsi = wilder_rsi(CF).values
    ok200 = nfin >= 200
    above = ok200 & (cf > sma200)
    below5 = cf < sma5
    sig10 = above & below5 & (rsi < 10)
    sig5 = above & below5 & (rsi < 5)
    pull = above & below5 & (rsi >= 10) & (rsi < 30)
    uni_p = (rc >= 10) & (adv >= 20e6) & above
    return {"sig10": sig10, "sig5": sig5, "pull": pull, "uni_p": uni_p, "rsi": rsi}


def daily_rows(P, B, sig_key="sig10", ctl_key=None):
    c, o, cf = P["c"], P["o"], P["cf"]
    dates = pd.DatetimeIndex(P["dates"])
    T = c.shape[0]
    ID = c / o - 1
    gap = np.full_like(c, np.nan)
    gap[1:] = o[1:] / c[:-1] - 1
    cc = np.full_like(c, np.nan)
    cc[1:] = c[1:] / c[:-1] - 1
    r2 = np.full_like(cf, np.nan)
    r2[2:] = cf[2:] / cf[:-2] - 1
    spy = P["spy_c"] / P["spy_o"] - 1
    rows, names = [], []
    for t in range(201, T):
        p = t - 1
        live = np.isfinite(o[t]) & np.isfinite(c[t]) & (o[t] > 0)
        uni = B["uni_p"][p] & live
        sig = B[sig_key][p] & uni
        ctl = (B[ctl_key][p] & uni & ~sig) if ctl_key else (uni & ~sig)
        ns, nc = int(sig.sum()), int(ctl.sum())
        if ns < 1 or nc < MIN_CTL:
            continue
        si, ci = np.where(sig)[0], np.where(ctl)[0]
        row = {"date": dates[t], "n_sig": ns, "n_ctl": nc,
               "diff": float(np.nanmean(ID[t, si]) - np.nanmean(ID[t, ci])), "sig_id": float(np.nanmean(ID[t, si])),
               "gap_diff": float(np.nanmean(gap[t, si]) - np.nanmean(gap[t, ci])),
               "cc_diff": float(np.nanmean(cc[t, si]) - np.nanmean(cc[t, ci])),
               "gapdown_share": float(np.mean(gap[t, si] < -0.01)), "spy": float(spy[t]),
               "spr_sig": float(np.nanmean(P["spr_ar"][p, si])), "spr_ctl": float(np.nanmean(P["spr_ar"][p, ci])),
               "vol_sig": float(np.nanstd(ID[max(0, t - 20):t, si])), "vol_ctl": float(np.nanstd(ID[max(0, t - 20):t, ci]))}
        # decile-matched control on the 2-day return (computed among the day's universe)
        u_idx = np.where(uni)[0]
        rr = r2[p, u_idx]
        if np.isfinite(rr).sum() >= 50:
            qs = np.nanquantile(rr, np.linspace(0, 1, 11))
            dec = np.clip(np.searchsorted(qs, r2[p], side="right") - 1, 0, 9)
            vals = []
            for k in si:
                pool = ci[(dec[ci] == dec[k])]
                if len(pool) >= 5:
                    vals.append(ID[t, k] - np.nanmean(ID[t, pool]))
            row["dec_diff"] = float(np.mean(vals)) if vals else np.nan
        rows.append(row)
        ctl_mean = np.nanmean(ID[t, ci])
        names += [(int(k), float(ID[t, k] - ctl_mean), t) for k in si]
    return pd.DataFrame(rows).set_index("date"), names


def connors_swing(P, B):
    """Information: buy at the close of a sig10 day, sell at the first close above SMA5 (max 10 days); per-event return."""
    cf, c = P["cf"], P["c"]
    CF = pd.DataFrame(cf)
    sma5 = CF.rolling(5, min_periods=5).mean().values
    T = c.shape[0]
    ev = []
    pp, kk = np.where(B["sig10"] & B["uni_p"])
    for p, k in zip(pp, kk):
        if p + 1 >= T:
            continue
        for j in range(p + 1, min(T, p + 11)):
            if np.isfinite(c[j]) and (c[j] > sma5[j] or j == min(T, p + 11) - 1):
                ev.append((pd.Timestamp(P["dates"][p]).year, c[j] / c[p] - 1))
                break
    return ev


def main():
    import overnight_rsp_hedge as H
    os.makedirs(OUT, exist_ok=True)
    P = load()
    B = build(P)
    D, names = daily_rows(P, B)
    halves = {"2016_2020": D.index.year <= 2020, "2021_2026": D.index.year >= 2021}
    L = ["# Daily RSI-2 name picker (prereg docs/studies/rsi2_daily_prereg.json)", "",
         f"signal days {len(D)} ({D.index[0].date()}..{D.index[-1].date()}); median signal names/day {int(D['n_sig'].median())}, "
         f"median control names/day {int(D['n_ctl'].median())}", ""]
    ok = True
    res = {}
    for h, m in halves.items():
        g = D[m]
        rel, ab = H.mean_stat(g["diff"].values), H.mean_stat((g["sig_id"] - COST).values)
        ab20 = H.mean_stat((g["sig_id"] - 2 * COST).values)
        good = len(g) >= 200 and rel["mean_bp"] >= 5 and rel["t"] >= 2 and ab["mean_bp"] > 0
        ok &= good
        res[h] = {"days": len(g), "rel": rel, "abs10": ab, "abs20": ab20}
        L.append(f"- {h}: days {len(g)} | signal minus control {rel['mean_bp']:+.2f} bp (t {rel['t']:+.2f}) | signal net 10 bp "
                 f"{ab['mean_bp']:+.2f} (t {ab['t']:+.2f}) | net 20 bp {ab20['mean_bp']:+.2f}")
    rel_ok = all(res[h]["days"] >= 200 and res[h]["rel"]["mean_bp"] >= 5 and res[h]["rel"]["t"] >= 2 for h in halves)
    verdict = "PASS" if ok else ("RELATIVE-ONLY (FAIL as a picker)" if rel_ok else "FAIL")
    L += ["", f"**PRIMARY: {verdict}**", "", "## Information only (per half, same statistic)"]
    for lab, col in (("close(p)->open(t) gap, signal minus control", "gap_diff"), ("close(p)->close(t), signal minus control", "cc_diff"),
                     ("2-day-return decile-matched control", "dec_diff")):
        L.append(f"- {lab}: " + "; ".join(f"{h} {H.mean_stat(D[m][col].values)['mean_bp']:+.2f} bp (t {H.mean_stat(D[m][col].values)['t']:+.2f})"
                                           for h, m in halves.items()))
    for lab, kw in (("RSI2 < 5", {"sig_key": "sig5"}), ("control (a): pullbacks with RSI2 in [10, 30)", {"ctl_key": "pull"})):
        Dx, _ = daily_rows(P, B, **kw)
        L.append(f"- {lab}: " + "; ".join(f"{h} {H.mean_stat(Dx[(Dx.index.year <= 2020) if h == '2016_2020' else (Dx.index.year >= 2021)]['diff'].values)['mean_bp']:+.2f} bp "
                                           f"(t {H.mean_stat(Dx[(Dx.index.year <= 2020) if h == '2016_2020' else (Dx.index.year >= 2021)]['diff'].values)['t']:+.2f})"
                                           for h in halves))
    for h, m in halves.items():
        g = D[m].dropna(subset=["spy"])
        X = np.vstack([np.ones(len(g)), g["spy"].values]).T
        coef = np.linalg.lstsq(X, g["diff"].values, rcond=None)[0]
        L.append(f"- beta {h}: alpha {coef[0] * 1e4:+.2f} bp, beta to SPY open->close {coef[1]:+.2f}; vol signal {g['vol_sig'].mean() * 1e4:.0f} "
                 f"vs control {g['vol_ctl'].mean() * 1e4:.0f} bp; spread proxy signal {g['spr_sig'].mean() * 1e4:.1f} vs control {g['spr_ctl'].mean() * 1e4:.1f} bp; "
                 f"gap-down > 1% share {g['gapdown_share'].mean():.0%}")
        top3 = g["diff"].nlargest(3).index
        L.append(f"- drop top 3 days {h}: {H.mean_stat(g.drop(top3)['diff'].values)['mean_bp']:+.2f} bp (t {H.mean_stat(g.drop(top3)['diff'].values)['t']:+.2f})")
        terc = pd.qcut(g["n_sig"].rank(method="first"), 3, labels=["few", "mid", "many"])
        L.append(f"- by signal-count tercile {h}: " + ", ".join(f"{k} {H.mean_stat(g[terc == k]['diff'].values)['mean_bp']:+.2f}" for k in ("few", "mid", "many")))
    nm = pd.DataFrame(names, columns=["k", "x", "t"])
    top5 = nm.groupby("k")["x"].sum().nlargest(5).index
    keep = nm[~nm["k"].isin(top5)]
    L.append(f"- drop top 5 names (all years): per-event mean {keep['x'].mean() * 1e4:+.2f} bp vs all {nm['x'].mean() * 1e4:+.2f} bp "
             f"({', '.join(str(P['syms'][k]) for k in top5)})")
    sw = connors_swing(P, B)
    for h, f in (("2016_2020", lambda y: y <= 2020), ("2021_2026", lambda y: y >= 2021)):
        x = [r for y, r in sw if f(y)]
        L.append(f"- Connors swing (close -> first close above SMA5, max 10 days) {h}: n {len(x)}, mean {np.mean(x) * 1e4:+.1f} bp gross")
    open(os.path.join(OUT, "report.md"), "w").write("\n".join(L) + "\n")
    json.dump({"verdict": verdict, "halves": res}, open(os.path.join(OUT, "result.json"), "w"), indent=1, default=str)
    print("\n".join(L))


if __name__ == "__main__":
    main()
