import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

import square_confirmation as sc  # noqa: E402


def test_starter_is_first_5min_breakout_in_window():
    h = np.full(390, 10.0)
    c = np.full(390, 9.9)
    c[50] = 10.2                     # before the window
    c[70] = 10.3                     # first breakout in [60, 90]
    c[75] = 10.5
    assert sc.starter_bar(h, c, 60) == 70
    assert sc.starter_bar(h, np.full(390, 9.9), 60) is None


def test_classify_groups():
    zone = np.zeros(390, bool)
    assert sc.classify(100, [105], zone) == ("CONFIRMED", 105)
    assert sc.classify(100, [116], zone) == ("UNCONFIRMED", None)       # outside 15 minutes
    assert sc.classify(100, [100], zone) == ("UNCONFIRMED", None)       # the starter bar itself does not confirm
    zone[100] = True
    assert sc.classify(100, [105], zone) == ("ALREADY_SQUARED", None)


def test_hold_ret_and_session_end():
    o = np.full(390, 10.0)
    c = np.full(390, 10.0)
    c[129] = 11.0
    assert abs(sc.hold_ret(o, c, 100, 129) - 1000.0) < 1e-9
    assert sc.hold_ret(o, c, 380, 400) is None


def test_separation_needs_both_groups_per_day():
    rows = [{"day": "d1", "group": "CONFIRMED", "lift": 10.0}, {"day": "d1", "group": "UNCONFIRMED", "lift": 4.0},
            {"day": "d2", "group": "CONFIRMED", "lift": 7.0},
            {"day": "d3", "group": "ALREADY_SQUARED", "lift": 50.0}, {"day": "d3", "group": "UNCONFIRMED", "lift": 1.0}]
    assert sc.separation(rows) == [("d1", 6.0)]


def test_beta_prev_recovers_slope():
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.001, 389)
    spy = 400 * np.exp(np.concatenate([[0], np.cumsum(x)]))
    name = 50 * np.exp(np.concatenate([[0], np.cumsum(1.5 * x + rng.normal(0, 0.0002, 389))]))
    assert abs(sc.beta_prev(name, spy) - 1.5) < 0.1
