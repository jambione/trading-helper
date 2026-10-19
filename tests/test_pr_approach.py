"""pr_approach: the sooner open. No market returns from the warehouse."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import early_entry_honest as E  # noqa: E402
import pr_approach as A  # noqa: E402


def test_the_approach_is_still_apart_both_rising_and_the_gap_closing():
    # Gap 50 -> 30, both lines up and still below 0.
    got = A.flags(-10.0, -40.0, -30.0, -80.0)
    assert got == (True, False, False)


def test_a_gap_of_15_is_the_arrival_and_is_not_the_call():
    assert A.flags(-5.0, -20.0, -25.0, -70.0)[0] is False
    assert A.flags(-5.0, -20.01, -25.0, -70.0)[0] is True


def test_an_unchanged_or_wider_gap_is_not_closing():
    assert A.flags(-20.0, -60.0, -40.0, -80.0)[0] is False   # gap stayed 40
    assert A.flags(-10.0, -40.0, -30.0, -45.0)[0] is False   # gap 15 -> 30


def test_one_line_falling_or_a_line_already_at_zero_is_not_the_approach():
    assert A.flags(-10.0, -50.0, -30.0, -40.0)[0] is False   # slow fell
    assert A.flags(0.0, -40.0, -10.0, -70.0)[0] is False     # fast is at 0
    assert A.flags(-10.0, 0.0, -30.0, -40.0)[0] is False     # slow is at 0


def test_fast_leaving_the_top_is_a_picture_and_disjoint_from_the_approach():
    leaving = A.flags(-25.0, -40.0, -10.0, -30.0)
    assert leaving[1] is True and leaving[0] is False
    # Fell, but the fast line was not in the top band five minutes ago.
    assert A.flags(-50.0, -70.0, -30.0, -40.0)[1] is False
    # Rose, so it is not leaving.
    assert A.flags(-5.0, -30.0, -15.0, -40.0)[1] is False


def test_close_and_flat_is_a_picture():
    assert A.flags(-30.0, -38.0, -30.4, -37.5)[2] is True
    assert A.flags(-30.0, -38.0, -32.0, -37.5)[2] is False   # fast moved 2 points
    assert A.flags(-10.0, -40.0, -10.2, -40.2)[2] is False   # gap is 30


def test_a_later_minute_does_not_change_the_reading():
    m = 600
    at = {m: (-10.0, -40.0), m - 5: (-30.0, -80.0), m + 1: (0.0, 0.0)}
    assert A.reading(at, m) == (True, False, False)


def test_a_name_is_not_dropped_for_how_many_bars_the_finished_day_has():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "pr_approach.py")).read()
    body = src.split("def day_job(", 1)[1].split("\ndef ", 1)[0]
    assert "< 30" not in body


def test_a_missing_five_minute_reading_is_dropped():
    assert A.reading({600: (-10.0, -40.0)}, 600) is None
    assert A.flags(float("nan"), -40.0, -30.0, -80.0) is None


def test_three_minute_return_starts_at_the_next_open():
    sip = {601: (100.0, 101.0), 602: (101.0, 102.0), 603: (102.0, 103.0)}
    assert math.isclose(E.markout(sip, 601, 3), 103.0 / 100.0 - 1.0)
    assert A.HOLD == 3 and A.LAG == 5 and A.APART == 15.0
    assert A.SIG_START == 9 * 60 + 35 and A.SIG_END == 15 * 60 + 56


def _day(mean, n, excess, cross, n_up=None):
    return {"cnt": {"sampled": 10, "skipped_short": 0, "indicator_fail": 0,
                    "no_history": 0, "minutes": 100},
            "approach": {"mean": mean, "n": n, "n_up": n if n_up is None else n_up,
                         "excess": excess, "cross": cross},
            "leaving": {"mean": None, "n": 0, "n_up": 0, "excess": None, "cross": None},
            "flat": {"mean": None, "n": 0, "n_up": 0, "excess": None, "cross": None}}


def _half(gross0, ex0, cross0, n_days_month, month, n_each=40, bump=1e-5):
    return {f"2026-{month}-{i + 1:02d}": _day(gross0 + i * bump, n_each, ex0 + i * bump / 10, cross0 + i * bump / 10)
            for i in range(n_days_month)}


def test_the_grade_is_net_of_10bp_on_each_half_and_ignores_the_pictures():
    # June is half A (on or before 2026-07-23). August is half B. 20 bp gross clears 10 bp.
    per = {}
    per.update(_half(0.002, 0.001, 0.0008, 30, "06"))
    per.update(_half(0.002, 0.001, 0.0008, 30, "08"))
    assert A.grade_of(per, sorted(per)) == "PROMISING"
    # 4 bp gross is -6 bp after the 10 bp toll.
    thin = {}
    thin.update(_half(0.0004, 0.001, 0.0008, 30, "06", bump=1e-6))
    thin.update(_half(0.0004, 0.001, 0.0008, 30, "08", bump=1e-6))
    assert A.grade_of(thin, sorted(thin)) == "FAIL"
    # One half below zero is FAIL. A negative excess on one half is FAIL.
    down = dict(per)
    down.update(_half(-0.002, 0.001, 0.0008, 30, "08"))
    assert A.grade_of(down, sorted(down)) == "FAIL"
    worse = dict(per)
    worse.update(_half(0.002, -0.001, 0.0008, 30, "08"))
    assert A.grade_of(worse, sorted(worse)) == "FAIL"
    # A short half is UNDERPOWERED, including when the means would otherwise pass.
    short = {k: v for k, v in per.items() if not k.startswith("2026-08-") or k <= "2026-08-10"}
    assert A.grade_of(short, sorted(short)) == "UNDERPOWERED"
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "pr_approach.py")).read()
    grade_body = src.split("def grade(", 1)[1].split("\ndef ", 1)[0]
    of_body = src.split("def grade_of(", 1)[1].split("\ndef ", 1)[0]
    collect_body = src.split("def _collect(", 1)[1].split("\ndef ", 1)[0]
    assert "0.0003" not in grade_body and "0.0003" not in of_body and "0.0003" not in collect_body
    assert "leaving" not in grade_body and "flat" not in grade_body
    assert "E.COST" in collect_body and '["net"]' in of_body and '["t_net"]' in of_body
    assert E.COST == 0.0010
