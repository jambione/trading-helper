"""tools/studies/pdh_retest.py: the bracket, and a setup that only reads bars up to its decision."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import pdh_retest as R


def _bars(path):
    c = np.array(path, float)
    o = np.r_[c[0], c[:-1]]
    return o, np.maximum(o, c) + 0.01, np.minimum(o, c) - 0.01, c


def test_bracket_stop_target_and_close():
    o, h, l, c = _bars([100, 100, 101, 102, 103])
    assert abs(R.bracket(o, h, l, c, 1, 1, 0.005) - 100.0) < 1e-6          # +1% target hit (2x 0.5%)
    o, h, l, c = _bars([100, 100, 99.4, 98])
    assert abs(R.bracket(o, h, l, c, 1, 1, 0.005) + 50.0) < 1e-6           # stop at -0.5%
    o, h, l, c = _bars([100, 100, 100.2, 100.3])
    assert abs(R.bracket(o, h, l, c, 1, 1, 0.02) - 30.0) < 1e-6            # neither: the last close
    o, h, l, c = _bars([100, 100, 99, 98])
    assert abs(R.bracket(o, h, l, c, 1, -1, 0.005) - 100.0) < 1e-6         # a short's target


def _day(path, prev_high=100.0, prev_low=95.0):
    o, h, l, c = _bars(path)
    day = {"syms": ["A"], "o": o[None], "h": h[None], "l": l[None], "c": c[None]}
    prev = {"syms": ["A"], "o": np.full((1, 5), 97.0), "h": np.array([[prev_high] * 5]), "l": np.array([[prev_low] * 5]),
            "c": np.full((1, 5), 97.0)}
    return day, prev


def test_long_setup_waits_for_the_5min_close_then_a_green_retest():
    path = [99.5] * 4 + [100.6] + [100.5, 100.05, 100.3, 100.8, 101.5, 102, 103] + [103] * 20
    day, prev = _day(path)
    s = R.setups(day, prev)
    assert len(s) == 1 and s[0][0] == 0 and s[0][2] == 1 and s[0][1] == 8   # retest at bar 7 (dip to 100.04, green close 100.3) -> buy bar 8
    assert R.setups(day, prev, retest=False)[0][1] == 5                      # no-retest version buys right after the break candle
    dead = [99.5] * 4 + [100.6, 100.5, 99.7, 100.3] + [100.3] * 20          # closes 0.3% back under the level first
    assert R.setups(*_day(dead)) == []


def test_no_lookahead_in_the_setup():
    rng = np.random.default_rng(3)
    path = list(100 + np.cumsum(rng.normal(0, 0.15, 120)))
    day, prev = _day(path, prev_high=100.3, prev_low=99.7)
    full = R.setups(day, prev)
    for s, i, side, risk in full:
        cut = {k: (v.copy() if k != "syms" else v) for k, v in day.items()}
        for k in ("o", "h", "l", "c"):
            cut[k][:, i + 1:] = np.nan                                        # blank everything after the entry bar
        again = [x for x in R.setups(cut, prev) if x[2] == side]
        assert again and again[0][1] == i


def test_vectorised_bracket_matches_the_loop():
    rng = np.random.default_rng(5)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, (40, 200)), axis=1))
    o = np.c_[c[:, :1], c[:, :-1]]
    h, l = np.maximum(o, c) * (1 + rng.uniform(0, 0.002, c.shape)), np.minimum(o, c) * (1 - rng.uniform(0, 0.002, c.shape))
    h[3, 50:60] = np.nan; l[3, 50:60] = np.nan; o[3, 50:60] = np.nan; c[3, 50:60] = np.nan
    for side in (1, -1):
        for risk in (0.002, 0.01):
            v = R.bracket_all(o, h, l, c, 20, side, risk)
            w = np.array([R.bracket(o[k], h[k], l[k], c[k], 20, side, risk) for k in range(40)], float)
            assert np.allclose(v, w, equal_nan=True)
