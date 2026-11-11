"""Map-derived reversal entry (tools/studies/reversal_map.py, dev days only): the name is at least DEPTH prior-day
ranges below its session VWAP. No candle or volume condition (the map showed neither matters). Meant for the 50
largest names: run with --top-n 50 --price-hi 5000 --cost-bp 1 --stress-bp 2. From 09:50."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _lib import after  # noqa: E402
from entry_ideas import _ctx  # noqa: E402

NAME = "vwap_deep"
HOLD = 60
DEPTH = 0.5


def deep(day, prev, depth):
    x = _ctx(day, prev)
    with np.errstate(all="ignore"):
        return x["live"] & (x["vwd"] <= -depth * x["prng"])


def signal(day, prev):
    return after(deep(day, prev, DEPTH), 20)
