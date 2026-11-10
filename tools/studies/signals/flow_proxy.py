"""IDEA 2 - buying pressure from bars, meant for the lowest-cost names: over the last 5 bars at least 60% of net
volume is in up bars (volume signed by close vs open), on at least 1.5x recent volume. From 09:50.
Run with --top-n 30 --price-hi 5000 --cost-bp 1 --stress-bp 2."""
import numpy as np
from _lib import after, vol_ratio

NAME = "flow_proxy"
HOLD = 3


def signal(day, prev):
    v = np.nan_to_num(day["v"])
    sv = v * np.sign(np.nan_to_num(day["c"] - day["o"]))
    z = np.zeros((v.shape[0], 1))
    cs, cv = np.concatenate([z, np.cumsum(sv, 1)], 1), np.concatenate([z, np.cumsum(v, 1)], 1)
    imb = np.full(v.shape, np.nan)
    with np.errstate(all="ignore"):
        imb[:, 4:] = (cs[:, 5:] - cs[:, :-5]) / (cv[:, 5:] - cv[:, :-5])
        sig = (imb >= 0.6) & (vol_ratio(day) >= 1.5)
    return after(sig, 20)
