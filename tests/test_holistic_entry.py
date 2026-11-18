"""holistic_entry: the bracket label and past-only features."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "studies"))
import holistic_entry as he  # noqa: E402


def _flat(px=100.0):
    a = np.full(390, px)
    return a.copy(), a.copy(), a.copy(), a.copy(), np.full(390, 1000.0)


def test_bracket_win_loss_stop_first_and_timeout():
    o, h, l, c, v = _flat()
    h[105] = 100.25
    assert he.bracket(o, h, l, c, 100) == he.UP - he.COST
    o, h, l, c, v = _flat(); l[103] = 99.4; h[103] = 100.3       # both in one minute -> stop
    assert he.bracket(o, h, l, c, 100) == -he.DOWN - he.COST
    o, h, l, c, v = _flat(); c[115] = 100.1
    assert abs(he.bracket(o, h, l, c, 100) - (10.0 - he.COST)) < 1e-6
    assert he.bracket(o, h, l, c, 380) is None


def test_features_use_only_the_past():
    o, h, l, c, v = _flat()
    spy = np.full(390, 500.0)
    f1 = he.features(o, h, l, c, v, 100, 98.0, spy, 499.0)
    c2 = c.copy(); c2[101:] = 150.0; h2 = h.copy(); h2[101:] = 150.0
    f2 = he.features(o, h2, l, c2, v, 100, 98.0, spy, 499.0)
    for k in he.FEATS:
        a, b = f1[k], f2[k]
        assert (np.isnan(a) and np.isnan(b)) or a == b, k
