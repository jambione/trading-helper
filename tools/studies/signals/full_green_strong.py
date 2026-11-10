"""The same candle, stricter: at least 25 bp tall and on at least 1.5x the average volume of the previous 20 bars."""
import numpy as np
from _lib import after, vol_ratio
from full_green import full_green

NAME = "full_green_strong"
HOLD = 3


def signal(day, prev):
    with np.errstate(all="ignore"):
        return after(full_green(day, 25.0) & (vol_ratio(day) >= 1.5), 20)
