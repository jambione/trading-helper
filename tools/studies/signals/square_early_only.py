"""Only the trades the VARIABLE slow line adds: the current arm on today's bars, before the fixed 112-bar line can
exist (09:50 to 11:21). Answers "are the extra early opens any good?"."""
from square_variable import signal as _sq

NAME = "square_early_only"
HOLD = 3


def signal(day, prev):
    s = _sq(day, prev)
    s[:, 111:] = False
    return s
