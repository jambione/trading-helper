"""Operator 11/10: a full green candle with no wicks as the open. The bar opens at its low and closes at its high
(each wick at most 5% of the bar's range) and the bar is at least 10 bp tall. Buy the next bar's open. From 09:35."""
import numpy as np
from _lib import after

NAME = "full_green"
HOLD = 3
MIN_RANGE_BP, MAX_WICK = 10.0, 0.05


def full_green(day, min_range_bp=MIN_RANGE_BP):
    o, h, l, c = day["o"], day["h"], day["l"], day["c"]
    with np.errstate(all="ignore"):
        rng = h - l
        return (c > o) & (rng / c * 1e4 >= min_range_bp) & ((h - c) <= MAX_WICK * rng) & ((o - l) <= MAX_WICK * rng)


def signal(day, prev):
    return after(full_green(day), 5)
