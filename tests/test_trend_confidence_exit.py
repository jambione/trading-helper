"""trend_confidence_exit: the hold shape and when the rule acts."""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "studies"))
import trend_confidence_exit as tc  # noqa: E402

ET = ZoneInfo("America/New_York")


def test_trend_shapes():
    assert tc.trend_up_or_curl([10, 10.1, 10.2, 10.3, 10.4], [9.9] * 5)               # up-slope
    assert not tc.trend_up_or_curl([10.4, 10.3, 10.2, 10.1, 10.0], [9.9, 9.8, 9.7, 9.6, 9.5])   # down
    assert not tc.trend_up_or_curl([10, 10.01, 9.99, 10.0, 9.99], [9.98, 9.97, 9.98, 9.97, 9.96])  # sideways, no curl
    assert tc.trend_up_or_curl([10.4, 10.2, 10.0, 9.9, 9.95], [10.3, 10.1, 9.9, 9.85, 9.9])      # curl: higher close + higher low


def test_decide_only_never_above_below_and_flat():
    day = "2026-11-12"
    t0 = datetime.fromisoformat(day + "T09:30").replace(tzinfo=ET).timestamp()
    tr = {"day": day, "t_in": t0 + 100 * 60 + 5, "t_out": t0 + 200 * 60, "px_in": 10.0, "ret_bp": -60.0}
    h, lo, c = np.full(390, 9.99), np.full(390, 9.95), np.full(390, 9.97)
    d = tc.decide(tr, h, lo, c, 100, 5)
    assert abs(d - ((9.97 / 10 - 1) * 1e4 - tc.EXIT_COST + 60.0)) < 1e-6
    h2 = h.copy(); h2[102] = 10.02                       # was above entry: no action
    assert tc.decide(tr, h2, lo, c, 100, 5) == 0.0
    c3 = c.copy(); c3[101:106] = [9.95, 9.96, 9.97, 9.98, 9.985]   # trending up: hold
    assert tc.decide(tr, h, lo, c3, 100, 5) == 0.0
    assert tc.decide({**tr, "t_out": t0 + 103 * 60}, h, lo, c, 100, 5) == 0.0   # already out
