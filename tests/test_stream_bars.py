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


# ---------------------------------------------------------------- shadow RSI

def test_the_extracted_rsi_reproduces_the_live_one_exactly():
    """The extraction must be behaviour-neutral.

    live_cm_rsi's loop was lifted into cm_rsi_series so the shadow path
    uses the same arithmetic instead of a second copy that can drift. If
    these ever disagree, the comparison this whole experiment rests on is
    measuring the refactor rather than the bar source.
    """
    import ai_entry_watch as ew
    closes = [10.0, 10.4, 10.1, 10.9, 11.3, 11.0, 11.6, 11.2, 11.9]
    got = ew.cm_rsi_series(closes, 2)
    # Recompute inline exactly as the original body did.
    alpha, up, down, want = 1.0 / 2, 0.0, 0.0, []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = (d if d > 0 else 0.0), (-d if d < 0 else 0.0)  # noqa: E741
        if i == 1:
            up, down = g, l
        else:
            up = alpha * g + (1.0 - alpha) * up
            down = alpha * l + (1.0 - alpha) * down
        want.append(100.0 if down == 0 else
                    (0.0 if up == 0 else 100.0 - (100.0 / (1.0 + up / down))))
    assert got == pytest.approx(want)


def test_rsi_needs_a_series_not_a_clock_window():
    """RMA smoothing carries the whole history, so a short slice is wrong.

    Needs a series that actually oscillates: on a monotonic rise every RSI
    is pinned at 100 (down == 0) and any slice agrees, which says nothing.
    """
    import ai_entry_watch as ew
    closes = [10.0, 11.0, 10.2, 11.4, 10.6, 11.9, 10.9, 12.3, 11.1, 12.8,
              11.4, 13.0, 11.8, 13.4, 12.0]
    long_s = ew.cm_rsi_series(closes, 2)
    short_s = ew.cm_rsi_series(closes[-5:], 2)
    assert long_s[-1] != pytest.approx(short_s[-1])


def test_a_flat_series_is_pinned_not_divided_by_zero():
    import ai_entry_watch as ew
    s = ew.cm_rsi_series([10.0] * 10, 2)
    assert s and all(v == 100.0 for v in s)


def test_too_few_closes_yield_no_series():
    import ai_entry_watch as ew
    assert ew.cm_rsi_series([10.0], 2) == []


def test_the_shadow_row_carries_both_stream_indicators():
    src = open(os.path.join(ROOT, "ai_entry_watch.py"), encoding="utf-8").read()
    i = src.index("def _shadow_row")
    body = src[i:src.index("\ndef ", i + 10)]
    for f in ("pctr_stream", "pctr_stream_src", "cm_rsi_stream",
              "cm_rsi_stream_rising", "stream_empty_min"):
        assert f'"{f}"' in body, f"{f} missing from the shadow row"


# ---------------------------------------------------------------- seed / live overlay

def test_seed_backfills_closed_minutes_without_clobbering_live():
    sb.observe("TEM", 12.0, T0 + 600)
    hist_rows = [(10.0, 9.0, 9.5), (11.0, 10.0, 10.5)]
    hist_ts = [T0, T0 + 60]
    n = sb.seed("TEM", hist_rows, hist_ts)
    assert n == 2
    rows, stamps = sb.ohlc_with_stamps("TEM")
    assert stamps[0] == pytest.approx(T0)
    assert rows[-1][2] == pytest.approx(12.0)
    # Second seed of the same minutes must not duplicate or overwrite live.
    n2 = sb.seed("TEM", hist_rows + [(99.0, 99.0, 99.0)],
                 hist_ts + [T0 + 600])
    assert n2 == 0
    rows2, _ = sb.ohlc_with_stamps("TEM")
    assert rows2[-1][2] == pytest.approx(12.0)


def test_stream_overlay_fills_a_sparse_iex_window():
    """21 IEX prints an hour apart are not a 1-minute %R; tape minutes are."""
    import ai_entry_watch as ew

    now = T0 + 21 * 60
    sparse = [(10.0 + i * 0.01, 9.9, 10.0) for i in range(21)]
    sparse_ts = [T0 + i * 600.0 for i in range(21)]  # 10 min apart
    with ew._ohlc_cache_lock:
        ew._ohlc_cache["RUM"] = (now, list(sparse))
        ew._ohlc_ts_cache["RUM"] = (now, list(sparse_ts))
    for i in range(22):
        sb.observe("RUM", 10.0 + i * 0.02, T0 + i * 60)
    cfg = {
        "rte_fast_length": 21,
        "ai_watch_db_bar_seconds": 60.0,
        "ai_watch_stream_bars_live": True,
        "ai_watch_db_bar_refresh_sec": 120.0,
    }
    rows = ew.symbol_ohlc("RUM", cfg, now)
    stamps = ew._cached_ohlc_stamps("RUM", cfg, now)
    assert stamps is not None
    # Recent minutes are 1m stream, not the 10-minute IEX gaps.
    assert stamps[-1] - stamps[-2] == pytest.approx(60.0)
    got = ew.live_exhaustion("RUM", 10.5, cfg, now)
    assert got is not None
    rec = {"symbol": "RUM"}
    assert ew.apply_live_exhaustion(rec, 10.5, cfg, now) is True
    assert rec["indicator"]["pctr_src"] == "live"
    assert rec["indicator"]["cm_rsi"] is not None
    assert rec["indicator"]["cm_rsi_src"] == "realtime"
