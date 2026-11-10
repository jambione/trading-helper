"""IDEA 3 - find the name BEFORE the leg: a green bar on at least 3x recent volume while the name is still within
2% of yesterday's close. From 09:50."""
import numpy as np
from _lib import after, day_change, vol_ratio

NAME = "surge_unmoved"
HOLD = 3


def signal(day, prev):
    chg = day_change(day, prev)
    with np.errstate(all="ignore"):
        sig = (vol_ratio(day) >= 3.0) & (np.abs(chg) < 2.0) & (day["c"] > day["o"])
    return after(sig, 20)
