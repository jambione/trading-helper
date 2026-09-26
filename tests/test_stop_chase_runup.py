import pytest
import ai_positions as ap
from config import DEFAULT_CONFIG


def test_stop_chase_and_runup_defaults():
    # Verify stop chase is enabled and Run up -0.01 parameters are configured
    assert DEFAULT_CONFIG["ai_local_trail_time_decay_enabled"] is True
    assert DEFAULT_CONFIG["ai_local_trail_min_give_px"] == 0.0
    assert DEFAULT_CONFIG["ai_local_trail_decay_overtake"] is False

    # Cushion at last=$10.00 with min_give_px=0.0 is 1 cent
    cushion = ap._trail_min_cushion_px(10.0, 0.10, DEFAULT_CONFIG)
    assert cushion == pytest.approx(0.01)


def test_green_catchup_stop_chase_ratchets_to_one_cent():
    # Simulate a green position where price ran up to 10.50 from 10.00 entry
    # and has stayed idle without a new high
    cfg = {
        **DEFAULT_CONFIG,
        "ai_local_trail_time_decay_enabled": True,
        "ai_local_trail_decay_idle_sec": 8.0,
        "ai_local_trail_decay_step_r": 0.50,  # 0.5R = $0.10 per step
        "ai_local_trail_min_give_px": 0.0,
        "ai_local_trail_decay_overtake": False,
    }
    pos = {
        "symbol": "TEST",
        "entry_price": 10.00,
        "last_seen_price": 10.50,
        "local_stop_price": 9.80,
        "risk_per_share": 0.20,
        "peak_price": 10.50,
        "trail_decay_peak": 10.50,
        "trail_decay_last_step_at": 1000.0,
        "trail_decay_idle_since": 1000.0,
    }
    now = 1020.0  # 20s idle (> 8s idle_sec)

    # First decay raise (2 steps * $0.10 = $0.20 -> 9.80 + 0.20 = 10.00)
    new_stop = ap.green_catchup_raise(pos, cfg, now=now)
    assert new_stop is not None
    assert new_stop == pytest.approx(10.00)

    # Near the top: ceiling is last - 0.01 = 10.49
    pos["local_stop_price"] = 10.45
    pos["trail_decay_last_step_at"] = 1000.0
    new_stop2 = ap.green_catchup_raise(pos, cfg, now=now)
    assert new_stop2 is not None
    # Parks at 10.49 (Run up -0.01)
    assert new_stop2 == pytest.approx(10.49)
