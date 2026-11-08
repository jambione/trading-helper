import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from ai_liquidity_maker import (
    is_liquidity_maker_eligible,
    compute_maker_asymmetric_exits,
)

ET = ZoneInfo("America/New_York")

def _generate_synthetic_bars(n_bars=20, base_price=100.0, drop_bp=0.0, vol_spike=1.0):
    bars = []
    for i in range(n_bars):
        vol = 1000.0
        px = base_price
        if i == n_bars - 1 and drop_bp > 0:
            px = base_price * (1.0 - drop_bp / 10000.0)
            vol = 1000.0 * vol_spike
        bars.append({
            "o": base_price,
            "h": max(base_price, px),
            "l": min(base_price, px),
            "c": px,
            "v": vol,
        })
    return bars

def test_liquidity_maker_disabled_by_default():
    rec = {"symbol": "MSFT", "price": 400.0, "dollar_volume": 50_000_000.0}
    cfg = {"ai_liquidity_maker_enabled": False}
    ok, why, _ = is_liquidity_maker_eligible(rec, cfg)
    assert not ok
    assert why == "liquidity_maker_disabled"

def test_liquidity_maker_session_time_window():
    rec = {"symbol": "MSFT", "price": 400.0, "dollar_volume": 50_000_000.0}
    cfg = {"ai_liquidity_maker_enabled": True}

    # Wednesday 09:35 ET (too early, opening noise)
    dt_early = datetime(2026, 11, 4, 9, 35, 0, tzinfo=ET)
    ok, why, _ = is_liquidity_maker_eligible(rec, cfg, now=dt_early.timestamp())
    assert not ok
    assert why == "pre_market_session_too_early"

    # Wednesday 15:45 ET (too late, closing auction)
    dt_late = datetime(2026, 11, 4, 15, 45, 0, tzinfo=ET)
    ok, why, _ = is_liquidity_maker_eligible(rec, cfg, now=dt_late.timestamp())
    assert not ok
    assert why == "market_session_too_late"

def test_liquidity_maker_air_pocket_detection():
    cfg = {
        "ai_liquidity_maker_enabled": True,
        "ai_liquidity_maker_drop_bp": 80.0,
        "ai_liquidity_maker_vol_mult": 3.0,
    }
    # Wednesday 10:30 ET
    dt_valid = datetime(2026, 11, 4, 10, 30, 0, tzinfo=ET).timestamp()
    rec = {"symbol": "AAPL", "price": 200.0, "dollar_volume": 100_000_000.0, "bid": 198.35}

    # Case 1: Drop is small (40 bp drop, 4x volume) -> refused
    bars_small_drop = _generate_synthetic_bars(20, base_price=200.0, drop_bp=40.0, vol_spike=4.0)
    ok, why, _ = is_liquidity_maker_eligible(rec, cfg, bars=bars_small_drop, now=dt_valid)
    assert not ok
    assert "drop_40.0bp<80.0bp" in why

    # Case 2: Drop is 90 bp, but volume is low (1.5x) -> refused
    bars_low_vol = _generate_synthetic_bars(20, base_price=200.0, drop_bp=90.0, vol_spike=1.5)
    ok, why, _ = is_liquidity_maker_eligible(rec, cfg, bars=bars_low_vol, now=dt_valid)
    assert not ok
    assert "vol_ratio_1.5x<3.0x" in why

    # Case 3: Genuine air pocket (90 bp drop, 4.0x volume spike) -> eligible!
    bars_panic = _generate_synthetic_bars(20, base_price=200.0, drop_bp=90.0, vol_spike=4.0)
    ok, why, details = is_liquidity_maker_eligible(rec, cfg, bars=bars_panic, now=dt_valid)
    assert ok
    assert why == "liquidity_air_pocket"
    assert details["symbol"] == "AAPL"
    assert details["order_style"] == "passive_limit"
    assert details["limit_price"] == 198.35
    assert details["target_bp"] == 40.0
    assert details["stop_bp"] == 30.0

def test_liquidity_maker_spy_regime_filter():
    cfg = {"ai_liquidity_maker_enabled": True}
    dt_valid = datetime(2026, 11, 4, 10, 30, 0, tzinfo=ET).timestamp()
    rec = {"symbol": "AAPL", "price": 200.0, "dollar_volume": 100_000_000.0, "bid": 198.35}
    bars_panic = _generate_synthetic_bars(20, base_price=200.0, drop_bp=90.0, vol_spike=4.0)

    # SPY dumping heavily (-50 bp in 15m) -> systemic sell-off, refuse!
    ok, why, _ = is_liquidity_maker_eligible(rec, cfg, bars=bars_panic, now=dt_valid, spy_return_15m=-0.0050)
    assert not ok
    assert "spy_regime_-50bp<-30bp" in why

def test_compute_maker_asymmetric_exits():
    fill_px = 100.0
    cfg = {
        "ai_liquidity_maker_target_bp": 40.0,
        "ai_liquidity_maker_stop_bp": 30.0,
    }
    exits = compute_maker_asymmetric_exits(fill_px, cfg)
    assert exits["fill_price"] == 100.0
    assert exits["target_price"] == 100.40
    assert exits["stop_price"] == 99.70
    assert exits["target_bp"] == 40.0
    assert exits["stop_bp"] == 30.0
