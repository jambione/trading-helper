"""BASELINE: the desk's current arm as bars see it. Both %R lines (21 and 112 one-minute bars) overbought (>= -20),
within 15 points, fast line rising. Every candidate is read against this, in the same harness."""
import numpy as np
from _lib import after, percent_r

NAME = "current_square"
HOLD = 3


def signal(day, prev):
    fast, slow = percent_r(day, prev, 21, ewm_span=7), percent_r(day, prev, 112, ewm_span=3)
    rising = np.zeros(fast.shape, bool)
    rising[:, 1:] = fast[:, 1:] > fast[:, :-1]
    sig = (fast >= -20) & (slow >= -20) & (np.abs(fast - slow) <= 15) & rising
    return after(np.nan_to_num(sig, nan=0).astype(bool), 15)
