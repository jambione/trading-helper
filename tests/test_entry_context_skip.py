"""entry_context_skip: SPY drop window, bunched-buy count, skip scoring."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "studies"))
import entry_context_skip as ec  # noqa: E402


def test_spy_drop_uses_the_five_minutes_before_entry():
    c = np.full(390, 100.0)
    c[10:] = 99.9                          # minute 10 on: -10 bp
    assert ec.spy_drop_bp(c, 12) is not None and abs(ec.spy_drop_bp(c, 12) - (-10.0)) < 1e-6   # close 6 (100) -> close 11 (99.9)
    assert ec.spy_drop_bp(c, 10) == 0.0    # close 4 -> close 9: no change yet (entry minute's own move not used)
    assert ec.spy_drop_bp(c, 3) is None


def test_prior_buys_counts_other_symbols_in_window():
    a = {"symbol": "A", "t_in": 1000.0}
    b = {"symbol": "B", "t_in": 950.0}
    c = {"symbol": "A", "t_in": 990.0}     # same symbol: not counted
    d = {"symbol": "D", "t_in": 800.0}     # outside 2 min
    assert ec.prior_buys([a, b, c, d], a, 2) == 1
    assert ec.prior_buys([a, b, c, d], a, 5) == 2


def test_score_skips_count_as_zero():
    tr = [{"day": "d1", "ret_bp": -60.0}, {"day": "d1", "ret_bp": 10.0}, {"day": "d2", "ret_bp": 5.0}]
    r = ec.score(tr, lambda t: t["ret_bp"] < 0)
    assert r["skipped"] == 1 and abs(r["book_delta_bp"] - 20.0) < 1e-9
    assert r["kept_mean_bp"] == 7.5
