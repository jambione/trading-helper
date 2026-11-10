#!/usr/bin/env python3
"""ml_rank.py - a learned ranking instead of a hand-written entry rule (operator 11/10, idea 1).

One gradient-boosted model reads ~25 causal bar features for every name-minute and predicts the next HOLD minutes' return
relative to the other names at the same minute. It is trained on the first block of warehouse days, its trade threshold is
set on a second block, and it is scored by tools/studies/harness.py on a third block it has never seen. The harness
hold-out (the latest 20 sessions) is not touched at all.

    .venv/bin/python tools/studies/ml_rank.py [--warehouse DIR] [--hold 15] [--top-n 400] [--price-lo 10 --price-hi 500]
"""
from __future__ import annotations

import argparse
import sys
import time
import warnings
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "signals")]
import harness as H
from _lib import _ffill, day_change, percent_r, roll, vol_ratio

NAMES = ["ret1", "ret3", "ret5", "ret15", "ret30", "chg", "gap", "pos_day", "wr21", "wr112", "volr", "cumvol_rel", "bar_rng",
         "rng15", "imb5", "vwap_dist", "minute", "mkt5", "mkt15", "rel5", "rel15", "prev_ret", "prev_rng", "log_px", "log_dv"]


def features(day: dict, prev: dict) -> np.ndarray:
    """(n, 390, F). Column m uses bars at or before m (and the prior session) only."""
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        c = _ffill(day["c"])
        n, T = c.shape
        o, h, l, v = day["o"], np.where(np.isnan(day["h"]), c, day["h"]), np.where(np.isnan(day["l"]), c, day["l"]), np.nan_to_num(day["v"])
        pc_all = _ffill(prev["c"])
        pc = pc_all[:, -1]

        def ret(k):
            r = np.full(c.shape, np.nan)
            r[:, k:] = (c[:, k:] / c[:, :-k] - 1) * 1e4
            return r
        r1, r3, r5, r15, r30 = ret(1), ret(3), ret(5), ret(15), ret(30)
        chg = day_change(day, prev) * 100.0
        first_o = np.full(n, np.nan)
        for m in range(min(T, 30)):
            first_o = np.where(np.isnan(first_o), o[:, m], first_o)
        gap = np.repeat(((first_o / pc - 1) * 1e4)[:, None], T, 1)
        hh, ll = np.fmax.accumulate(h, axis=1), np.fmin.accumulate(l, axis=1)
        pos_day = np.where(hh > ll, (c - ll) / (hh - ll), np.nan)
        cv = np.cumsum(v, 1)
        pv = np.cumsum(np.nan_to_num(prev["v"]), 1)
        cumvol_rel = np.where(pv > 0, cv / pv, np.nan)
        bar_rng = (h - l) / c * 1e4
        rng15 = (roll(h, 15, np.nanmax) - roll(l, 15, np.nanmin)) / c * 1e4
        sv = v * np.sign(np.nan_to_num(c - np.where(np.isnan(o), c, o)))
        z = np.zeros((n, 1))
        cs, cvv = np.concatenate([z, np.cumsum(sv, 1)], 1), np.concatenate([z, cv], 1)
        imb5 = np.full(c.shape, np.nan)
        imb5[:, 4:] = (cs[:, 5:] - cs[:, :-5]) / (cvv[:, 5:] - cvv[:, :-5])
        vwap = np.cumsum(v * (h + l + c) / 3.0, 1) / np.where(cv > 0, cv, np.nan)
        vwap_dist = (c / vwap - 1) * 1e4
        minute = np.repeat(np.arange(T, dtype=float)[None, :], n, 0)
        live = ~np.isnan(day["c"])
        mkt5 = np.repeat(np.nanmean(np.where(live, r5, np.nan), 0)[None, :], n, 0)
        mkt15 = np.repeat(np.nanmean(np.where(live, r15, np.nan), 0)[None, :], n, 0)
        p_first = np.full(n, np.nan)
        for m in range(prev["o"].shape[1]):
            p_first = np.where(np.isnan(p_first), prev["o"][:, m], p_first)
        prev_ret = np.repeat(((pc / p_first - 1) * 1e4)[:, None], T, 1)
        prev_rng = np.repeat(((np.nanmax(prev["h"], 1) - np.nanmin(prev["l"], 1)) / pc * 1e4)[:, None], T, 1)
        log_px = np.log(c)
        log_dv = np.repeat(np.log(np.nansum(prev["c"] * prev["v"], 1) + 1.0)[:, None], T, 1)
        F = np.stack([r1, r3, r5, r15, r30, chg, gap, pos_day, percent_r(day, prev, 21), percent_r(day, prev, 112), vol_ratio(day),
                      cumvol_rel, bar_rng, rng15, imb5, vwap_dist, minute, mkt5, mkt15, r5 - mkt5, r15 - mkt15, prev_ret, prev_rng,
                      log_px, log_dv], axis=-1)
        F[~live] = np.nan
    return F.astype(np.float32)


def day_pairs(files, cfg):
    prev_raw = H.load_day(files[0])
    for f in files[1:]:
        raw = H.load_day(f)
        day, prev = H.universe(raw, prev_raw, cfg["top_n"], cfg["price_lo"], cfg["price_hi"])
        prev_raw = raw
        if day["syms"]:
            yield f.stem, day, prev


def rows(files, cfg, hold, step=5):
    X, y = [], []
    for _d, day, prev in day_pairs(files, cfg):
        F = features(day, prev)
        fwd = H.forward_returns(day, hold)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            rel = fwd - np.nanmean(fwd, axis=0, keepdims=True)
        cols = np.arange(20, H.NBAR - hold - 1, step)
        ok = ~np.isnan(rel[:, cols]) & ~np.isnan(F[:, cols, 0])
        X.append(F[:, cols][ok])
        y.append(np.clip(rel[:, cols][ok], -300, 300))
    return np.concatenate(X), np.concatenate(y)


def main(argv=None) -> int:
    from sklearn.ensemble import HistGradientBoostingRegressor
    ap = argparse.ArgumentParser()
    ap.add_argument("--warehouse", default=str(H.WH))
    ap.add_argument("--hold", type=int, default=15)
    ap.add_argument("--cent-spread", type=float, default=0.0)
    ap.add_argument("--final", action="store_true", help="ONE read of the top 0.2%% rule on the harness hold-out sessions")
    for k in ("top_n", "price_lo", "price_hi", "cost_bp", "stress_bp", "holdout"):
        ap.add_argument("--" + k.replace("_", "-"), type=type(H.DEFAULTS[k]), default=H.DEFAULTS[k])
    a = ap.parse_args(argv)
    cfg = {**H.DEFAULTS, **{k: getattr(a, k) for k in ("top_n", "price_lo", "price_hi", "cost_bp", "stress_bp", "holdout")},
           "cent_spread": a.cent_spread}
    files = sorted(Path(a.warehouse).glob("*.npz"))
    dev = files[:-cfg["holdout"]]
    n = len(dev) - 1
    n_tr, n_cal = int(n * 0.5), int(n * 0.15)
    tr, cal, te = dev[:n_tr + 1], dev[n_tr:n_tr + n_cal + 1], dev[n_tr + n_cal:]
    t0 = time.time()
    Xtr, ytr = rows(tr, cfg, a.hold)
    print(f"train {tr[1].stem}..{tr[-1].stem}: {len(ytr):,} rows; calibrate {cal[1].stem}..{cal[-1].stem}; "
          f"test {te[1].stem}..{te[-1].stem} ({len(te) - 1} days)  [{time.time() - t0:.0f} s]", flush=True)
    model = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=2000,
                                          l2_regularization=1.0, random_state=1)
    model.fit(Xtr, ytr)
    Xc, yc = rows(cal, cfg, a.hold)
    pc = model.predict(Xc)
    print(f"fit done [{time.time() - t0:.0f} s]; calibration-block rank correlation of prediction with outcome "
          f"{np.corrcoef(np.argsort(np.argsort(pc)), np.argsort(np.argsort(yc)))[0, 1]:+.4f}", flush=True)
    try:
        from sklearn.inspection import permutation_importance
        idx = np.random.default_rng(1).choice(len(yc), size=min(200_000, len(yc)), replace=False)
        imp = permutation_importance(model, Xc[idx], yc[idx], n_repeats=2, random_state=1).importances_mean
        top = np.argsort(-imp)[:6]
        print("features the model leans on most: " + ", ".join(f"{NAMES[i]}" for i in top), flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"(importance skipped: {e})")
    if a.final:
        te = files[len(dev) - 1:]
        print(f"FINAL read on the hold-out {te[1].stem}..{te[-1].stem} ({len(te) - 1} days): same model, same threshold rule", flush=True)
    picked = []
    for pct in ((99.8,) if a.final else (99.0, 99.8)):
        thr = float(np.percentile(pc, pct))

        def fn(day, prev, thr=thr):
            F = features(day, prev)
            p = np.full(F.shape[:2], -np.inf)
            ok = ~np.isnan(F[:, :, 0])
            p[ok] = model.predict(F[ok])
            s = p >= thr
            s[:, :20] = False
            if pct == 99.8 and s.any():
                picked.append(np.concatenate([F[s], np.nonzero(s)[1][:, None].astype(np.float32)], axis=1))
            return s
        picked.clear()
        r = H.run(fn, a.hold, te, {**cfg, "family": 1 if a.final else 2, "min_days": 15}, check=not a.final)
        print(H.report(f"ml_rank top {100 - pct:.1f}%", a.hold, r, cfg, a.final), flush=True)
    if picked:
        P = np.concatenate(picked)
        mins = P[:, -1]
        show = ["chg", "ret5", "ret15", "pos_day", "wr21", "wr112", "volr", "vwap_dist", "gap", "log_px"]
        print("what the top 0.2% picks look like (median): " + ", ".join(f"{k} {np.nanmedian(P[:, NAMES.index(k)]):.1f}" for k in show))
        print("when: " + ", ".join(f"{lab} {100 * np.mean((mins >= lo) & (mins < hi)):.0f}%" for lab, lo, hi in
                                   (("09:50-10:30", 20, 60), ("10:30-12:00", 60, 150), ("12:00-14:00", 150, 270), ("14:00-close", 270, 390))))
    print(f"[{time.time() - t0:.0f} s]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
