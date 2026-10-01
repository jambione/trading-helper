"""%R Trend Exhaustion counts minutes, not IEX rows (FLY 2026-09-30)."""
import numpy as np
import pandas as pd

import signals


def _frame(minutes, closes):
    t = [pd.Timestamp("2026-09-30T14:00:00Z") + pd.Timedelta(minutes=m) for m in minutes]
    c = np.asarray(closes, float)
    return pd.DataFrame({"time": [x.strftime("%Y-%m-%dT%H:%M:%SZ") for x in t],
                         "open": c, "high": c + 0.01, "low": c - 0.01, "close": c, "volume": 1.0})


def test_no_gaps_matches_the_row_based_line():
    rng = np.random.default_rng(1)
    df = _frame(range(200), 10 + np.cumsum(rng.normal(0, 0.02, 200)))
    grid = signals._minute_grid_pr(df, 21, 7)
    rows = signals.williams_pr(df["high"], df["low"], df["close"], 21).ewm(span=7, adjust=False).mean()
    assert np.allclose(grid.dropna(), rows.dropna())


def test_a_thin_tape_reads_its_minutes_not_its_rows():
    # A high early on, then sparse prints (every 3rd minute) drifting down: 21
    # ROWS reach back 60 minutes and still see the high; 21 MINUTES do not.
    mins = list(range(0, 30)) + list(range(30, 200, 3))
    closes = [12.0] * 30 + list(np.linspace(11.0, 10.0, len(mins) - 30))
    df = _frame(mins, closes)
    grid = signals._minute_grid_pr(df, 21, 1).iloc[-1]
    rows = signals.williams_pr(df["high"], df["low"], df["close"], 21).iloc[-1]
    assert rows < grid                     # rows: deeper, dragged by the old range
    assert len(signals._minute_grid_pr(df, 21, 7)) == len(df)


def test_long_silences_are_not_filled():
    # 200-minute gap (overnight-like): rows after it are not 200 flat bars.
    closes = list(np.linspace(10.0, 10.5, 40)) + list(np.linspace(11.0, 10.6, 40))
    df = _frame(list(range(40)) + list(range(240, 280)), closes)
    out = signals._minute_grid_pr(df, 21, 1)
    assert len(out) == 80 and out.iloc[20:].notna().all()
    # with no fill across the gap, 21 minutes after it = 21 rows after it
    rows = signals.williams_pr(df["high"], df["low"], df["close"], 21)
    assert np.isclose(out.iloc[-1], rows.iloc[-1])


def test_no_time_column_falls_back_to_rows():
    df = _frame(range(150), np.linspace(10, 11, 150)).drop(columns=["time"])
    assert signals._minute_grid_pr(df, 21, 7) is None
    out = signals.compute_percent_r_exhaustion(df, {})
    assert out["l_percentR"].notna().any()
