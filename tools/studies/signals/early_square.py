"""CANDIDATE 3 - the desk's own idea, one step earlier: the fast line has just gone overbought (first bar >= -20
after being below) while the two-hour line is still mid-range (-70 .. -30) but rising. Buys the push before the
slow line confirms it."""
import numpy as np
from _lib import after, percent_r

NAME = "early_square"
HOLD = 3


def signal(day, prev):
    fast, slow = percent_r(day, prev, 21, ewm_span=3), percent_r(day, prev, 112, ewm_span=3)
    first_ob = np.zeros(fast.shape, bool)
    first_ob[:, 1:] = (fast[:, 1:] >= -20) & (fast[:, :-1] < -20)
    slow_up = np.zeros(slow.shape, bool)
    slow_up[:, 3:] = slow[:, 3:] > slow[:, :-3]
    return after(first_ob & (slow >= -70) & (slow <= -30) & slow_up, 5)
