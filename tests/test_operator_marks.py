"""operator_marks: RSI-2 and the four rule triggers."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "studies"))
import operator_marks as om  # noqa: E402


def test_rsi2_drops_on_falls_and_rises_on_up():
    c = np.array([10, 10.1, 10.0, 9.9, 9.8, 9.7, 9.6, 9.65], float)
    r = om.rsi2(c)
    assert r[6] < 10 and r[7] > r[6]


def test_rules_fire():
    n = 20
    fast = np.full(n, -60.0); slow = np.full(n, -30.0); r2 = np.full(n, 50.0)
    fast[10] = -55.0                                   # rising from -60, slow >= -35
    assert om.fire(fast, slow, r2, 10)["EARLY_CURL"]
    r2[9], r2[10] = 5.0, 20.0
    assert om.fire(fast, slow, r2, 10)["RSI2_HOOK"]
    fast2 = np.array([-60, -65, -75, -80, -78, -74, -70.0] + [-70.0] * 13)
    r2b = np.full(n, 50.0); r2b[3], r2b[4] = 4.0, 12.0
    f = om.fire(fast2, slow, r2b, 6)
    assert f["DEEP_CURL"] and f["BOTH"]
    r2c = np.full(n, 50.0)
    assert not om.fire(fast2, slow, r2c, 6)["BOTH"]
