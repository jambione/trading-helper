"""The latency screen decides whether an entry-gate search is worth running.

If it understates ``captured`` the desk goes hunting for a signal when the
real problem is that admission arrives after the move.
"""
import os
import sys
from datetime import datetime, timezone

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


def _rth_bar(t, o, day="2026-08-20"):
    """A bar inside RTH (14:xx UTC == 10:xx ET)."""
    return {"t": t, "day": day, "hm": (14, int(t // 60) % 60),
            "o": o, "h": o, "l": o, "c": o}


def test_decision_leg_is_admit_to_first_arm():
    base = 1787320000.0
    rows = [{"ts": base, "admit_ts": base, "arm_ok": False},
            {"ts": base + 600, "admit_ts": base, "arm_ok": True},
            {"ts": base + 900, "admit_ts": base, "arm_ok": True}]
    bars = [_rth_bar(base + 60 * i, 10.0 + i * 0.1) for i in range(20)]
    out = al.latency_chain(rows, None, bars, "2026-08-20")
    assert out["decision_min"] == pytest.approx(10.0)   # first arm, not last
    assert out["decision_pct"] > 0                       # price moved up


def test_execution_leg_is_arm_to_fill():
    base = 1787320000.0
    rows = [{"ts": base + 300, "admit_ts": base, "arm_ok": True}]
    bars = [_rth_bar(base + 60 * i, 10.0) for i in range(30)]
    out = al.latency_chain(rows, base + 1200, bars, "2026-08-20")
    assert out["exec_min"] == pytest.approx(15.0)


def test_premarket_arm_is_flagged_separately():
    """The wait for the open is a constraint, not a fixable delay."""
    pre = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc).timestamp()
    rth = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc).timestamp()
    bars = [_rth_bar(rth + 60 * i, 10.0) for i in range(10)]
    out = al.latency_chain(
        [{"ts": pre, "admit_ts": pre, "arm_ok": True}], rth, bars, "2026-08-20")
    assert out["arm_premarket"] is True
    out2 = al.latency_chain(
        [{"ts": rth, "admit_ts": rth, "arm_ok": True}], rth + 600, bars,
        "2026-08-20")
    assert out2["arm_premarket"] is False


def test_rth_check_uses_the_clock_not_the_tape():
    """A thin name printing no bar at 10:15 must not read as pre-market."""
    quiet = datetime(2026, 8, 20, 14, 15, tzinfo=timezone.utc).timestamp()
    assert al._is_rth_ts(quiet) is True
    early = datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc).timestamp()
    assert al._is_rth_ts(early) is False


def test_missing_arm_or_fill_yields_none_not_zero():
    base = 1787320000.0
    bars = [_rth_bar(base + 60 * i, 10.0) for i in range(10)]
    never = al.latency_chain(
        [{"ts": base, "admit_ts": base, "arm_ok": False}], None, bars,
        "2026-08-20")
    assert never["decision_min"] is None
    assert never["exec_min"] is None


def test_out_of_order_timestamps_are_refused():
    """A fill before its arm is bad data, not a negative latency."""
    base = 1787320000.0
    bars = [_rth_bar(base + 60 * i, 10.0) for i in range(10)]
    out = al.latency_chain(
        [{"ts": base + 600, "admit_ts": base, "arm_ok": True}], base + 60,
        bars, "2026-08-20")
    assert out["exec_min"] is None


def test_threshold_differs_by_source():
    cfg = {"ai_watch_min_pct_change": 50.0,
           "ai_watch_trending_min_pct_change": 15.0}
    assert al.threshold_for("momentum", cfg) == 50.0
    assert al.threshold_for("trending", cfg) == 15.0
    assert al.threshold_for("research", cfg) == 50.0
