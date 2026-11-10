"""The current arm, only on names whose day change at the decision is in [6%, 999%). Read the three bands side
by side to see whether refusing extended names would help."""
import numpy as np
from _lib import day_change
from current_square import signal as _square

NAME = "square_up6plus"
HOLD = 3


def signal(day, prev):
    chg = day_change(day, prev)
    with np.errstate(all="ignore"):
        return _square(day, prev) & (chg >= 6) & (chg < 999)
