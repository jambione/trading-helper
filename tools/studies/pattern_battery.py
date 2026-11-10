#!/usr/bin/env python3
"""pattern_battery.py - do classic support / reversal price patterns help an open? (operator 11/10)

Ten textbook patterns on 1-minute bars, each written to use only bars at or before the decision bar, all run through
tools/studies/harness.py with the t-bar tightened for the number of reads. Buy the next bar's open.

    .venv/bin/python tools/studies/pattern_battery.py [--holds 3,15] [--warehouse DIR --top-n N --price-lo X --price-hi Y --cent-spread C]
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "signals")]
import harness as H
from _lib import _ffill, roll


def _bars(day):
    c = _ffill(day["c"])
    o = np.where(np.isnan(day["o"]), c, day["o"])
    h = np.where(np.isnan(day["h"]), c, day["h"])
    l = np.where(np.isnan(day["l"]), c, day["l"])
    return o, h, l, c, np.nan_to_num(day["v"]), ~np.isnan(day["c"])


def _lag(a, k):
    out = np.full(a.shape, np.nan)
    out[:, k:] = a[:, :-k]
    return out


def hammer(day, prev):
    """Long lower wick (at least 2x the body), small upper wick, at the low of the last 15 bars."""
    o, h, l, c, _v, live = _bars(day)
    body, rng = np.abs(c - o), h - l
    return live & (rng / c * 1e4 >= 10) & ((np.minimum(o, c) - l) >= 2 * body) & ((h - np.maximum(o, c)) <= 0.2 * rng) & (l <= roll(l, 15, np.nanmin))


def engulfing(day, prev):
    """Green bar whose body covers the previous red bar's body, after a 5-bar decline."""
    o, _h, _l, c, _v, live = _bars(day)
    po, pc = _lag(o, 1), _lag(c, 1)
    return live & (c > o) & (pc < po) & (c >= po) & (o <= pc) & (pc < _lag(c, 6)) & ((c - o) / c * 1e4 >= 8)


def three_green(day, prev):
    """Three green bars in a row, each closing higher, each at least 5 bp."""
    o, _h, _l, c, _v, live = _bars(day)
    g = (c > o) & ((c - o) / c * 1e4 >= 5)
    return live & g & _lag(g, 1).astype(bool) & _lag(g, 2).astype(bool) & (c > _lag(c, 1)) & (_lag(c, 1) > _lag(c, 2))


def higher_low(day, prev):
    """The low of the last 5 bars holds above the low of the 15 bars before them, and this bar closes above the prior bar's high."""
    _o, h, l, c, _v, live = _bars(day)
    recent, older = roll(l, 5, np.nanmin), _lag(roll(l, 15, np.nanmin), 5)
    return live & (recent > older) & (recent < older * 1.004) & (c > _lag(h, 1))


def double_bottom(day, prev):
    """The last 5 bars' low is within 5 bp of the low set 10 to 40 bars ago, and this bar closes green."""
    o, _h, l, c, _v, live = _bars(day)
    recent, older = roll(l, 5, np.nanmin), _lag(roll(l, 30, np.nanmin), 10)
    return live & (np.abs(recent / older - 1) * 1e4 <= 5) & (c > o) & (c > recent * 1.001)


def vwap_bounce(day, prev):
    """Above VWAP for the previous 10 bars, dips to touch it, closes back above, green."""
    o, h, l, c, v, live = _bars(day)
    cv = np.cumsum(v, 1)
    vwap = np.cumsum(v * (h + l + c) / 3.0, 1) / np.where(cv > 0, cv, np.nan)
    above = roll((_lag(l, 1) > _lag(vwap, 1)).astype(float), 10, np.nanmin) >= 1
    return live & above & (l <= vwap) & (c > vwap) & (c > o)


def opening_range_low(day, prev):
    """Touches the low of the first 15 minutes (within 5 bp) after 09:50 and closes green above it."""
    o, _h, l, c, _v, live = _bars(day)
    orl = np.nanmin(l[:, :15], axis=1)[:, None]
    s = live & (np.abs(l / orl - 1) * 1e4 <= 5) & (c > orl) & (c > o)
    s[:, :20] = False
    return s


def prior_close_hold(day, prev):
    """Dips to yesterday's close (within 5 bp) from above and closes green above it."""
    o, _h, l, c, _v, live = _bars(day)
    pc = _ffill(prev["c"])[:, -1][:, None]
    return live & (_lag(c, 3) > pc * 1.002) & (np.abs(l / pc - 1) * 1e4 <= 5) & (c > pc) & (c > o)


def breakout_retest(day, prev):
    """Broke the day's earlier high, comes back to touch that level, closes green above it (old resistance as support)."""
    o, h, l, c, _v, live = _bars(day)
    level = _lag(np.fmax.accumulate(h, axis=1), 10)                    # the day's high as of 10 bars ago
    broke = roll((_lag(c, 1) > level).astype(float), 5, np.nanmax) >= 1
    return live & broke & (l <= level * 1.0005) & (c > level) & (c > o)


def inside_break(day, prev):
    """The previous bar sits inside the one before it; this bar closes above that mother bar's high."""
    _o, h, l, c, _v, live = _bars(day)
    inside = (_lag(h, 1) <= _lag(h, 2)) & (_lag(l, 1) >= _lag(l, 2))
    return live & inside & (c > _lag(h, 2)) & ((_lag(h, 2) - _lag(l, 2)) / c * 1e4 >= 10)


PATTERNS = {f.__name__: f for f in (hammer, engulfing, three_green, higher_low, double_bottom, vwap_bounce, opening_range_low,
                                    prior_close_hold, breakout_retest, inside_break)}


def wrap(fn):
    def sig(day, prev):
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            s = np.asarray(fn(day, prev), dtype=bool)
        s[:, :5] = False
        return s
    return sig


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--warehouse", default=str(H.WH))
    ap.add_argument("--holds", default="3,15")
    ap.add_argument("--cent-spread", type=float, default=0.0)
    for k in ("top_n", "price_lo", "price_hi", "cost_bp", "stress_bp", "holdout"):
        ap.add_argument("--" + k.replace("_", "-"), type=type(H.DEFAULTS[k]), default=H.DEFAULTS[k])
    a = ap.parse_args(argv)
    holds = [int(x) for x in a.holds.split(",")]
    cfg = {**H.DEFAULTS, **{k: getattr(a, k) for k in ("top_n", "price_lo", "price_hi", "cost_bp", "stress_bp", "holdout")},
           "cent_spread": a.cent_spread, "family": len(PATTERNS) * len(holds)}
    files = sorted(Path(a.warehouse).glob("*.npz"))
    dev = files[:-cfg["holdout"]]
    print(f"{len(PATTERNS)} patterns x holds {holds}, {dev[1].stem}..{dev[-1].stem}; t needed {H.NormalDist().inv_cdf(1 - 0.025 / cfg['family']):.2f}")
    print(f"{'pattern':<20}{'hold':>5}{'trades':>9}{'lift bp':>9}{'day-t':>7}{'halves':>16}{'gross':>8}{'net':>8}  verdict")
    for name, fn in PATTERNS.items():
        for hold in holds:
            r = H.run(wrap(fn), hold, dev, cfg)
            if r["verdict"] == "NO TRADES":
                print(f"{name:<20}{hold:>5}  no trades")
                continue
            print(f"{name:<20}{hold:>5}{r['trades']:>9}{r['lift']['mean']:>+9.2f}{r['lift']['t']:>+7.2f}"
                  f"{r['lift_halves'][0]:>+8.2f}/{r['lift_halves'][1]:<+7.2f}{r['gross']['mean']:>+8.2f}{r['net']['mean']:>+8.2f}  {r['verdict']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
