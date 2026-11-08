import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from ai_catalyst_thrust import (
    is_catalyst_thrust_eligible,
    compute_open_thrust_ratchet_stop,
)

ET = ZoneInfo("America/New_York")

def test_catalyst_thrust_disabled_by_default():
    record = {"symbol": "NVDA", "price": 120.0, "gap_pct": 10.0, "source": "ai_catalyst"}
    cfg = {"ai_catalyst_thrust_enabled": False}
    ok, why, _ = is_catalyst_thrust_eligible(record, cfg)
    assert not ok
    assert why == "catalyst_thrust_disabled"

def test_catalyst_thrust_time_window_enforcement():
    record = {"symbol": "NVDA", "price": 120.0, "gap_pct": 10.0, "source": "ai_catalyst"}
    cfg = {"ai_catalyst_thrust_enabled": True}

    # Wednesday 09:25 ET (pre-open)
    dt_early = datetime(2026, 11, 4, 9, 25, 0, tzinfo=ET)
    ok, why, _ = is_catalyst_thrust_eligible(record, cfg, now=dt_early.timestamp())
    assert not ok
    assert why == "pre_open"

    # Wednesday 09:31 ET (inside window)
    dt_valid = datetime(2026, 11, 4, 9, 31, 0, tzinfo=ET)
    ok, why, details = is_catalyst_thrust_eligible(record, cfg, now=dt_valid.timestamp())
    assert ok
    assert why == "catalyst_open_thrust"
    assert details["symbol"] == "NVDA"
    assert details["gap_pct"] == 10.0

    # Wednesday 09:40 ET (window expired)
    dt_late = datetime(2026, 11, 4, 9, 40, 0, tzinfo=ET)
    ok, why, _ = is_catalyst_thrust_eligible(record, cfg, now=dt_late.timestamp())
    assert not ok
    assert why == "window_expired"

def test_catalyst_thrust_gap_and_price_floors():
    cfg = {
        "ai_catalyst_thrust_enabled": True,
        "ai_catalyst_thrust_min_gap_pct": 8.0,
        "ai_catalyst_thrust_min_price": 20.0,
    }
    dt_valid = datetime(2026, 11, 4, 9, 31, 0, tzinfo=ET).timestamp()

    # Gap too low
    low_gap = {"symbol": "AAPL", "price": 150.0, "gap_pct": 5.0, "source": "ai_catalyst"}
    ok, why, _ = is_catalyst_thrust_eligible(low_gap, cfg, now=dt_valid)
    assert not ok
    assert "gap_5.0%<8.0%" in why

    # Price too low
    cheap_stock = {"symbol": "CHEAP", "price": 15.0, "gap_pct": 12.0, "source": "ai_catalyst"}
    ok, why, _ = is_catalyst_thrust_eligible(cheap_stock, cfg, now=dt_valid)
    assert not ok
    assert "price_$15.00<$20.00" in why

def test_catalyst_thrust_bar0_green_filter():
    cfg = {"ai_catalyst_thrust_enabled": True}
    dt_valid = datetime(2026, 11, 4, 9, 31, 0, tzinfo=ET).timestamp()

    # Red bar 0 (close < open)
    red_bars = [{"o": 100.0, "h": 101.0, "l": 98.0, "c": 99.0}]
    rec = {"symbol": "FAIL", "price": 99.0, "gap_pct": 10.0, "source": "ai_catalyst"}
    ok, why, _ = is_catalyst_thrust_eligible(rec, cfg, now=dt_valid, bars=red_bars)
    assert not ok
    assert why == "bar0_red"

    # Green bar 0 (close > open)
    green_bars = [{"o": 100.0, "h": 102.0, "l": 99.5, "c": 101.5}]
    rec_ok = {"symbol": "WIN", "price": 101.5, "gap_pct": 10.0, "source": "ai_catalyst"}
    ok, why, _ = is_catalyst_thrust_eligible(rec_ok, cfg, now=dt_valid, bars=green_bars)
    assert ok
    assert why == "catalyst_open_thrust"

def test_compute_open_thrust_ratchet_stop():
    entry_px = 100.0
    # Initial floor is 2% below entry = 98.0
    stop0 = compute_open_thrust_ratchet_stop(entry_px, None, [])
    assert stop0 == 98.0

    # Bar closes with low at 99.5 -> ratchet raises stop to 99.5
    bars = [
        {"o": 100.0, "h": 102.0, "l": 99.5, "c": 101.5},
        {"o": 101.5, "h": 103.0, "l": 101.0, "c": 102.5}, # active bar
    ]
    stop1 = compute_open_thrust_ratchet_stop(entry_px, stop0, bars)
    assert stop1 == 99.5

    # Subsequent bar dips low to 99.0 -> ratchet NEVER moves down
    bars_dip = [
        {"o": 101.5, "h": 102.0, "l": 99.0, "c": 100.0},
        {"o": 100.0, "h": 100.5, "l": 99.8, "c": 100.2},
    ]
    stop2 = compute_open_thrust_ratchet_stop(entry_px, stop1, bars_dip)
    assert stop2 == 99.5
