"""The latency screen decides whether an entry-gate search is worth running.

If it understates ``captured`` the desk goes hunting for a signal when the
real problem is that admission arrives after the move.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

al = pytest.importorskip("admission_latency")


def _bar(t, o, h, low, c, day="2026-08-20"):
    hm_h = 14 + (t // 3600)
    return {"t": t, "day": day, "hm": (hm_h, (t // 60) % 60),
            "o": o, "h": h, "l": low, "c": c}


def test_captured_is_one_when_the_move_finished_before_admission():
    """Prior close 10, run to 20 by admission, nothing left after."""
    bars = [_bar(0, 10.0, 20.0, 10.0, 20.0)]
    bars += [_bar(60 * i, 20.0, 20.0, 19.0, 19.5) for i in range(1, 10)]
    r = al.analyse_day(bars, "2026-08-20", admit_ts=60, pclose=10.0,
                       threshold=50.0)
    assert r["before"] == pytest.approx(100.0)
    assert r["after"] == pytest.approx(0.0)
    assert r["captured"] == pytest.approx(1.0)


def test_captured_is_half_when_the_desk_arrives_midway():
    """10 -> 15 -> 20 is half the move before admission, not 0.60.

    ``before`` is a percentage of the prior close and ``after`` one of the
    admit price. Summing those two percentages would read 0.60 here and
    overstate how much the desk still had in front of it.
    """
    bars = [_bar(0, 10.0, 15.0, 10.0, 15.0)]
    bars += [_bar(60 * i, 15.0, 20.0, 15.0, 20.0) for i in range(1, 5)]
    r = al.analyse_day(bars, "2026-08-20", admit_ts=60, pclose=10.0,
                       threshold=50.0)
    assert r["before"] == pytest.approx(50.0)
    assert r["after"] == pytest.approx(100.0 * (20.0 - 15.0) / 15.0)
    assert r["captured"] == pytest.approx(0.50)


def test_no_runup_means_no_captured_number():
    """Admitted below the prior close: there is no missed run to report."""
    bars = [_bar(60 * i, 9.0, 9.2, 8.8, 9.0) for i in range(10)]
    r = al.analyse_day(bars, "2026-08-20", admit_ts=60, pclose=10.0,
                       threshold=50.0)
    assert r["before"] < 0
    assert r["captured"] is None


def test_latency_measures_threshold_cross_to_admit():
    bars = [_bar(0, 10.0, 10.5, 10.0, 10.4)]          # below +50%
    bars += [_bar(60, 10.4, 15.5, 10.4, 15.4)]        # crosses +50% here
    bars += [_bar(60 * i, 15.4, 15.6, 15.0, 15.4) for i in range(2, 12)]
    r = al.analyse_day(bars, "2026-08-20", admit_ts=60 * 6, pclose=10.0,
                       threshold=50.0)
    assert r["latency_min"] == pytest.approx(5.0)


def test_latency_is_none_when_threshold_never_cleared():
    bars = [_bar(60 * i, 10.0, 10.2, 9.9, 10.0) for i in range(10)]
    r = al.analyse_day(bars, "2026-08-20", admit_ts=60, pclose=10.0,
                       threshold=50.0)
    assert r["latency_min"] is None


def test_thin_tape_returns_nothing_rather_than_a_number():
    assert al.analyse_day([], "2026-08-20", 0, 10.0, 50.0) is None
    bars = [_bar(0, 10.0, 10.0, 10.0, 10.0)]
    assert al.analyse_day(bars, "2026-08-20", 0, 10.0, 50.0) is None


def test_prior_close_takes_the_session_before():
    daily = [{"date": "2026-08-18", "close": 8.0},
             {"date": "2026-08-19", "close": 9.0},
             {"date": "2026-08-20", "close": 20.0}]
    assert al.prior_close(daily, "2026-08-20") == 9.0
    assert al.prior_close(daily, "2026-08-19") == 8.0
    assert al.prior_close(daily, "2026-08-18") is None


def test_threshold_differs_by_source():
    cfg = {"ai_watch_min_pct_change": 50.0,
           "ai_watch_trending_min_pct_change": 15.0}
    assert al.threshold_for("momentum", cfg) == 50.0
    assert al.threshold_for("trending", cfg) == 15.0
    assert al.threshold_for("research", cfg) == 50.0
