import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from ai_entry_watch import should_arm_buy

ET = ZoneInfo("America/New_York")

def test_should_arm_buy_catalyst_thrust_arm_integration():
    dt_thrust = datetime(2026, 11, 4, 9, 31, 30, tzinfo=ET).timestamp()
    rec = {
        "symbol": "TSLA",
        "status": "watching",
        "price": 250.0,
        "ask": 250.05,
        "bid": 249.95,
        "gap_pct": 9.5,
        "source": "ai_catalyst",
        "has_catalyst": True,
        "open": 248.0,
        "last_ask_ts": dt_thrust,
        "price_ts": dt_thrust,
    }
    cfg = {
        "ai_catalyst_thrust_enabled": True,
        "ai_catalyst_thrust_min_gap_pct": 8.0,
        "ai_catalyst_thrust_min_price": 20.0,
        "ai_watch_engine_stale_max_sec": 0, # bypass engine heartbeat in unit test
    }
    ok, why = should_arm_buy(rec, ask=250.05, bid=249.95, cfg=cfg, now=dt_thrust)
    assert ok
    assert "catalyst_thrust" in why
    assert "arm_details" in rec
    assert rec["arm_details"]["gap_pct"] == 9.5

