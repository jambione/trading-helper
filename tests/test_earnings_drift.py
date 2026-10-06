"""Pure rules of tools/studies/earnings_drift.py (prereg docs/studies/earnings_drift_prereg.json).

Written for the skeptic's 2026-10-06 review: each test pins one finding so a regression reads as that finding.
No network, no panel: only the parsers, the walk and the statistics helpers.
"""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))

import earnings_drift as ed  # noqa: E402


# ---------------------------------------------------------------- F2 t_used sign rule
def test_t_used_is_signed_min():
    assert ed.t_used(3.0, 2.5) == 2.5
    assert ed.t_used(3.0, -2.5) == -2.5          # old rule: min |t| = 2.5 with the mean's sign -> +2.5, a false pass
    assert ed.t_used(-1.0, 4.0) == -1.0


def test_t_used_nan_fails():
    assert math.isnan(ed.t_used(float("nan"), 3.0))
    assert math.isnan(ed.t_used(3.0, float("nan")))
    assert not (ed.t_used(3.0, float("nan")) >= 2)


# ---------------------------------------------------------------- F3 NaN inside the hold
def _series(n=300, seed=1):
    import numpy as np
    rng = np.random.default_rng(seed)
    spy = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    stk = 50 * np.cumprod(1 + rng.normal(0, 0.015, n))
    spy_r = np.r_[np.nan, spy[1:] / spy[:-1] - 1]
    return stk.reshape(-1, 1), spy, spy_r, [f"d{i}" for i in range(n)]


def test_excess_needs_every_close_in_the_hold():
    c, spy, spy_r, dates = _series()
    ok = ed.excess_at(c, spy, spy_r, dates, 0, 200, 5)
    assert ok is not None and all(math.isfinite(v) for v in ok["daily"])
    c[202, 0] = float("nan")                       # endpoints fine, a hole mid-hold
    assert ed.excess_at(c, spy, spy_r, dates, 0, 200, 5) is None
