#!/usr/bin/env python3
"""One-parameter changes to the live overnight book, on the 10-year panel.

Pre-registered: docs/studies/overnight_variants_prereg.json (dcb9856, before any result).
Base = the live config: top-20 12-1 momentum, raw close >= $5, ADV20 >= $50M, equal weight, drop picks
down more than 1% on the day. Each variant changes one thing. Statistic: paired nightly difference vs base.

Run on the mini: .venv/bin/python tools/studies/overnight_variants.py
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

LH = os.path.expanduser("~/lh_cache")
sys.path.insert(0, LH)
import lh_core as C  # noqa: E402
import lh_strat as S  # noqa: E402

COST = 4e-4
IS_END = pd.Timestamp("2021-12-31")


def main():
    P = S.returns(C.load())
    cf, ON, ID, R = P["cf"], P["ON"], P["ID"], P["R"]
    rc, adv, dates = P["rc"], P["adv20"], P["dates"]
    T, N = cf.shape
    spy_id = P["spy_c"] / P["spy_o"] - 1
    vol60 = pd.DataFrame(R).rolling(60, min_periods=40).std().values

    def mom(look, skip):
        m = np.full_like(cf, np.nan)
        with np.errstate(all="ignore"):
            m[look:] = cf[look - skip:T - skip] / cf[:T - look] - 1
        return m
    M = {(252, 21): mom(252, 21), (126, 21): mom(126, 21), (189, 21): mom(189, 21),
         (63, 21): mom(63, 21), (252, 1): mom(252, 1)}

    base = dict(n=20, look=(252, 21), px=5.0, adv=50e6, w="eq", f=-0.01, spy=False)
    V = {"BASE": {}, "N5": dict(n=5), "N10": dict(n=10), "N30": dict(n=30), "N40": dict(n=40),
         "L6": dict(look=(126, 21)), "L9": dict(look=(189, 21)), "L3": dict(look=(63, 21)), "L12_0": dict(look=(252, 1)),
         "ADV20M": dict(adv=20e6), "ADV100M": dict(adv=100e6), "ADV250M": dict(adv=250e6), "PX10": dict(px=10.0),
         "IVOL": dict(w="ivol"), "F_OFF": dict(f=None), "F_M2": dict(f=-0.02), "F_0": dict(f=0.0), "SPY_SKIP": dict(spy=True)}

    t0 = 260
    nights = list(range(t0, T - 1))
    res, held = {}, {}
    for name, ch in V.items():
        cfg = {**base, **ch}
        m = M[cfg["look"]]
        out, hn = np.full(len(nights), np.nan), np.zeros(len(nights))
        for k, t in enumerate(nights):
            ok = (rc[t] >= cfg["px"]) & (adv[t] >= cfg["adv"]) & np.isfinite(m[t]) & np.isfinite(cf[t])
            idx = np.where(ok)[0]
            if len(idx) < 30:
                continue
            pick = idx[np.argsort(-m[t, idx])][:cfg["n"]]
            if cfg["spy"] and np.isfinite(spy_id[t]) and spy_id[t] < -0.01:
                out[k] = 0.0
                continue
            if cfg["f"] is not None:
                pick = pick[np.nan_to_num(ID[t, pick], nan=0.0) >= cfg["f"]]
            on = ON[t + 1, pick]
            good = np.isfinite(on)
            pick, on = pick[good], on[good]
            if len(pick) == 0:
                out[k] = 0.0
                continue
            if cfg["w"] == "ivol":
                iv = 1 / np.where(np.isfinite(vol60[t, pick]) & (vol60[t, pick] > 0), vol60[t, pick], np.nan)
                iv = np.where(np.isfinite(iv), iv, np.nanmean(iv) if np.isfinite(iv).any() else 1.0)
                w = iv / iv.sum()
            else:
                w = np.full(len(pick), 1 / len(pick))
            out[k] = float((w * on).sum())
            hn[k] = len(pick)
        res[name], held[name] = out, hn
        print(f"  {name} done", file=sys.stderr, flush=True)

    d = dates[np.array(nights)]
    is_m, oos_m = np.array(d <= IS_END), np.array(d > IS_END)
    b = res["BASE"]

    def tstat(x):
        x = x[np.isfinite(x)]
        return x.mean() / (x.std(ddof=1) / math.sqrt(len(x))) if len(x) > 2 else float("nan"), len(x)

    print(f"nights IS {is_m.sum()} ({d[is_m][0].date()}..{d[is_m][-1].date()}), OOS {oos_m.sum()} "
          f"({d[oos_m][0].date()}..{d[oos_m][-1].date()})\n")
    print(f"{'variant':9} {'names':>5} {'IS gross':>9} {'OOS gross':>10} {'OOS net':>8} {'green':>6} {'worst':>7} "
          f"{'IS diff (t)':>14} {'OOS diff (t)':>15}  verdict")
    for name in V:
        x = res[name] * 1e4
        g_is, g_oos = np.nanmean(x[is_m]), np.nanmean(x[oos_m])
        dep = np.nanmean(np.where(held[name][oos_m] > 0, 1, 0))
        net_oos = g_oos - COST * 1e4 * dep
        green = np.nanmean(x[oos_m] > 0)
        worst = np.nanmin(x[oos_m])
        if name == "BASE":
            print(f"{name:9} {held[name][oos_m].mean():>5.1f} {g_is:>+9.1f} {g_oos:>+10.1f} {net_oos:>+8.1f} {green:>6.0%} "
                  f"{worst:>+7.0f} {'—':>14} {'—':>15}")
            continue
        diff = (res[name] - b) * 1e4
        ti, _ = tstat(diff[is_m])
        to, _ = tstat(diff[oos_m])
        mi, mo = np.nanmean(diff[is_m]), np.nanmean(diff[oos_m])
        verdict = ("PASS" if mo >= 2 and to >= 3.0 and mi > 0 and ti >= 1.5 else
                   "lean" if to >= 2.0 and mo > 0 and mi > 0 else
                   "worse" if to <= -2.0 else "no")
        print(f"{name:9} {held[name][oos_m].mean():>5.1f} {g_is:>+9.1f} {g_oos:>+10.1f} {net_oos:>+8.1f} {green:>6.0%} "
              f"{worst:>+7.0f} {mi:>+7.1f} ({ti:>+4.1f}) {mo:>+8.1f} ({to:>+4.1f})  {verdict}")


if __name__ == "__main__":
    main()
