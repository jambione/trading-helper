"""pr_dir5: 5-minute direction. No market returns from the warehouse."""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import early_entry_honest as E  # noqa: E402
import pr_dir5 as D  # noqa: E402


def test_direction_is_the_last_five_minutes_and_the_gap_is_fast_minus_slow():
    m = 600
    at = {m: (-20.0, -50.0), m - 5: (-40.0, -55.0)}
    fd, sd, gd, gap = D.direction(at, m)
    assert fd == 20.0 and sd == 5.0
    assert gap == 30.0
    assert gd == fd - sd == 15.0
    assert D.cell_of(fd, sd, gd) == (1, 1, 1)


def test_a_flat_direction_is_left_out_of_the_cells():
    assert D.cell_of(0.0, 5.0, 5.0) is None
    assert D.cell_of(-3.0, -4.0, -1.0) == (-1, -1, -1)
    assert D.cell_of(3.0, 4.0, -1.0) == (1, 1, -1)


def test_a_later_minute_does_not_change_the_five_minute_direction():
    m = 600
    at = {m: (-20.0, -50.0), m - 5: (-40.0, -55.0), m + 1: (0.0, 0.0)}
    assert D.direction(at, m) == (20.0, 5.0, 15.0, 30.0)


def test_a_missing_five_minute_reading_is_dropped():
    assert D.direction({600: (-20.0, -50.0)}, 600) is None


def test_three_minute_return_ends_three_minutes_after_the_signal():
    # Signal at 10:00. Entry is the 10:01 open. Hold 3 ends at the 10:03 close.
    sip = {
        601: (100.0, 101.0),
        602: (101.0, 102.0),
        603: (102.0, 103.0),
        605: (104.0, 110.0),
    }
    assert math.isclose(E.markout(sip, 601, 3), 103.0 / 100.0 - 1.0)
    assert math.isclose(E.markout(sip, 601, 5), 110.0 / 100.0 - 1.0)
    assert D.CALL_HOLD == 3 and 5 in D.HOLDS and D.LAG == 5


def _ok(**over):
    base = dict(gross_a=0.001, t_a=1.5, ex_a=0.001, t_ex_a=1.5, cross_a=0.001, t_cross_a=1.5,
                days_a=30, n_a=1000,
                gross_b=0.001, t_b=1.5, ex_b=0.001, t_ex_b=1.5, cross_b=0.001, t_cross_b=1.5,
                days_b=30, n_b=1000, fail_share=0.0)
    base.update(over)
    return D.call(**base)


def test_call_requires_both_halves_and_does_not_read_a_cost():
    assert _ok() == "CORRELATION"
    assert _ok(gross_a=-0.001, t_a=2.0) == "NO CORRELATION"
    assert _ok(t_b=1.49) == "NO CORRELATION"
    assert _ok(ex_b=-0.001) == "NO CORRELATION"
    assert _ok(cross_b=-0.001) == "NO CORRELATION"
    assert _ok(t_a=None) == "UNDERPOWERED"
    assert _ok(days_b=29) == "UNDERPOWERED"
    assert _ok(n_b=999) == "UNDERPOWERED"
    assert _ok(fail_share=0.02) == "UNDERPOWERED"
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "pr_dir5.py")).read()
    body = src.split("def call(", 1)[1].split("\ndef ", 1)[0]
    assert "0.0003" not in body and "COST" not in body


def test_day_slope_is_per_point_of_direction():
    x = np.array([0.0, 10.0, 20.0])
    y = np.array([0.0, 0.001, 0.002])
    assert math.isclose(D.day_slope(x, y), 0.0001)
