"""The current arm, only on names whose day change at the decision is in [-1%, 3%). Read the three bands side
by side to see whether refusing extended names would help."""
import numpy as np
from _lib import day_change
from current_square import signal as _square

NAME = "square_flat"
HOLD = 3


def signal(day, prev):
    chg = day_change(day, prev)
    with np.errstate(all="ignore"):
        return _square(day, prev) & (chg >= -1) & (chg < 3)
