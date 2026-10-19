"""pr_rows: five combination rows. No market returns."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import early_entry_honest as E  # noqa: E402
import pr_rows as R  # noqa: E402


def _at(m, pairs):
    return {m - i: pairs[i] for i in range(7)}


def test_the_hook_is_the_first_minute_the_fast_line_leaves_the_floor():
    m = 600
    # Fast was -90 five minutes ago and is -70 now. Slow is still -75.
    # One minute earlier the fast line had risen only 2 points, so that minute is not the hook.
    at = _at(m, {
        0: (-70.0, -75.0),
        1: (-89.0, -78.0),
        2: (-90.0, -80.0),
        3: (-90.0, -82.0),
        4: (-91.0, -84.0),
        5: (-90.0, -88.0),
        6: (-91.0, -90.0),
    })
    assert R.reading(at, m) == (True, False, False, False, False)
    assert R.reading(at, m) == R.reading({k: v for k, v in at.items() if k <= m}, m)


def test_a_rise_of_less_than_five_points_is_not_the_hook():
    now = ((-86.0, -75.0), (-90.0, -80.0), (-91.0, -82.0), (-90.0, -88.0))
    prev = ((-90.0, -80.0), (-91.0, -82.0), (-92.0, -84.0), (-93.0, -90.0))
    assert R.combine(now, prev)[0] is False


def test_square_onset_and_the_minutes_that_follow_it():
    m = 600
    at = _at(m, {
        0: (-10.0, -12.0),
        1: (-25.0, -30.0),
        2: (-28.0, -32.0),
        3: (-30.0, -34.0),
        4: (-32.0, -36.0),
        5: (-40.0, -50.0),
        6: (-42.0, -55.0),
    })
    assert R.reading(at, m)[1] is True and R.reading(at, m)[2] is False
    stay = _at(m, {
        0: (-8.0, -10.0),
        1: (-12.0, -14.0),
        2: (-18.0, -16.0),
        3: (-22.0, -24.0),
        4: (-26.0, -28.0),
        5: (-30.0, -40.0),
        6: (-36.0, -44.0),
    })
    # Previous minute was already touching and the fast line was rising then too.
    assert R.reading(stay, m)[1] is False and R.reading(stay, m)[2] is True


def test_pinned_needs_the_slow_line_far_below_and_the_fast_line_not_falling():
    pinned = ((-5.0, -50.0), (-6.0, -55.0), (-8.0, -60.0), (-12.0, -70.0))
    prev = ((-6.0, -55.0), (-7.0, -58.0), (-10.0, -64.0), (-14.0, -72.0))
    assert R.combine(pinned, prev)[3] is True
    rolling = ((-15.0, -50.0), (-10.0, -48.0), (-5.0, -40.0), (-8.0, -30.0))
    assert R.combine(rolling, prev)[3] is False
    close = ((-5.0, -15.0), (-6.0, -16.0), (-8.0, -18.0), (-10.0, -20.0))
    assert R.combine(close, prev)[3] is False


def test_falling_is_both_lines_still_dropping_and_close():
    falling = ((-88.0, -90.0), (-86.0, -88.0), (-80.0, -82.0), (-70.0, -75.0))
    prev = ((-86.0, -88.0), (-84.0, -86.0), (-78.0, -80.0), (-68.0, -74.0))
    got = R.combine(falling, prev)
    assert got[4] is True and got[0] is False
    wide = ((-88.0, -50.0), (-86.0, -48.0), (-80.0, -40.0), (-70.0, -30.0))
    assert R.combine(wide, prev)[4] is False


def test_a_missing_lag_is_dropped_and_a_later_minute_is_not_read():
    m = 600
    at = _at(m, {i: (-70.0, -75.0) for i in range(7)})
    at[m + 3] = (0.0, 0.0)
    assert R.reading(at, m) is not None
    del at[m - 6]
    assert R.reading(at, m) is None


def test_three_minute_return_and_the_window():
    sip = {601: (100.0, 101.0), 602: (101.0, 102.0), 603: (102.0, 103.0),
           604: (103.0, 104.0), 605: (104.0, 105.0)}
    assert math.isclose(E.markout(sip, 601, 3), 0.03)
    assert math.isclose(E.markout(sip, 601, 5), 0.05)
    assert R.HOLD == 3 and R.SIG_START == 9 * 60 + 36 and R.SIG_END == 15 * 60 + 56


def test_a_name_is_not_dropped_for_the_finished_days_bar_count():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "pr_rows.py")).read()
    body = src.split("def day_job(", 1)[1].split("\ndef ", 1)[0]
    assert "< 30" not in body


def test_the_word_compares_the_two_gross_means():
    assert R.describe(0.001, 30, 1000, 0.0002, 30, 1000) == "SAME WAY UP"
    assert R.describe(-0.001, 30, 1000, -0.0002, 30, 1000) == "SAME WAY DOWN"
    assert R.describe(0.0, 30, 1000, 0.0, 30, 1000) == "FLAT BOTH"
    assert R.describe(0.001, 30, 1000, -0.001, 30, 1000) == "NOT THE SAME"
    assert R.describe(0.001, 29, 1000, 0.001, 30, 1000) == "THIN"
    assert R.describe(0.001, 30, 999, 0.001, 30, 1000) == "THIN"
    assert R.describe(None, 30, 1000, 0.001, 30, 1000) == "THIN"
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "pr_rows.py")).read()
    body = src.split("def describe(", 1)[1].split("\ndef ", 1)[0]
    assert "COST" not in body and "0.0003" not in body and "net" not in body


def test_the_report_prints_every_row():
    def pack(mean):
        return {"mean": mean, "n": 1000, "n_up": 600, "n_10": 100, "n_30": 20,
                "mean5": mean, "excess": mean, "cross": mean}

    per = {}
    days = [f"2026-06-{i:02d}" for i in range(1, 31)] + [f"2026-08-{i:02d}" for i in range(1, 31)]
    for i, day in enumerate(days):
        per[day] = {"cnt": {"sampled": 10, "skipped_short": 0, "indicator_fail": 0,
                            "no_history": 0, "minutes": 100}}
        for name in R.ROWS:
            per[day][name] = pack(0.001 + i * 1e-6)
    text = R.report(per, days)
    for name in R.ROWS:
        assert name in text
    assert "SAME WAY UP" in text
