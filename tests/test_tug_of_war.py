import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import tug_of_war as T  # noqa: E402


def test_score_window_and_sign():
    hist = [(i, 100.0, 101.0, 99.0) for i in range(20)]          # intraday +1%/day, overnight +1.0101%/day
    sc = T.score(hist, 20)
    assert sc is not None
    assert abs(sc[1] - 20 * math.log(1.01)) < 1e-12
    assert abs(sc[2] - 20 * math.log(100 / 99)) < 1e-12
    assert T.score(hist, 40) is None                             # outside the 20-session window
    assert T.score(hist[:14], 20) is None                        # fewer than 15


def test_two_way_t_runs():
    trades = [(f"d{i % 10}", f"s{i % 7}", 0.001 + 0.0001 * (i % 3)) for i in range(70)]
    t = T.two_way_t(trades)
    assert t is not None and t > 0
