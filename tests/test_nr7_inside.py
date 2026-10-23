import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import nr7_inside as N  # noqa: E402


def test_setups():
    prior = [(110, 100), (109, 100), (108, 100), (107, 100), (106, 100), (105, 99), (104, 101)]
    f = N.setups(prior)
    assert f["nr7"] and f["nr7_inside"]
    prior[-1] = (106, 101)                       # high not below t-2 high: not inside
    assert not N.setups(prior)["nr7_inside"]


def test_trigger_and_walk():
    r = np.array([[570, 100, 100.5, 99.5, 100, 1], [575, 100.2, 101.2, 100.1, 101.0, 1],
                  [576, 101, 101.5, 100.9, 101.4, 1], [959, 102, 102, 102, 102, 1]], dtype=float)
    k = N.trigger(r, 101.01)
    assert k == 1
    w = N.walk(r, k, 101.01, 99.0)
    assert w["why"] == "time" and abs(w["gross"] - (102 / 101.01 - 1)) < 1e-12
    stop = np.array([[575, 101, 101.2, 100.9, 101.1, 1], [576, 100, 100, 98.0, 98.5, 1],
                     [577, 98.4, 98.6, 98.2, 98.3, 1], [959, 99, 99, 99, 99, 1]], dtype=float)
    w = N.walk(stop, 0, 101.01, 99.0)
    assert w["why"] == "stop" and abs(w["gross"] - (98.4 / 101.01 - 1)) < 1e-12


def test_no_trigger_on_gap_above_level():
    r = np.array([[575, 101.5, 101.8, 101.3, 101.6, 1], [959, 102, 102, 102, 102, 1]], dtype=float)
    assert N.trigger(r, 101.01) is None           # opened above the level: no cross from below
