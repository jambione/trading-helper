#!/usr/bin/env python3
"""reversal_map.py - where does "buy weakness that turns" pay, and does it behave like a real effect? (operator 11/10)

Not a rule test: a MAP. For every name-minute (every 5th minute) on the development days it measures the forward return
relative to the other names at the same minute, and averages it by how far the name is from its session VWAP, measured
in units of the name's own prior-day range (so a $200 large cap and a $5 mover are on one scale). A real effect should
show a DOSE-RESPONSE (a deeper stretch gives a bigger bounce) and should hold in both halves. Then the same map is cut by
whether the bar has turned green, by volume, time of day, what the market is doing, and name size.

Stats are by day: each day gives one mean per cell, the t is across days, halves are by date.

    .venv/bin/python tools/studies/reversal_map.py [--hold 60] [--warehouse DIR --top-n 400 --price-lo 10 --price-hi 500]
"""
from __future__ import annotations

import argparse
import math
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "signals")]
import harness as H  # noqa: E402
import ml_rank as M  # noqa: E402
from _lib import vol_ratio  # noqa: E402
from entry_ideas import _ctx  # noqa: E402

DEPTH = [(-9.0, -0.5, "0.5+ below"), (-0.5, -0.3, "0.3-0.5 below"), (-0.3, -0.2, "0.2-0.3 below"), (-0.2, -0.1, "0.1-0.2 below"),
         (-0.1, 0.1, "near VWAP"), (0.1, 0.2, "0.1-0.2 above"), (0.2, 0.3, "0.2-0.3 above"), (0.3, 0.5, "0.3-0.5 above"), (0.5, 9.0, "0.5+ above")]
STEP = 5


def line(label: str, per_day: dict[str, float], days: list[str], n: int) -> str:
    v = np.array([per_day[d] for d in days if d in per_day])
    if len(v) < 10:
        return f"  {label:<26}{n:>9}   too few days ({len(v)})"
    t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v)))
    h = len(v) // 2
    return f"  {label:<26}{n:>9}{v.mean():>+9.2f}{t:>+7.2f}{v[:h].mean():>+9.2f} /{v[h:].mean():>+7.2f}   {len(v)} days"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--warehouse", default=str(H.WH))
    ap.add_argument("--hold", type=int, default=60)
    for k in ("top_n", "price_lo", "price_hi", "holdout"):
        ap.add_argument("--" + k.replace("_", "-"), type=type(H.DEFAULTS[k]), default=H.DEFAULTS[k])
    a = ap.parse_args(argv)
    cfg = {**H.DEFAULTS, **{k: getattr(a, k) for k in ("top_n", "price_lo", "price_hi", "holdout")}}
    files = sorted(Path(a.warehouse).glob("*.npz"))
    dev = files[:-cfg["holdout"]] if cfg["holdout"] > 0 else files
    cells: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))   # table -> row -> day -> mean
    counts: dict[tuple, int] = defaultdict(int)
    days = []
    for d, day, prev in M.day_pairs(dev, cfg):
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            x = _ctx(day, prev)
            fwd = H.forward_returns(day, a.hold)
            rel = fwd - np.nanmean(fwd, axis=0, keepdims=True)
            z = x["vwd"] / x["prng"]
            vr = vol_ratio(day)
        cols = np.arange(20, H.NBAR - a.hold - 1, STEP)
        rel, z, vr = rel[:, cols], z[:, cols], vr[:, cols]
        green, mkt = x["green"][:, cols], np.repeat(x["mkt15"][:, cols], rel.shape[0], 0)
        ok = ~np.isnan(rel) & ~np.isnan(z) & x["live"][:, cols]
        minute = np.repeat(cols[None, :], rel.shape[0], 0)
        rank = np.repeat(np.arange(rel.shape[0])[:, None], rel.shape[1], 1)            # rows are ordered by prior dollar volume
        days.append(d)

        def put(table, row, mask):
            m = mask & ok
            if m.sum() >= 5:
                cells[table][row][d] = float(rel[m].mean())
                counts[(table, row)] += int(m.sum())
        for lo, hi, lab in DEPTH:
            put("A. distance from VWAP (in prior-day ranges), all bars", lab, (z >= lo) & (z < hi))
        deep = z < -0.3
        for lo, hi, lab in DEPTH[:4]:
            b = (z >= lo) & (z < hi)
            put("B. below VWAP: has the bar turned green?", f"{lab}, green", b & green)
            put("B. below VWAP: has the bar turned green?", f"{lab}, red", b & ~green)
        put("C. 0.3+ below VWAP, by volume", "volume 2x or more", deep & (vr >= 2))
        put("C. 0.3+ below VWAP, by volume", "volume 1-2x", deep & (vr >= 1) & (vr < 2))
        put("C. 0.3+ below VWAP, by volume", "volume under 1x", deep & (vr < 1))
        for lab, lo, hi in (("09:50-10:30", 20, 60), ("10:30-12:00", 60, 150), ("12:00-14:00", 150, 270), ("14:00-close", 270, 390)):
            put("D. 0.3+ below VWAP, by time of day", lab, deep & (minute >= lo) & (minute < hi))
        put("E. 0.3+ below VWAP, by what the market did in the last 15 min", "market down 15 bp+", deep & (mkt <= -15))
        put("E. 0.3+ below VWAP, by what the market did in the last 15 min", "market flat", deep & (mkt > -15) & (mkt < 15))
        put("E. 0.3+ below VWAP, by what the market did in the last 15 min", "market up 15 bp+", deep & (mkt >= 15))
        for lab, lo, hi in (("largest 50", 0, 50), ("next 100", 50, 150), ("the rest", 150, 10_000)):
            put("F. 0.3+ below VWAP, by name size (prior dollar volume)", lab, deep & (rank >= lo) & (rank < hi))
        # G: the largest 50 judged ONLY against the other largest-50 names at the same minute (rules out "big names just bounce
        # harder when the market bounces"), with the same depth scale.
        top = rank < 50
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            f50 = fwd[:, cols]
            rel50 = f50 - np.nanmean(np.where(top, f50, np.nan), axis=0, keepdims=True)
        for lo, hi, lab in DEPTH:
            m = top & ok & (z >= lo) & (z < hi)
            if m.sum() >= 3:
                cells["G. largest 50 only, vs the other largest-50 names at the same minute"][lab][d] = float(rel50[m].mean())
                counts[("G. largest 50 only, vs the other largest-50 names at the same minute", lab)] += int(m.sum())
            if lo < -0.25:
                raw = f50[m]
                if len(raw) >= 3:
                    cells["H. largest 50, 0.3+ below VWAP: RAW forward return (what a buyer gets before cost)"][lab][d] = float(raw.mean())
                    counts[("H. largest 50, 0.3+ below VWAP: RAW forward return (what a buyer gets before cost)", lab)] += len(raw)
    print(f"reversal map, hold {a.hold} m, {days[0]}..{days[-1]} ({len(days)} days), every {STEP}th minute; "
          f"numbers are bp relative to other names at the same minute")
    for table, rows in cells.items():
        print(f"\n{table}\n  {'':<26}{'cells':>9}{'mean bp':>9}{'day-t':>7}{'halves':>17}")
        for row, per_day in rows.items():
            print(line(row, per_day, days, counts[(table, row)]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
