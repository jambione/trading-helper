import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from ai_catalyst_thrust import (
    is_catalyst_thrust_eligible,
    compute_open_thrust_initial_stop,
    compute_open_thrust_ratchet_stop,
)
import ai_positions

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

    # Wednesday 09:30:30 ET (Bar 0 forming)
    dt_b0 = datetime(2026, 11, 4, 9, 30, 30, tzinfo=ET)
    ok, why, _ = is_catalyst_thrust_eligible(record, cfg, now=dt_b0.timestamp())
    assert not ok
    assert why == "bar0_forming"

    # Wednesday 09:31 ET (inside window, Bar 0 closed)
    dt_valid = datetime(2026, 11, 4, 9, 31, 0, tzinfo=ET)
    ok, why, details = is_catalyst_thrust_eligible(record, cfg, now=dt_valid.timestamp())
    assert ok
    assert why == "catalyst_open_thrust"
    assert details["symbol"] == "NVDA"
    assert details["gap_pct"] == 10.0

    # Wednesday 09:34 ET (window expired, no late chasing)
    dt_late = datetime(2026, 11, 4, 9, 34, 0, tzinfo=ET)
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


def test_catalyst_thrust_bar0_green_and_doji_filters():
    cfg = {"ai_catalyst_thrust_enabled": True}
    dt_valid = datetime(2026, 11, 4, 9, 31, 0, tzinfo=ET).timestamp()

    # Red bar 0 (close < open)
    red_bars = [{"o": 100.0, "h": 101.0, "l": 98.0, "c": 99.0}]
    rec = {"symbol": "FAIL", "price": 99.0, "gap_pct": 10.0, "source": "ai_catalyst"}
    ok, why, _ = is_catalyst_thrust_eligible(rec, cfg, now=dt_valid, bars=red_bars)
    assert not ok
    assert why == "bar0_red"

    # Doji bar 0 (close == open)
    doji_bars = [{"o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0}]
    ok, why, _ = is_catalyst_thrust_eligible(rec, cfg, now=dt_valid, bars=doji_bars)
    assert not ok
    assert why == "bar0_doji"

    # Green bar 0 (close > open)
    green_bars = [{"o": 100.0, "h": 102.0, "l": 99.5, "c": 101.5}]
    rec_ok = {"symbol": "WIN", "price": 101.5, "gap_pct": 10.0, "source": "ai_catalyst"}
    ok, why, details = is_catalyst_thrust_eligible(rec_ok, cfg, now=dt_valid, bars=green_bars)
    assert ok
    assert why == "catalyst_open_thrust"
    assert details["bar0_low"] == 99.5
    assert details["initial_stop"] == 99.5  # 99.5 > 101.5 * 0.98 (99.47)

    # Upper blow-off wick rejection (Condition 1: close < top 35% of bar range)
    wick_bars = [{"o": 100.0, "h": 105.0, "l": 99.5, "c": 100.5}]
    rec_wick = {"symbol": "WICK", "price": 100.5, "gap_pct": 10.0, "source": "ai_catalyst"}
    ok, why, _ = is_catalyst_thrust_eligible(rec_wick, cfg, now=dt_valid, bars=wick_bars)
    assert not ok
    assert "bar0_wick_rejection" in why


def test_compute_open_thrust_initial_stop():
    entry_px = 100.0
    # Tight bar0_low (99.5): initial stop is 99.5
    assert compute_open_thrust_initial_stop(entry_px, bar0_low=99.5) == 99.5

    # Wide bar0_low (96.0): 2% catastrophic floor takes over (98.0)
    assert compute_open_thrust_initial_stop(entry_px, bar0_low=96.0) == 98.0

    # Missing bar0_low: default 2% floor = 98.0
    assert compute_open_thrust_initial_stop(entry_px, bar0_low=None) == 98.0


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


def test_catalyst_thrust_premarket_news_timestamp_verification():
    cfg = {"ai_catalyst_thrust_enabled": True}
    dt_valid = datetime(2026, 11, 4, 9, 31, 0, tzinfo=ET)
    now_ts = dt_valid.timestamp()

    # Raw discord / screener mover without catalyst
    raw_mover = {"symbol": "RAW", "price": 50.0, "gap_pct": 10.0, "source": "discord"}
    ok, why, _ = is_catalyst_thrust_eligible(raw_mover, cfg, now=now_ts)
    assert not ok
    assert why == "no_verified_premarket_catalyst"

    # Premarket news published at 08:30 ET (before 09:25 ET cutoff) with bullish keyword
    news_cache = {
        "GOOD": [
            {
                "ts": datetime(2026, 11, 4, 8, 30, 0, tzinfo=ET).timestamp(),
                "headline": "GOOD Reports Q3 Earnings Beat and Raises Guidance",
            }
        ]
    }
    with patch("news_feed.load_cache", return_value=news_cache):
        good_cand = {"symbol": "GOOD", "price": 50.0, "gap_pct": 10.0, "source": "movers"}
        ok, why, details = is_catalyst_thrust_eligible(good_cand, cfg, now=now_ts)
        assert ok
        assert why == "catalyst_open_thrust"

    # News published at 09:28 ET (late, past 09:25 ET cutoff) -> rejected
    late_news_cache = {
        "LATE": [
            {
                "ts": datetime(2026, 11, 4, 9, 28, 0, tzinfo=ET).timestamp(),
                "headline": "LATE Announces Strategic Partnership",
            }
        ]
    }
    with patch("news_feed.load_cache", return_value=late_news_cache):
        late_cand = {"symbol": "LATE", "price": 50.0, "gap_pct": 10.0, "source": "movers"}
        ok, why, _ = is_catalyst_thrust_eligible(late_cand, cfg, now=now_ts)
        assert not ok
        assert why == "no_verified_premarket_catalyst"

    # Premarket news with bearish keywords (offering / dilution) -> rejected
    bearish_cache = {
        "DILUTE": [
            {
                "ts": datetime(2026, 11, 4, 8, 0, 0, tzinfo=ET).timestamp(),
                "headline": "DILUTE Announces Proposed Public Offering of Common Stock",
            }
        ]
    }
    with patch("news_feed.load_cache", return_value=bearish_cache):
        dilute_cand = {"symbol": "DILUTE", "price": 50.0, "gap_pct": 10.0, "source": "movers"}
        ok, why, _ = is_catalyst_thrust_eligible(dilute_cand, cfg, now=now_ts)
        assert not ok
        assert why == "no_verified_premarket_catalyst"


def test_catalyst_thrust_position_management_time_stop():
    """Verify that a thrust position is closed out at the 15-minute mark (900 seconds)."""
    t0 = time.time()
    pos = {
        "strategy": "thrust",
        "zone_kind": "catalyst_thrust",
        "entry_confirmed": True,
        "entry_price": 50.00,
        "entry_time": t0 - 905.0,  # 905 seconds ago (> 900s)
        "local_stop_price": 49.00,
        "max_hold_sec": 900.0,
    }
    events = []
    exit_why = {}

    with patch("alpaca_trader.cancel_open_orders") as mock_cancel, \
         patch("alpaca_trader.close_out", return_value={"order_id": "ord_close_time"}) as mock_close:

        changed, closed = ai_positions._thrust_exit_tick(
            "WIN", pos, trigger=52.00, events=events, exit_why=exit_why
        )

        assert changed is True
        assert closed is True
        assert pos["closing_reason"] == "thrust_time_stop"
        mock_cancel.assert_called_once_with("WIN")
        mock_close.assert_called_once_with("WIN")


def test_catalyst_thrust_position_management_ratchet_and_breach():
    """Verify that a thrust position ratchets stop and closes on stop breach."""
    t0 = time.time()
    pos = {
        "strategy": "thrust",
        "zone_kind": "catalyst_thrust",
        "entry_confirmed": True,
        "entry_price": 100.00,
        "entry_time": t0 - 120.0,
        "local_stop_price": 98.00,
        "stop_price": 98.00,
        "max_hold_sec": 900.0,
        "bar0_low": 98.00,
    }
    events = []
    exit_why = {}

    # Trigger above stop (101.00) -> stays open
    changed, closed = ai_positions._thrust_exit_tick(
        "RUN", pos, trigger=101.00, events=events, exit_why=exit_why
    )
    assert not closed

    # Trigger at or below stop (97.90 <= 98.00) -> closes on stop breach
    with patch("alpaca_trader.cancel_open_orders") as mock_cancel, \
         patch("alpaca_trader.close_out", return_value={"order_id": "ord_close_stop"}) as mock_close:

        changed, closed = ai_positions._thrust_exit_tick(
            "RUN", pos, trigger=97.90, events=events, exit_why=exit_why
        )
        assert changed is True
        assert closed is True
        assert pos["closing_reason"] == "thrust_stop"
        mock_cancel.assert_called_once_with("RUN")
        mock_close.assert_called_once_with("RUN")


def test_catalyst_thrust_breakeven_lock_at_30bp():
    """Verify that hitting +30 bp MFE locks stop at entry + $0.01 (Condition 2)."""
    t0 = time.time()
    pos = {
        "strategy": "thrust",
        "zone_kind": "catalyst_thrust",
        "entry_confirmed": True,
        "entry_price": 100.00,
        "entry_time": t0 - 60.0,
        "local_stop_price": 98.00,
        "stop_price": 98.00,
        "peak_price": 100.40,  # +40 bp MFE (> +30 bp threshold)
        "max_hold_sec": 900.0,
    }
    events = []
    exit_why = {}
    cfg = {"ai_catalyst_thrust_be_at_bp": 30.0}

    changed, closed = ai_positions._thrust_exit_tick(
        "RUN", pos, trigger=100.20, events=events, exit_why=exit_why, cfg=cfg
    )
    assert changed is True
    assert not closed
    # Stop is raised to entry + 0.01 = 100.01
    assert pos["local_stop_price"] == 100.01
    assert pos["stop_price"] == 100.01
    assert any(e.get("event") == "thrust_breakeven_lock" for e in events)


def test_catalyst_thrust_exempt_from_earliest_entry_delay():
    """Verify that catalyst thrust entries can execute at 09:31 ET even when ai_entry_earliest_time is 09:45."""
    import ai_entry_watch as aew
    cfg = {
        "ai_catalyst_thrust_enabled": True,
        "ai_entry_earliest_time": "09:45",
        "ai_sod_liquidate_enabled": False,
    }
    # 09:32:00 ET
    dt_open = datetime(2026, 11, 9, 9, 32, 0, tzinfo=ET)
    t_open = dt_open.timestamp()

    # entry_window_open is False for regular scalp trades
    assert aew.entry_window_open(cfg, t_open) is False

    # But trading_hours_active is True during market hours
    assert aew.trading_hours_active(cfg, t_open, market_open=True) is True


def test_catalyst_thrust_arms_despite_wide_sip_spread():
    """Verify that a catalyst thrust candidate arms even if the scalp spread check would fail."""
    import ai_entry_watch as aew
    cfg = {
        "ai_catalyst_thrust_enabled": True,
        "ai_watch_max_sip_spread_pct": 0.15,
    }
    dt_valid = datetime(2026, 11, 4, 9, 31, 15, tzinfo=ET)
    rec = {
        "symbol": "THRUST",
        "price": 50.0,
        "gap_pct": 12.0,
        "source": "movers",
        "status": "watching",
        "has_catalyst": True,
    }
    green_bars = [{"o": 49.0, "h": 50.5, "l": 48.9, "c": 50.2}]  # close_pos: 1.3 / 1.6 = 0.81 >= 0.65

    with patch("ai_catalyst_thrust.is_catalyst_thrust_eligible", return_value=(True, "bar1_inception", {"initial_stop": 48.9})), \
         patch("ai_entry_watch.sip_spread_pct", return_value=0.35):  # 35 bp spread (> 15 bp limit)
        ok, why = aew.should_arm_buy(rec, ask=50.2, bid=50.0, cfg=cfg, now=dt_valid.timestamp())
        assert ok is True
        assert why == "catalyst_thrust_bar1_inception"
