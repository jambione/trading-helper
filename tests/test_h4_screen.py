"""H4 daily-hold simulator — no Alpaca."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import h4_screen as h4  # noqa: E402
import desk_null as N  # noqa: E402


def _ohlc(days, opens, lows, closes):
    return [
        {"date": d, "open": o, "high": max(o, c), "low": lo, "close": c}
        for d, o, lo, c in zip(days, opens, lows, closes)
    ]


def test_simulate_hold_takes_stop_then_skips_to_next():
    bars = _ohlc(
        ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"],
        [10.0, 9.7, 10.2, 10.4],
        [9.5, 9.6, 10.1, 10.3],  # day0 low 9.5 hits 2% stop at 9.80
        [9.8, 9.9, 10.3, 10.5],
    )
    swings = h4.simulate_hold(bars, hold_days=2, stop_pct=2.0, haircut_pct=0.20)
    assert swings[0]["stopped"] is True
    assert swings[0]["fwd"] == pytest.approx(-2.0)
    # next entry is the day after the stop (index 1)
    assert swings[1]["entry_date"] == "2026-08-11"


def test_simulate_hold_hold_to_close_without_stop():
    bars = _ohlc(
        ["2026-08-10", "2026-08-11", "2026-08-12"],
        [10.0, 10.1, 10.2],
        [9.9, 10.0, 10.1],
        [10.1, 10.3, 10.4],
    )
    swings = h4.simulate_hold(bars, hold_days=2, stop_pct=2.0, haircut_pct=0.20)
    assert len(swings) == 1
    assert swings[0]["stopped"] is False
    # entry 10.0, exit close of day1 10.3 → +3.0%, net +2.80
    assert abs(swings[0]["fwd"] - 3.0) < 1e-9
    assert abs(swings[0]["net"] - 2.80) < 1e-9


def test_attach_bench_and_verdict_fail_when_under_spy():
    swings = [
        {"entry_date": "2026-08-10", "exit_date": "2026-08-11",
         "fwd": 0.1, "net": -0.1, "day": "2026-08-10"},
    ]
    spy = [
        {"date": "2026-08-10", "open": 100.0, "close": 100.0},
        {"date": "2026-08-11", "open": 100.0, "close": 101.0},
    ]
    scored = h4.attach_bench(swings, spy)
    assert scored[0]["eligible"] == 1.0  # SPY +1% over the window
    null_scores = h4._to_null_scores(scored)
    assert N.verdict(null_scores) in ("UNDERPOWERED", "FAIL", "EMPTY")
