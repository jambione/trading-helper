"""Unit tests for Option 2 Anti-Pullback Gate (ai_watch_anti_pullback_gate)."""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_entry_watch as ew
from tests.test_ai_entry_watch import _armable_rec, _last_cfg


@pytest.fixture(autouse=True)
def _clear_cache():
    ew._DAY_HIGH_VWAP_CACHE.clear()
    ew._DAY_HIGH_CACHE.clear()
    yield
    ew._DAY_HIGH_VWAP_CACHE.clear()
    ew._DAY_HIGH_CACHE.clear()


def test_anti_pullback_gate_disabled_by_default():
    """Gate is off by default and does not block any trade."""
    rec = _armable_rec()
    why = ew.anti_pullback_refusal(rec, "AAPL", 100.0, {})
    assert why is None


def test_anti_pullback_blocks_below_vwap_floor():
    """Refuses when price is below VWAP by more than 15 bp."""
    rec = _armable_rec()
    cfg = {"ai_watch_anti_pullback_gate": True}
    
    # VWAP = 100.0, 15 bp buffer floor = 99.85
    # Price at 99.80 is below floor (20 bp below VWAP)
    dummy_getter = lambda s, now=None: (100.1, 100.0)
    why = ew.anti_pullback_refusal(rec, "AAPL", 99.80, cfg, high_vwap_fn=dummy_getter)
    assert why == "below_vwap"
    assert "VWAP floor" in rec.get("block_detail", "")


def test_anti_pullback_allows_price_within_vwap_buffer():
    """Allows price that is slightly below VWAP (e.g. 8 bp below) within the 15 bp buffer."""
    rec = _armable_rec()
    cfg = {"ai_watch_anti_pullback_gate": True}
    
    # VWAP = 100.0, 15 bp buffer floor = 99.85
    # Price at 99.92 is only 8 bp below VWAP (above the 99.85 floor)
    dummy_getter = lambda s, now=None: (100.1, 100.0)
    why = ew.anti_pullback_refusal(rec, "AAPL", 99.92, cfg, high_vwap_fn=dummy_getter)
    assert why is None


def test_anti_pullback_blocks_deep_waterfall_drop_from_hod():
    """Refuses when price drops >= 2.0% from HOD even if still above VWAP."""
    rec = _armable_rec()
    cfg = {"ai_watch_anti_pullback_gate": True}
    
    # HOD = 100.0, VWAP = 90.0
    # Price = 97.5 (2.5% drop from HOD, well above VWAP 90.0)
    dummy_getter = lambda s, now=None: (100.0, 90.0)
    why = ew.anti_pullback_refusal(rec, "AAPL", 97.50, cfg, high_vwap_fn=dummy_getter)
    assert why == "anti_pullback_drop"
    assert "2.5% >= 2.0%" in rec.get("block_detail", "")


def test_anti_pullback_allows_shallow_momentum_pullback():
    """Allows shallow pullbacks (e.g. 1.2% drop from HOD) when above VWAP floor."""
    rec = _armable_rec()
    cfg = {"ai_watch_anti_pullback_gate": True}
    
    # HOD = 100.0, VWAP = 98.0
    # Price = 98.8 (1.2% drop from HOD, well above VWAP floor)
    dummy_getter = lambda s, now=None: (100.0, 98.0)
    why = ew.anti_pullback_refusal(rec, "AAPL", 98.80, cfg, high_vwap_fn=dummy_getter)
    assert why is None


def test_anti_pullback_fails_open_on_missing_data():
    """Gracefully permits entry if market data feed has not published bars yet."""
    rec = _armable_rec()
    cfg = {"ai_watch_anti_pullback_gate": True}
    
    dummy_getter = lambda s, now=None: (None, None)
    why = ew.anti_pullback_refusal(rec, "AAPL", 50.0, cfg, high_vwap_fn=dummy_getter)
    assert why is None


def test_should_arm_buy_integration_blocks_and_allows(monkeypatch):
    """Verifies that should_arm_buy integrates the gate on live entry decisions."""
    rec = _armable_rec()
    base_cfg = _last_cfg(ai_watch_anti_pullback_gate=True)
    
    # 1. Blocked by below_vwap (HOD is 10.05, drop is only 0.5%, but ask 10.00 < VWAP 10.10 - 15bp)
    monkeypatch.setattr(ew, "day_high_and_vwap_iex", lambda s, now=None: (10.05, 10.10))
    ok, why = ew.should_arm_buy(rec, ask=10.00, bid=9.99, cfg=base_cfg)
    assert not ok
    assert why == "below_vwap"
    
    # 2. Blocked by anti_pullback_drop (2.5% drop from HOD, well above VWAP 9.50)
    monkeypatch.setattr(ew, "day_high_and_vwap_iex", lambda s, now=None: (10.26, 9.50))
    ok, why = ew.should_arm_buy(rec, ask=10.00, bid=9.99, cfg=base_cfg)
    assert not ok
    assert why == "anti_pullback_drop"
    
    # 3. Passed when trending above VWAP and near HOD
    monkeypatch.setattr(ew, "day_high_and_vwap_iex", lambda s, now=None: (10.05, 9.90))
    ok, why = ew.should_arm_buy(rec, ask=10.00, bid=9.99, cfg=base_cfg)
    assert ok
    assert why in ("at_last", "last_presquare", "presquare", "ready", "in_zone", "last_overbought")


def test_day_high_and_vwap_caching():
    """Verifies day_high_and_vwap_iex caches readings across calls within ttl."""
    calls = []
    
    import pandas as pd
    from datetime import datetime
    from zoneinfo import ZoneInfo
    
    et = ZoneInfo("America/New_York")
    t0 = datetime(2026, 10, 30, 10, 0, tzinfo=et)
    
    df = pd.DataFrame({
        "high": [10.5, 10.6],
        "low": [10.1, 10.2],
        "close": [10.4, 10.5],
        "volume": [1000, 2000],
    }, index=[datetime(2026, 10, 30, 9, 31, tzinfo=et), datetime(2026, 10, 30, 9, 32, tzinfo=et)])
    
    def fake_fetch(cl, sym, bar_cfg):
        calls.append(sym)
        return df
        
    t_now = t0.timestamp()
    hod1, vwap1 = ew.day_high_and_vwap_iex("TEST", now=t_now, ttl=60.0, fetch_bars_fn=fake_fetch)
    assert len(calls) == 1
    assert hod1 == 10.6
    assert vwap1 is not None
    
    # Second call within 60s uses cache
    hod2, vwap2 = ew.day_high_and_vwap_iex("TEST", now=t_now + 30.0, ttl=60.0, fetch_bars_fn=fake_fetch)
    assert len(calls) == 1
    assert hod2 == hod1
    assert vwap2 == vwap1
    
    # Third call after TTL re-fetches
    hod3, vwap3 = ew.day_high_and_vwap_iex("TEST", now=t_now + 70.0, ttl=60.0, fetch_bars_fn=fake_fetch)
    assert len(calls) == 2
