"""CANDIDATE 1 - start of a leg: the two-hour line is NOT yet at the highs (slow %R between -80 and -40), the
21-minute line crosses up through -50 this bar, on at least 1.5x recent volume, bar closes green. From 09:35."""
import numpy as np
from _lib import after, percent_r, vol_ratio

NAME = "early_leg_turn"
HOLD = 3


def signal(day, prev):
    fast, slow = percent_r(day, prev, 21, ewm_span=3), percent_r(day, prev, 112, ewm_span=3)
    cross = np.zeros(fast.shape, bool)
    cross[:, 1:] = (fast[:, 1:] >= -50) & (fast[:, :-1] < -50)
    sig = cross & (slow >= -80) & (slow <= -40) & (vol_ratio(day) >= 1.5) & (day["c"] > day["o"])
    return after(sig, 5)
