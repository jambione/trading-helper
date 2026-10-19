"""pr_read: both lines against the next 3 minutes. No market returns."""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import early_entry_honest as E  # noqa: E402
import pr_read as R  # noqa: E402


def _at():
    m = 600
    return m, {
        m: (-10.0, -40.0),
        m - 3: (-20.0, -45.0),
        m - 5: (-30.0, -80.0),
        m - 15: (-50.0, -60.0),
        m - 30: (-70.0, -90.0),
        m + 1: (0.0, 0.0),
    }, (-55.0, -65.0)


def test_the_row_keeps_both_lines_and_the_five_minute_product():
    m, at, prev = _at()
    row = R.features(at, prev, m)
    assert row is not None and len(row) == R.N_FEAT == 14
    assert math.isclose(row[0], -0.10) and math.isclose(row[1], -0.40)
    assert math.isclose(row[4], -0.30) and math.isclose(row[5], -0.80)
    # gap now = 30 points, stored only as its square on the scaled lines.
    assert math.isclose(row[12], 0.30 ** 2)
    # together5 = (fast change) * (slow change) on the scaled lines = 0.20 * 0.40.
    assert math.isclose(row[13], 0.08)
    assert R.features(at, prev, m) == R.features(
        {k: v for k, v in at.items() if k <= m}, prev, m)


def test_a_missing_lag_is_dropped_and_a_later_minute_is_not_read():
    m, at, prev = _at()
    assert R.features(at, prev, m) is not None
    del at[m - 30]
    assert R.features(at, prev, m) is None
    at[m - 30] = (-70.0, -90.0)
    at[m + 3] = (50.0, 50.0)
    again = R.features(at, prev, m)
    assert math.isclose(again[0], -0.10)


def test_three_minute_return_and_the_window():
    sip = {601: (100.0, 101.0), 602: (101.0, 102.0), 603: (102.0, 103.0)}
    assert math.isclose(E.markout(sip, 601, 3), 0.03)
    assert R.HOLD == 3 and R.SIG_START == 10 * 60 and R.SIG_END == 15 * 60 + 56


def test_a_name_is_not_dropped_for_the_finished_days_bar_count():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "pr_read.py")).read()
    body = src.split("def day_job(", 1)[1].split("\ndef ", 1)[0]
    assert "< 30" not in body


def _ok(**over):
    base = dict(gross=0.001, t=1.5, excess=0.001, t_ex=1.5, cross=0.001, t_cross=1.5,
                days=30, excess_days=30, cross_days=30, n=1000, singular=False, fail_share=0.0)
    base.update(over)
    return R.grade(**base)


def test_the_call_is_the_held_out_gross_and_does_not_read_a_cost():
    assert _ok() == "DIRECTION"
    assert _ok(gross=-0.0001, t=3.0) == "NO DIRECTION"
    assert _ok(excess=-0.001) == "NO DIRECTION"
    assert _ok(cross=-0.001) == "NO DIRECTION"
    assert _ok(t=1.49) == "NO DIRECTION"
    assert _ok(t=None) == "UNDERPOWERED"
    assert _ok(days=29) == "UNDERPOWERED"
    assert _ok(n=999) == "UNDERPOWERED"
    assert _ok(excess_days=29) == "UNDERPOWERED"
    assert _ok(singular=True) == "UNDERPOWERED"
    assert _ok(fail_share=0.02) == "UNDERPOWERED"
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "pr_read.py")).read()
    body = src.split("def grade(", 1)[1].split("\ndef ", 1)[0]
    verdict = src.split("verdict = grade(", 1)[1].split(")", 1)[0]
    assert "0.0003" not in body and "COST" not in body and "net" not in body
    assert 'b_slice["mean"]' in verdict and "net10" not in verdict


def test_a_full_rank_fit_returns_a_coefficient_vector():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(80, R.N_FEAT))
    y = rng.normal(size=80)
    design = np.column_stack([np.ones(80), x])
    beta = R.R.fit_beta(design.T @ design, design.T @ y)
    assert beta is not None and len(beta) == R.N_FEAT + 1
