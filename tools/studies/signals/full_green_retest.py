"""The candle as SUPPORT: after a full green candle (25 bp+), wait up to 10 bars for price to come back and touch
the candle's midpoint without closing below its open, then buy on the first bar that closes back up (green)."""
import numpy as np
from _lib import after
from full_green import full_green

NAME = "full_green_retest"
HOLD = 3
WAIT = 10


def signal(day, prev):
    o, l, c = day["o"], day["l"], day["c"]
    fg = full_green(day, 25.0)
    n, T = c.shape
    sig = np.zeros((n, T), bool)
    with np.errstate(all="ignore"):
        for k in range(1, WAIT + 1):                       # candle at m-k, decision at m: only bars <= m are read
            cand, mid, base = fg[:, :-k], (o[:, :-k] + c[:, :-k]) / 2.0, o[:, :-k]
            held = np.ones(cand.shape, bool)
            for j in range(1, k + 1):                      # every close since the candle stayed above its open
                held &= c[:, j:T - k + j] >= base
            touched = l[:, k:] <= mid
            sig[:, k:] |= cand & held & touched & (c[:, k:] > o[:, k:])
    return after(sig, 6)
