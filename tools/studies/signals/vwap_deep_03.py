"""The same entry at a shallower depth: at least 0.3 prior-day ranges below VWAP (more trades, smaller bounce on the map)."""
from _lib import after
from vwap_deep import deep

NAME = "vwap_deep_03"
HOLD = 60


def signal(day, prev):
    return after(deep(day, prev, 0.3), 20)
