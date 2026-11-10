#!/usr/bin/env python3
"""day_pick.py - "find the 5 names that make the most from the morning to the close" (operator 11/10).

At 10:00 each day, pick 5 names, buy the 10:01 open, sell the 15:59 open. Pickers: a gradient-boosted model trained on
earlier days (decisions at 09:45/10:00/10:15/10:30, same ~25 causal features as ml_rank.py), and four plain rules. Every
picker is scored on days the model never saw, against the average universe name that day (lift) and after cost.
The "oracle" row is the best 5 in hindsight: the size of the prize, not something tradeable.

    .venv/bin/python tools/studies/day_pick.py [--warehouse DIR --top-n 400 --price-lo 10 --price-hi 500 --cost-bp 6]
"""
from __future__ import annotations

import argparse
import math
import sys
import warnings
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "signals")]
import harness as H
import ml_rank as M

DECIDE, EXIT, K = 30, 389, 5          # 10:00 bar close, 15:59 bar open, names per day
TRAIN_AT = (15, 30, 45, 60)


def to_close(day: dict, m: int) -> np.ndarray:
    with np.errstate(all="ignore"):
        return (day["o"][:, EXIT] / day["o"][:, m + 1] - 1.0) * 1e4


def stat(x: list[float]) -> str:
    v = np.array([a for a in x if not math.isnan(a)])
    if len(v) < 3:
        return "n/a"
    t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v)))
    h = len(v) // 2
    return f"{v.mean():+7.1f} bp/day  t {t:+.2f}  halves {v[:h].mean():+6.1f} / {v[h:].mean():+6.1f}"


def main(argv=None) -> int:
    from sklearn.ensemble import HistGradientBoostingRegressor
    ap = argparse.ArgumentParser()
    ap.add_argument("--warehouse", default=str(H.WH))
    ap.add_argument("--cent-spread", type=float, default=0.0)
    for k in ("top_n", "price_lo", "price_hi", "cost_bp", "holdout"):
        ap.add_argument("--" + k.replace("_", "-"), type=type(H.DEFAULTS[k]), default=H.DEFAULTS[k])
    a = ap.parse_args(argv)
    cfg = {**H.DEFAULTS, **{k: getattr(a, k) for k in ("top_n", "price_lo", "price_hi", "cost_bp", "holdout")}}
    files = sorted(Path(a.warehouse).glob("*.npz"))
    dev = files[:-cfg["holdout"]]
    n_tr = int((len(dev) - 1) * 0.55)
    X, y = [], []
    for _d, day, prev in M.day_pairs(dev[:n_tr + 1], cfg):
        F = M.features(day, prev)
        for m in TRAIN_AT:
            r = to_close(day, m)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                rel = r - np.nanmean(r)
            ok = ~np.isnan(rel) & ~np.isnan(F[:, m, 0])
            X.append(F[ok, m])
            y.append(np.clip(rel[ok], -1500, 1500))
    X, y = np.concatenate(X), np.concatenate(y)
    model = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
                                          l2_regularization=1.0, random_state=1).fit(X, y)
    ix = {n: i for i, n in enumerate(M.NAMES)}
    pickers = {"model": lambda F: model.predict(np.nan_to_num(F, nan=0.0)) if False else model.predict(F),
               "most up on the day": lambda F: F[:, ix["chg"]],
               "most up since the open": lambda F: F[:, ix["ret30"]],
               "highest relative volume": lambda F: F[:, ix["cumvol_rel"]],
               "most down on the day": lambda F: -F[:, ix["chg"]]}
    out = {k: {"gross": [], "lift": [], "net": []} for k in [*pickers, "oracle (hindsight)"]}
    uni_mean, days = [], []
    for d, day, prev in M.day_pairs(dev[n_tr:], cfg):
        F = M.features(day, prev)[:, DECIDE]
        r = to_close(day, DECIDE)
        px = day["o"][:, DECIDE + 1]
        ok = ~np.isnan(r) & ~np.isnan(F[:, 0])
        if ok.sum() < 50:
            continue
        F, r, px = F[ok], r[ok], px[ok]
        cost = np.maximum(cfg["cost_bp"], a.cent_spread * 100.0 / px)
        days.append(d)
        uni_mean.append(float(r.mean()))
        for name, score in {**pickers, "oracle (hindsight)": lambda F, r=r: r}.items():
            s = np.asarray(score(F), dtype=float)
            top = np.argsort(-np.where(np.isnan(s), -np.inf, s))[:K]
            g = float(r[top].mean())
            out[name]["gross"].append(g)
            out[name]["lift"].append(g - float(r.mean()))
            out[name]["net"].append(float((r[top] - cost[top]).mean()))
    print(f"train {dev[1].stem}..{dev[n_tr].stem} ({len(y):,} rows); test {days[0]}..{days[-1]} ({len(days)} days), "
          f"{K} names/day, buy 10:01 sell 15:59; average universe name {np.mean(uni_mean):+.1f} bp/day")
    for name, o in out.items():
        print(f"{name:<26} lift vs average name {stat(o['lift'])}\n{'':<26} net after cost        {stat(o['net'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
