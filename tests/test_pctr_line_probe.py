"""The engine %R line and the chart %R line are one series."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import pctr_line_probe as probe  # noqa: E402


def _minutes(n, start="2026-10-14T13:30:00Z"):
    t0 = pd.Timestamp(start)
    return [(t0 + pd.Timedelta(minutes=i)).isoformat() for i in range(n)]


def _climb(n=240):
    close = 10.0 + np.arange(n, dtype=float) * 0.02
    return pd.DataFrame({
        "time": _minutes(n),
        "open": close,
        "high": close + 0.05,
        "low": close - 0.05,
        "close": close,
        "volume": np.full(n, 1000.0),
    })


def _thin():
    mins = list(range(0, 30)) + list(range(30, 200, 3))
    close = np.array([12.0] * 30 + list(np.linspace(11.0, 10.0, len(mins) - 30)))
    t0 = pd.Timestamp("2026-09-30T14:00:00Z")
    return pd.DataFrame({
        "time": [(t0 + pd.Timedelta(minutes=m)).isoformat() for m in mins],
        "open": close,
        "high": close + 0.05,
        "low": close - 0.05,
        "close": close,
        "volume": np.full(len(mins), 1000.0),
    })


def test_gapless_engine_matches_the_chart_line():
    result = probe.compare(_climb())
    assert result["ok"], result["mismatches"]
    chart = result["chart"]
    engine = result["engine"]
    assert chart["fast"] is not None and chart["slow"] is not None
    assert engine["published_fast"] == round(chart["fast"], 1)
    assert engine["published_slow"] == round(chart["slow"], 1)
    assert result["arm_chart"] == result["arm_engine"]
    assert result["desk_local"]["src"] == "live"


def test_thin_tape_engine_still_matches_the_minute_chart():
    result = probe.compare(_thin())
    assert result["ok"], result["mismatches"]
    assert result["chart"]["slow"] is not None
    assert any("desk local" in note for note in result["notes"])


def test_short_window_is_clock_range_and_not_the_chart():
    df = _climb(8)
    local = probe.desk_local_line(df)
    assert local["src"] == "clock_range"
    assert local["slow"] is None
    opened, why, _seat = probe.arm_decision(
        fast=local["fast"], slow=-30.0, rising=True, src="clock_range",
    )
    assert opened is False
    assert why == "pctr_not_live_clock_range"


def test_empty_square_opens_and_a_full_square_does_not():
    opened, why, seat = probe.arm_decision(
        fast=-28.0, slow=-30.0, rising=True, slow_rising=True,
    )
    assert opened is True
    assert why == "presquare"
    assert seat == "pre_square"

    opened, why, seat = probe.arm_decision(
        fast=-10.0, slow=-8.0, rising=True, slow_rising=True,
        symbol="PCTRPROBE2",
    )
    assert opened is False
    assert why == "not_presquare"
    assert seat == "square"


def test_wire_flags_a_book_that_is_not_the_engine_line():
    engine = {"pctr": -28.0, "pctr_slow": -30.0, "pctr_rising": True}
    book = {"indicator": {"pctr": -28.0, "pctr_slow": -30.0, "pctr_rising": True, "pctr_src": "live"}}
    assert probe.wire_mismatches(engine, book) == []

    drifted = {"pctr": -40.0, "pctr_slow": -30.0, "pctr_rising": False, "pctr_src": "clock_range"}
    bad = probe.wire_mismatches(engine, drifted)
    assert any("clock_range" in item for item in bad)
    assert any("fast" in item for item in bad)
    assert any("rising" in item for item in bad)


def test_prepare_frame_gives_a_clock_to_a_datetime_index():
    n = 30
    close = np.linspace(10, 11, n)
    idx = pd.date_range("2026-10-14 09:30", periods=n, freq="min", tz="America/New_York")
    df = pd.DataFrame({"high": close + 0.1, "low": close - 0.1, "close": close}, index=idx)
    frame = probe.prepare_frame(df)
    assert "time" in frame.columns
    assert probe.chart_line(frame)["fast"] is not None
