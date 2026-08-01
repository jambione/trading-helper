"""The rvol denominator must come off the same tape as its numerator.

Measured on IEX over 7 sessions ending 2026-07-24, minute-sum ÷ daily-bar:

    AAPL/NVDA/SPY/TSLA (>1M/day)  1.000
    ABBV/AXP/AZN/REPL (~150-280K) 0.955-0.986
    DPZ (~70K)                    0.926
    MDGL (~20K)                   0.812
    RIGL (~13K)                   0.698   (worst session 0.475)
    EMAT (~2K)                    0.670

The daily bar carries odd-lot and off-exchange prints that appear in no minute
bar. So `minute_sum(today) / mean(daily_bar)` — what the funnel and the
dashboard both did — understated rvol by up to a third on exactly the thin,
low-float names this desk exists to find, and did it right under
`funnel_min_rvol`, where a true 2.0 reads 1.4 and the candidate is dropped.
"""
import os
import sys
from datetime import date

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools import morning_funnel as mf  # noqa: E402

TODAY = date(2026, 7, 24)


def minutes(sessions: dict, bars: int = 4) -> pd.DataFrame:
    """{date: total volume} spread over `bars` minute bars per session."""
    idx, vols = [], []
    for day, total in sorted(sessions.items()):
        for b in range(bars):
            idx.append(pd.Timestamp(day) + pd.Timedelta(hours=10, minutes=b))
            vols.append(total / bars)
    return pd.DataFrame({"volume": vols}, index=pd.DatetimeIndex(idx))


def _week(n, vol, upto=TODAY):
    return {upto - pd.Timedelta(days=i + 1).to_pytimedelta(): vol
            for i in range(n)}


# ── sum_by_session ───────────────────────────────────────────────────────────

def test_volume_is_totalled_per_session():
    df = minutes({date(2026, 7, 22): 500.0, date(2026, 7, 23): 900.0})
    assert mf.sum_by_session(df) == {date(2026, 7, 22): 500.0,
                                     date(2026, 7, 23): 900.0}


def test_an_empty_or_missing_frame_is_no_sessions():
    assert mf.sum_by_session(None) == {}
    assert mf.sum_by_session(pd.DataFrame()) == {}


def test_a_frame_without_a_volume_column_is_no_sessions():
    df = pd.DataFrame({"close": [1.0]}, index=pd.DatetimeIndex(["2026-07-23"]))
    assert mf.sum_by_session(df) == {}


# ── avg_session_volume ───────────────────────────────────────────────────────

def test_the_average_is_the_mean_of_completed_sessions():
    assert mf.avg_session_volume(
        minutes(_week(10, 1_000_000.0)), TODAY) == 1_000_000.0


def test_todays_partial_session_is_excluded():
    """Averaging a half-finished session in drags the denominator down all
    morning and inflates every rvol taken from it."""
    sessions = _week(10, 1_000_000.0)
    sessions[TODAY] = 12_000.0
    assert mf.avg_session_volume(minutes(sessions), TODAY) == 1_000_000.0


def test_too_little_history_is_no_average_rather_than_a_noisy_one():
    n = mf.MIN_AVG_SESSIONS
    assert mf.avg_session_volume(minutes(_week(n - 1, 5_000.0)), TODAY) is None
    assert mf.avg_session_volume(minutes(_week(n, 5_000.0)), TODAY) == 5_000.0


def test_only_the_last_avg_days_sessions_count():
    sessions = _week(10, 1_000_000.0)
    sessions[date(2026, 6, 1)] = 90_000_000.0      # one ancient blowout
    assert mf.avg_session_volume(
        minutes(sessions), TODAY, avg_days=10) == 1_000_000.0


def test_an_all_halted_history_is_none_not_zero():
    """A zero denominator would divide by zero in rvol_pair."""
    assert mf.avg_session_volume(minutes(_week(10, 0.0)), TODAY) is None


def test_no_frame_at_all_is_none():
    assert mf.avg_session_volume(None, TODAY) is None


# ── the ratio, end to end ────────────────────────────────────────────────────

def test_a_thin_name_no_longer_reads_low_enough_to_be_filtered_out():
    """RIGL's measured 0.698 ratio, applied to a genuine 2.0x pace. The old
    daily-bar denominator pushed it under funnel_min_rvol (1.5); the
    minute-based one reports the truth."""
    minute_avg = 12_533.0                      # summed from minute bars
    daily_avg = minute_avg / 0.698             # what the daily bar reported
    vol_so_far = minute_avg * 2.0 * mf.expected_fraction(20)

    fixed, _ = mf.rvol_pair(vol_so_far, minute_avg, 20)
    old, _ = mf.rvol_pair(vol_so_far, daily_avg, 20)

    assert fixed == pytest.approx(2.0, abs=0.01)
    assert old == pytest.approx(1.4, abs=0.05)
    assert old < 1.5 <= fixed                  # dropped vs kept


def test_a_liquid_name_is_unaffected():
    """AAPL measured 1.000, so the fix must be a no-op there — this is a
    correction to thin names, not a rescaling of everything."""
    avg = 1_733_127.0
    vol = avg * 3.0 * mf.expected_fraction(20)
    assert mf.rvol_pair(vol, avg, 20)[0] == pytest.approx(3.0, abs=0.01)


# ── evaluate refuses to guess ────────────────────────────────────────────────

def _daily(prev_close=10.0, days=12):
    idx = pd.bdate_range(end="2026-07-23", periods=days, tz=mf.ET)
    return pd.DataFrame({"open": prev_close, "high": prev_close * 1.02,
                         "low": prev_close * 0.98, "close": prev_close,
                         "volume": 1_000_000.0}, index=idx)


def _today_minutes(vol_per_bar=23_000, bars=20):
    idx = pd.date_range("2026-07-24 09:30", periods=bars, freq="1min",
                        tz=mf.ET)
    return pd.DataFrame({"open": 10.5, "high": 10.6, "low": 10.4,
                         "close": 10.55, "volume": float(vol_per_bar)},
                        index=idx)


NOW = pd.Timestamp("2026-07-24 09:50", tz=mf.ET).to_pydatetime()
KNOBS = mf.knobs_from_cfg({})


def test_no_average_means_no_rvol_rather_than_a_biased_one():
    row = mf.evaluate("X", _daily(), _today_minutes(), None, NOW, KNOBS)
    assert row["rvol"] is None


def test_a_missing_rvol_does_not_silently_fail_the_volume_gate():
    """rvol None must not read as 0 and reject the candidate — absence of
    evidence is not evidence of thin volume."""
    row = mf.evaluate("X", _daily(), _today_minutes(), None, NOW, KNOBS)
    assert "low rvol" not in row["rejects"]


def test_a_supplied_average_is_the_one_used():
    row = mf.evaluate("X", _daily(), _today_minutes(), None, NOW, KNOBS,
                      avg_vol=1_000_000.0)
    expected, _ = mf.rvol_pair(23_000.0 * 20, 1_000_000.0, 20)
    assert row["rvol"] == pytest.approx(expected)
