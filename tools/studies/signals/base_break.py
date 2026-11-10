"""CANDIDATE 2 - break of a short base on volume: the previous 15 bars span <= 0.6% (a base), this bar closes above
their high, on at least 2x the average volume of the previous 20 bars. From 09:50 (needs 20 bars of today)."""
import numpy as np
from _lib import NBAR, after, joined, roll, vol_ratio

NAME = "base_break"
HOLD = 3
BASE, MAX_RANGE = 15, 0.006


def signal(day, prev):
    h, l = joined(day, prev, "h"), joined(day, prev, "l")
    hh, ll = roll(h, BASE, np.nanmax), roll(l, BASE, np.nanmin)
    p_hh = np.full(hh.shape, np.nan)
    p_ll = np.full(ll.shape, np.nan)
    p_hh[:, 1:], p_ll[:, 1:] = hh[:, :-1], ll[:, :-1]            # the base ends at the bar before this one
    p_hh, p_ll = p_hh[:, NBAR:], p_ll[:, NBAR:]
    with np.errstate(all="ignore"):
        tight = (p_hh / p_ll - 1.0) <= MAX_RANGE
        sig = tight & (day["c"] > p_hh) & (vol_ratio(day) >= 2.0)
    return after(sig, 20)
