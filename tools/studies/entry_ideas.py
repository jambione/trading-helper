#!/usr/bin/env python3
"""entry_ideas.py - reversal-side entries (operator 11/10 "I need entry ideas").

Tonight's runs: continuation entries (at the highs, breakouts, three green bars) were flat to negative; the hammer and the
model's sharp-selloff picks were the only positive signs. These eight entries all buy weakness that has started to turn.
Sizes are scale-free: a "flush" is a drop of at least a quarter of the name's own prior-day range, so one rule fits a
$200 large cap and a $5 mover. Same harness, same costs, t-bar tightened for the number of reads.

    .venv/bin/python tools/studies/entry_ideas.py [--holds 15,30] [--warehouse DIR ...]   (options as pattern_battery.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "signals")]
import pattern_battery as PB
from _lib import _ffill, day_change, vol_ratio
from pattern_battery import _bars, _lag, hammer


def _ctx(day, prev):
    o, h, l, c, v, live = _bars(day)
    pc = _ffill(prev["c"])[:, -1]
    prng = ((np.nanmax(prev["h"], 1) - np.nanmin(prev["l"], 1)) / pc * 1e4)[:, None]      # prior-day range, bp
    r15 = (c / _lag(c, 15) - 1) * 1e4
    r2 = (c / _lag(c, 2) - 1) * 1e4
    mkt15 = np.nanmean(np.where(live, r15, np.nan), 0)[None, :]
    mkt2 = np.nanmean(np.where(live, r2, np.nan), 0)[None, :]
    cv = np.cumsum(v, 1)
    vwap = np.cumsum(v * (h + l + c) / 3.0, 1) / np.where(cv > 0, cv, np.nan)
    return dict(o=o, c=c, live=live, prng=prng, r15=r15, mkt15=mkt15, mkt2=mkt2, vwd=(c / vwap - 1) * 1e4, green=c > o,
                gap=((np.where(np.isnan(day["o"][:, :1]), c[:, :1], day["o"][:, :1]) / pc[:, None] - 1) * 1e4))


def hammer_up_day(day, prev):
    """Hammer at a 15-bar low in a name that is up at least 1% on the day (a dip inside strength)."""
    return hammer(day, prev) & (day_change(day, prev) >= 1.0)


def hammer_volume(day, prev):
    """Hammer at a 15-bar low on at least 2x recent volume."""
    return hammer(day, prev) & (vol_ratio(day) >= 2.0)


def flush_green(day, prev):
    """Fell at least a quarter of its prior-day range in 15 minutes on 2x volume; buy the first green bar."""
    x = _ctx(day, prev)
    return x["live"] & (x["r15"] <= -0.25 * x["prng"]) & (vol_ratio(day) >= 2.0) & x["green"]


def morning_flush(day, prev):
    """The same flush, 09:45 to 10:30 only (where the model placed half its picks)."""
    s = flush_green(day, prev)
    s[:, :15] = False
    s[:, 60:] = False
    return s


def idio_dip(day, prev):
    """The name dropped a quarter of its prior-day range MORE than the market in 15 minutes while the market held; green bar."""
    x = _ctx(day, prev)
    return x["live"] & ((x["r15"] - x["mkt15"]) <= -0.25 * x["prng"]) & (x["mkt15"] >= -10) & x["green"]


def washout_turn(day, prev):
    """The whole market fell 25 bp+ in 15 minutes and has turned up in the last 2; buy names that fell more than the market, on a green bar."""
    x = _ctx(day, prev)
    return x["live"] & (x["mkt15"] <= -25) & (x["mkt2"] >= 4) & (x["r15"] <= x["mkt15"]) & x["green"]


def gap_down_hammer(day, prev):
    """Opened at least 1% below yesterday's close; hammer at a 15-bar low in the first hour."""
    s = hammer(day, prev) & (_ctx(day, prev)["gap"] <= -100)
    s[:, 60:] = False
    return s


def vwap_stretch(day, prev):
    """Stretched below VWAP by at least 0.3 of the prior-day range, on 2x volume, green bar."""
    x = _ctx(day, prev)
    return x["live"] & (x["vwd"] <= -0.3 * x["prng"]) & (vol_ratio(day) >= 2.0) & x["green"]


IDEAS = {f.__name__: f for f in (hammer_up_day, hammer_volume, flush_green, morning_flush, idio_dip, washout_turn, gap_down_hammer,
                                 vwap_stretch)}

if __name__ == "__main__":
    PB.PATTERNS = IDEAS
    sys.exit(PB.main())
