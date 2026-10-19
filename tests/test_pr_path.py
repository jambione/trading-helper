"""pr_path: readings, line history, and gap history. No market returns."""
import inspect
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import pr_path as R  # noqa: E402


def _at(m, fast, slow, **past):
    at = {m: (fast, slow)}
    at.update(past)
    return at


def _pair_eq(a, b) -> bool:
    if a is None or b is None or len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if isinstance(x, float) and isinstance(y, float) and math.isnan(x) and math.isnan(y):
            continue
        if x != y:
            return False
    return True


def test_features_keep_line_history_and_gap_history():
    m = 600
    at = {
        m: (-20.0, -40.0),          # gap now = 20
        m - 3: (-30.0, -45.0),      # gap 3m = 15
        m - 15: (-80.0, -40.0),     # gap 15m = -40
        m - 30: (-10.0, -90.0),     # gap 30m = 80
    }
    prev = (-50.0, -70.0)           # prior gap = 20
    row = R.features(at, prev, m)
    assert row is not None and len(row) == R.N_FEAT == 14
    assert "gap0" not in R.FEAT_NAMES and "gap15" not in R.FEAT_NAMES and "gapP" not in R.FEAT_NAMES
    assert "gap0sq" in R.FEAT_NAMES
    # Lines now, 15 minutes ago, and at the prior session.
    assert row[0] == -20.0 / 100 and row[1] == -40.0 / 100
    assert row[4] == -80.0 / 100 and row[5] == -40.0 / 100
    assert row[8] == prev[0] / 100 and row[9] == prev[1] / 100
    # Gap history is fast minus slow at each of those times, not its own column.
    assert math.isclose(row[0] - row[1], 20.0 / 100)
    assert math.isclose(row[2] - row[3], 15.0 / 100)
    assert math.isclose(row[4] - row[5], -40.0 / 100)
    assert math.isclose(row[6] - row[7], 80.0 / 100)
    assert math.isclose(row[8] - row[9], 20.0 / 100)
    assert math.isclose(row[12], (20.0 / 100) ** 2)
    # A later minute is not an input.
    at[m + 5] = (0.0, 0.0)
    assert R.features(at, prev, m) == row


def test_gap_history_is_the_line_difference():
    m = 600
    at = {
        m: (-20.0, -40.0),
        m - 3: (-30.0, -45.0),
        m - 15: (-80.0, -40.0),
        m - 30: (-10.0, -90.0),
    }
    row = R.features(at, (-50.0, -70.0), m)
    gaps = [(row[i] - row[i + 1]) * 100 for i in (0, 2, 4, 6, 8)]
    for got, want in zip(gaps, (20.0, 15.0, -40.0, 80.0, 20.0)):
        assert math.isclose(got, want)


def test_features_are_full_rank_without_a_gap_column():
    rng = np.random.default_rng(0)
    rows = []
    m = 600
    for _ in range(60):
        at = {m - lag: (float(rng.uniform(-100, 0)), float(rng.uniform(-100, 0)))
              for lag in (0, 3, 15, 30)}
        prev = (float(rng.uniform(-100, 0)), float(rng.uniform(-100, 0)))
        rows.append(R.features(at, prev, m))
    X = np.asarray(rows, np.float32).astype(np.float64)
    design = np.column_stack([np.ones(len(X)), X])
    assert np.linalg.matrix_rank(design) == R.N_FEAT + 1


def test_a_missing_lag_is_dropped_not_filled():
    m = 600
    at = {m: (-20.0, -40.0), m - 3: (-30.0, -45.0), m - 15: (-80.0, -40.0)}
    assert R.features(at, (-50.0, -70.0), m) is None


def test_weighted_quantile_counts_days_equally():
    # 100 minutes at 0 on one day, one minute at 100 on another. Unweighted, the 80th
    # percentile is 0. Day-equal weight puts half the mass on the single minute.
    values = np.array([0.0] * 100 + [100.0])
    weights = np.array([0.01] * 100 + [1.0])
    assert R.weighted_quantile(values, weights, 0.80) == 100.0


def test_bins_do_not_fold_the_outside_into_an_end():
    assert R.bin_lo(-100.0, -100.0, 0.0, 10.0) == -100.0
    assert R.bin_lo(0.0, -100.0, 0.0, 10.0) == -10.0
    assert R.bin_lo(-90.0, -100.0, 0.0, 10.0) == -90.0
    assert R.bin_lo(0.1, -100.0, 0.0, 10.0) is None
    assert R.bin_lo(-100.1, -100.0, 0.0, 10.0) is None
    assert R.bin_lo(100.0, -100.0, 100.0, 10.0) == 90.0
    assert R.bin_lo(100.1, -100.0, 100.0, 10.0) is None


def _grade(**over):
    base = dict(net10=0.0001, t_net=1.5, excess=0.0001, t_ex=1.5, cross=0.0001, t_cross=1.5,
                days=30, excess_days=30, cross_days=30, n=1000, singular=False, fail_share=0.0)
    base.update(over)
    return R.grade(**base)


def test_grade_bar():
    assert _grade() == "PROMISING"
    assert _grade(t_net=1.49) == "FAIL"
    assert _grade(net10=-0.0001, t_net=2.0) == "FAIL"
    assert _grade(excess=-0.0001) == "FAIL"
    assert _grade(cross=-0.0001) == "FAIL"
    assert _grade(t_net=None) == "UNDERPOWERED"
    assert _grade(t_cross=None) == "UNDERPOWERED"
    assert _grade(days=29) == "UNDERPOWERED"
    assert _grade(excess_days=29) == "UNDERPOWERED"
    assert _grade(cross_days=29) == "UNDERPOWERED"
    assert _grade(n=999) == "UNDERPOWERED"
    assert _grade(singular=True) == "UNDERPOWERED"
    assert _grade(fail_share=0.02) == "UNDERPOWERED"


def test_grade_does_not_read_three_bp():
    src = inspect.getsource(R.grade)
    assert "COST3" not in src and "0.0003" not in src
    # +2 bp net, already after the caller's 10 bp subtraction, still clears zero.
    assert _grade(net10=0.0002, t_net=2.0) == "PROMISING"


def test_excess_is_against_the_complement_not_the_whole_day():
    # 99 slice minutes at +100 bp, one complement minute at -3 bp.
    # Versus the complement the excess is about +103 bp. Versus every minute it is under 2 bp.
    y = np.array([0.0100] * 99 + [-0.0003])
    take = np.array([True] * 99 + [False])
    mu, ex = R.slice_day(y, take)
    assert abs(mu - 0.0100) < 1e-12
    assert abs(ex - (0.0100 - (-0.0003))) < 1e-12
    assert mu - float(y.mean()) < 0.0002
    assert ex > 0.01


def test_empty_complement_is_left_out_of_the_excess():
    y = np.array([0.01, 0.02])
    mu, ex = R.slice_day(y, np.array([True, True]))
    assert mu == 0.015
    assert ex is None


def test_cross_excess_is_other_names_at_the_same_minute():
    # Minute 600 has a slice name and another name. Minute 601 is slice-only and is skipped.
    y = np.array([0.0010, -0.0005, 0.0200])
    minute = np.array([600, 600, 601], dtype=np.int16)
    take = np.array([True, False, True])
    assert abs(R.cross_day(y, minute, take) - (0.0010 - (-0.0005))) < 1e-12


def _walk(start, n, price0):
    idx = pd.date_range(start, periods=n, freq="min", tz="America/New_York")
    px = price0 + np.linspace(0, 1, n)
    return pd.DataFrame({
        "time": idx.tz_convert("UTC"), "open": px, "high": px + 0.05, "low": px - 0.05,
        "close": px, "volume": np.full(n, 100.0),
    })


def _clocks(frame):
    times = pd.to_datetime(frame["time"], utc=True).dt.tz_convert("America/New_York")
    return times.dt.hour * 60 + times.dt.minute


def test_prior_session_reading_ignores_today_and_a_later_bar():
    prior = _walk("2026-04-27 09:30", 220, 20.0)
    today = _walk("2026-04-28 09:30", 120, 21.0)
    at, prev = R.line_path(today, prior)
    shocked = today.copy()
    shocked["close"] = shocked["close"] * 3
    shocked["high"] = shocked["high"] * 3
    at2, prev2 = R.line_path(shocked, prior)
    assert _pair_eq(prev2, prev)
    # The 10:00 reading uses 09:30, 09:45, and 09:57. A bar at 11:00 must not move it.
    shocked.loc[shocked.index[90], "close"] = 500.0
    at3, prev3 = R.line_path(shocked, prior)
    assert _pair_eq(prev3, prev)
    assert _pair_eq(at3[600], at2[600])
    assert _pair_eq(at3[570], at2[570])
    row = R.features(at, prev, 600)
    assert row is not None
    assert row[4] == at[585][0] / 100.0 and row[5] == at[585][1] / 100.0
    assert row[8] == prev[0] / 100.0 and row[9] == prev[1] / 100.0
    assert math.isclose(row[4] - row[5], (at[585][0] - at[585][1]) / 100.0)
    assert math.isclose(row[8] - row[9], (prev[0] - prev[1]) / 100.0)


def test_twenty_minute_gap_fill_does_not_see_the_later_print():
    prior = _walk("2026-04-27 09:30", 220, 20.0)
    head = _walk("2026-04-28 09:30", 31, 21.0)   # 09:30 through 10:00
    late_idx = pd.date_range("2026-04-28 10:20", periods=1, freq="min", tz="America/New_York")
    late = pd.DataFrame({
        "time": late_idx.tz_convert("UTC"),
        "open": [80.0], "high": [80.0], "low": [80.0], "close": [80.0], "volume": [5000.0],
    })
    today = pd.concat([head, late], ignore_index=True)
    at, prev = R.line_path(today, prior)
    at_cut, prev_cut = R.line_path(head, prior)
    assert _pair_eq(prev, prev_cut)
    # +5 from 10:00 matches the run with the 10:20 bar deleted. +15 is absent either way.
    assert _pair_eq(at[605], at_cut[605])
    assert 614 in at and 614 in at_cut
    assert 615 not in at and 615 not in at_cut
    assert 620 in at and 620 not in at_cut
    g = R.causal_grid(today)
    clocks = _clocks(g)
    flat = g.loc[clocks == 605].iloc[0]
    assert flat["close"] == head.iloc[-1]["close"]
    assert flat["close"] != 80.0
    assert g.loc[clocks == 620].iloc[0]["close"] == 80.0


def test_a_real_print_replaces_a_flat():
    head = _walk("2026-04-28 10:00", 1, 21.0)
    mid_idx = pd.date_range("2026-04-28 10:05", periods=1, freq="min", tz="America/New_York")
    mid = pd.DataFrame({
        "time": mid_idx.tz_convert("UTC"),
        "open": [30.0], "high": [31.0], "low": [29.0], "close": [30.5], "volume": [50.0],
    })
    g = R.causal_grid(pd.concat([head, mid], ignore_index=True))
    clocks = _clocks(g)
    row = g.loc[clocks == 605].iloc[0]
    assert row["close"] == 30.5 and row["volume"] == 50.0


def test_flats_stop_before_the_close_and_do_not_cross_the_overnight():
    prior = _walk("2026-04-27 15:50", 1, 20.0)
    today = _walk("2026-04-28 09:30", 1, 21.0)
    g = R.causal_grid(pd.concat([prior, today], ignore_index=True))
    times = pd.to_datetime(g["time"], utc=True).dt.tz_convert("America/New_York")
    clocks = times.dt.hour * 60 + times.dt.minute
    on_prior = times.dt.date.astype(str) == "2026-04-27"
    on_today = times.dt.date.astype(str) == "2026-04-28"
    assert (clocks[on_prior] < 16 * 60).all()
    assert (clocks[on_prior] == 15 * 60 + 59).any()
    assert not (clocks[on_prior] == 16 * 60).any()
    assert (clocks[on_today] >= 9 * 60 + 30).all()
    assert (clocks[on_today] == 9 * 60 + 44).any()
    assert not (clocks[on_today] == 9 * 60 + 45).any()
