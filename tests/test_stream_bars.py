"""Bars built from what the desk observes, graded against the live path.

The whole point is a fair comparison: the same `_live_percent_r_line`, the
same parameters, only the bar source changed. So the rows this hands back
must match `clock_window_rows`' contract exactly — shape, clock discipline,
and the refusal to widen a 14-minute window into an hour and call it a %R.

It also must never throw. It runs inside the shadow-row builder, which
runs inside the entry poll, and a telemetry experiment may not stop the
desk.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

sb = pytest.importorskip("stream_bars")

T0 = 1_787_000_000.0
T0 = T0 - (T0 % 60.0)          # align to a minute boundary


@pytest.fixture(autouse=True)
def _clean():
    sb.reset()
    yield
    sb.reset()


# ---------------------------------------------------------------- aggregation

def test_one_minute_of_prices_becomes_one_bar():
    for i, px in enumerate((10.0, 10.5, 9.8, 10.2)):
        sb.observe("TEM", px, T0 + i * 10)
    rows, span = sb.window_rows("TEM", T0 + 50, length=14)
    assert rows == [(10.5, 9.8, 10.2)]      # high, low, close
    assert span == pytest.approx(60.0)


def test_prices_roll_into_separate_minutes():
    sb.observe("TEM", 10.0, T0 + 5)
    sb.observe("TEM", 11.0, T0 + 65)
    sb.observe("TEM", 12.0, T0 + 125)
    rows, _ = sb.window_rows("TEM", T0 + 130, length=14)
    assert [r[2] for r in rows] == [10.0, 11.0, 12.0]


def test_the_minute_in_progress_is_included():
    """A %R against the live price needs the current minute, not just closed."""
    sb.observe("TEM", 10.0, T0 + 5)
    rows, _ = sb.window_rows("TEM", T0 + 10, length=14)
    assert len(rows) == 1


def test_repeated_identical_prices_do_not_distort_the_bar():
    """The poll re-reads the same print many times between trades.

    Folding an unchanged value into high/low/close is idempotent, which is
    why sampling is safe here and why the counter is n_samples, not volume.
    """
    for i in range(30):
        sb.observe("TEM", 10.0, T0 + i)
    rows, _ = sb.window_rows("TEM", T0 + 30, length=14)
    assert rows == [(10.0, 10.0, 10.0)]
    assert sb.coverage("TEM")["samples"] == 30


def test_a_late_sample_for_a_closed_minute_is_dropped():
    sb.observe("TEM", 10.0, T0 + 5)
    sb.observe("TEM", 11.0, T0 + 65)
    sb.observe("TEM", 99.0, T0 + 10)        # late, belongs to a closed bar
    rows, _ = sb.window_rows("TEM", T0 + 70, length=14)
    assert max(r[0] for r in rows) == 11.0  # the 99 never landed


def test_millisecond_stamps_are_understood():
    sb.observe("TEM", 10.0, (T0 + 5) * 1000.0)
    assert sb.coverage("TEM")["bars"] == 1


# ---------------------------------------------------------------- clock window

def test_a_sparse_stretch_shortens_the_window_rather_than_widening_it():
    """The exact failure this exists to fix.

    Two prints an hour apart must NOT become a two-bar %R spanning an
    hour. The old bars fall outside the clock horizon and are dropped.
    """
    sb.observe("TEM", 10.0, T0)
    sb.observe("TEM", 11.0, T0 + 3600)
    rows, span = sb.window_rows("TEM", T0 + 3660, length=14)
    assert len(rows) == 1
    assert span == pytest.approx(60.0)


def test_the_window_never_returns_more_than_length_bars():
    for i in range(40):
        sb.observe("TEM", 10.0 + i * 0.01, T0 + i * 60)
    rows, _ = sb.window_rows("TEM", T0 + 40 * 60, length=14)
    assert len(rows) == 14


def test_an_unknown_symbol_has_no_window():
    rows, span = sb.window_rows("NOPE", T0, length=14)
    assert rows == []
    assert span is None


# ---------------------------------------------------------------- coverage

def test_coverage_counts_the_minutes_that_carried_nothing():
    """Density, honestly. p99 print gap is 66s, so gaps are expected."""
    sb.observe("TEM", 10.0, T0)
    sb.observe("TEM", 11.0, T0 + 60)
    sb.observe("TEM", 12.0, T0 + 240)       # skipped two minutes
    cov = sb.coverage("TEM")
    assert cov["bars"] == 3
    assert cov["span_min"] == pytest.approx(5.0)
    assert cov["empty_minutes"] == 2


def test_coverage_of_an_unseen_symbol_is_zero_not_an_error():
    cov = sb.coverage("NOPE")
    assert cov["bars"] == 0
    assert cov["empty_minutes"] is None


# ---------------------------------------------------------------- robustness

def test_junk_input_never_raises():
    for bad in (None, "", 0, -1, "abc", object()):
        sb.observe("TEM", bad, T0)
        sb.observe(bad, 10.0, T0)
    assert sb.coverage("TEM")["bars"] == 0


def test_a_missing_timestamp_falls_back_to_now():
    sb.observe("TEM", 10.0, None)
    assert sb.coverage("TEM")["bars"] == 1


# ---------------------------------------------------------------- fair compare

def test_the_rows_feed_the_live_percent_r_unchanged():
    """The comparison is only fair if the same function eats both sources."""
    import ai_entry_watch as ew
    for i in range(20):
        sb.observe("TEM", 10.0 + i * 0.05, T0 + i * 60)
    rows, _ = sb.window_rows("TEM", T0 + 20 * 60, length=14)
    got = ew._live_percent_r_line(rows, 11.0, 14, 7.0, 0.05, min_range=6)
    assert got is not None
    pctr, rising, falling, src = got
    assert -100.0 <= pctr <= 0.0
    assert src == "live"        # a full window, not clock_range
