"""CANDIDATE 4 - the current arm with a VARIABLE slow line: today's bars only, as few as 20, growing to 112.
Same rule as current_square otherwise. Shows what opening from ~09:50 instead of late morning would have bought."""
import numpy as np
from _lib import after, percent_r_session

NAME = "square_variable"
HOLD = 3


def signal(day, prev):
    fast, slow = percent_r_session(day, 21, 6, ewm_span=7), percent_r_session(day, 112, 20, ewm_span=3)
    rising = np.zeros(fast.shape, bool)
    rising[:, 1:] = fast[:, 1:] > fast[:, :-1]
    with np.errstate(all="ignore"):
        sig = (fast >= -20) & (slow >= -20) & (np.abs(fast - slow) <= 15) & rising
    return after(sig, 15)
