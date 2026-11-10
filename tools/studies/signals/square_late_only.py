"""The same arm from 11:22 on (what the desk can trade today with the fixed line). Read beside square_early_only."""
from square_variable import signal as _sq

NAME = "square_late_only"
HOLD = 3


def signal(day, prev):
    s = _sq(day, prev)
    s[:, :111] = False
    return s
