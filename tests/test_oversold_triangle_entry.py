import time
import pytest
import ai_entry_watch as ew
import ai_positions as ap
from config import DEFAULT_CONFIG


def _base_cfg():
    return {
        "ai_watch_exhaustion_rules": True,
        "ai_watch_exh_square_arm": True,
        "ai_watch_exh_oversold_triangle_arm": True,
        "ai_watch_require_exh_rising": True,
        "ai_watch_os_triangle_max_age_sec": 60.0,
        "rte_threshold": 20.0,
        "rte_confluence_max": 15.0,
        "ai_watch_exh_pre_thr": 35.0,
    }


def test_dual_r_os_tight_detection():
    cfg = _base_cfg()
    # Fast = -85, Slow = -82 -> both <= -80, gap = 3 <= 15
    rec = {"indicator": {"pctr": -85.0, "pctr_slow": -82.0}}
    both_os, tight, err = ew.dual_r_os_tight(rec, cfg)
    assert both_os is True
    assert tight is True
    assert err is None
    assert rec["indicator"]["pctr_os"] is True
    assert rec["indicator"]["pctr_os_tight"] is True

    # Both OS, but wide gap (gap = 18 > 15)
    rec2 = {"indicator": {"pctr": -98.0, "pctr_slow": -80.0}}
    both_os2, tight2, err2 = ew.dual_r_os_tight(rec2, cfg)
    assert both_os2 is True
    assert tight2 is False
    assert err2 is None

    # One line not OS
    rec3 = {"indicator": {"pctr": -75.0, "pctr_slow": -85.0}}
    both_os3, tight3, err3 = ew.dual_r_os_tight(rec3, cfg)
    assert both_os3 is False
    assert tight3 is True
    assert err3 is None

    # Missing slow
    rec4 = {"indicator": {"pctr": -85.0}}
    both_os4, tight4, err4 = ew.dual_r_os_tight(rec4, cfg)
    assert both_os4 is None
    assert tight4 is None
    assert err4 == "no_exhaustion_data"


def test_classify_exh_seat_os():
    cfg = _base_cfg()
    # Oversold squares: fast = -85, slow = -82
    rec = {"indicator": {"pctr": -85.0, "pctr_slow": -82.0, "pctr_rising": False}}
    cls, gap = ew.classify_exh_seat(rec, cfg)
    assert cls == "os_square"
    assert rec.get("exh_was_oversold") is True

    # Oversold triangle: was oversold, fast turned up to -75, slow = -82, tight & rising
    rec2 = {
        "exh_was_oversold": True,
        "indicator": {
            "pctr": -75.0,
            "pctr_slow": -82.0,
            "pctr_rising": True,
            "cm_rsi_rising": True,
        },
    }
    cls2, gap2 = ew.classify_exh_seat(rec2, cfg)
    assert cls2 == "os_triangle"
    assert gap2 == pytest.approx(7.0)


def test_oversold_triangle_entry_lifecycle():
    cfg = _base_cfg()
    rec = {
        "symbol": "XYZ",
        "indicator": {
            "pctr": -88.0,
            "pctr_slow": -85.0,
            "pctr_rising": False,
            "cm_rsi": 25.0,
            "cm_rsi_rising": False,
        },
    }
    t0 = 1000.0

    # Step 1: In oversold squares -> holds, sets latch, does NOT open
    ok1, why1 = ew.exhaustion_allows_buy(rec, cfg, now=t0)
    assert ok1 is False
    assert why1 == "oversold_squares"
    assert rec.get("exh_was_oversold") is True
    assert rec.get("os_square_since") == t0

    # Step 2: Still in squares, slow moves slightly -> still holds
    rec["indicator"]["pctr"] = -84.0
    rec["indicator"]["pctr_slow"] = -82.0
    rec["indicator"]["pctr_rising"] = True
    ok2, why2 = ew.exhaustion_allows_buy(rec, cfg, now=t0 + 5)
    assert ok2 is False
    assert why2 == "oversold_squares"

    # Step 3: Triangle forms! Fast leaves OS band (-75 > -80), slow = -80, rising, RSI rising
    rec["indicator"]["pctr"] = -75.0
    rec["indicator"]["pctr_slow"] = -80.0
    rec["indicator"]["pctr_rising"] = True
    rec["indicator"]["cm_rsi"] = 28.0
    rec["indicator"]["cm_rsi_rising"] = True

    ok3, why3 = ew.exhaustion_allows_buy(rec, cfg, now=t0 + 10)
    assert ok3 is True
    assert why3 == "oversold_triangle"
    assert rec.get("left_os_since") == t0 + 10


def test_oversold_triangle_refuses_when_rsi_not_rising():
    cfg = _base_cfg()
    rec = {
        "symbol": "XYZ",
        "exh_was_oversold": True,
        "indicator": {
            "pctr": -75.0,
            "pctr_slow": -80.0,
            "pctr_rising": True,
            "cm_rsi": 28.0,
            "cm_rsi_rising": False,  # RSI flat / falling
        },
    }
    ok, why = ew.exhaustion_allows_buy(rec, cfg)
    assert ok is False
    assert why == "rsi_not_rising"


def test_oversold_triangle_refuses_when_pctr_not_rising():
    cfg = _base_cfg()
    rec = {
        "symbol": "XYZ",
        "exh_was_oversold": True,
        "indicator": {
            "pctr": -75.0,
            "pctr_slow": -80.0,
            "pctr_rising": False,
            "cm_rsi_rising": True,
        },
    }
    ok, why = ew.exhaustion_allows_buy(rec, cfg)
    assert ok is False
    assert why == "exh_not_rising"


def test_oversold_triangle_refuses_when_never_oversold():
    cfg = _base_cfg()
    rec = {
        "symbol": "XYZ",
        "exh_was_oversold": False,
        "indicator": {
            "pctr": -75.0,
            "pctr_slow": -80.0,
            "pctr_rising": True,
            "cm_rsi_rising": True,
        },
    }
    ok, why = ew.exhaustion_allows_buy(rec, cfg)
    assert ok is False
    assert why in ("never_oversold", "wait_exh")


def test_oversold_triangle_stale_rejection():
    cfg = _base_cfg()
    rec = {
        "symbol": "XYZ",
        "exh_was_oversold": True,
        "left_os_since": 1000.0,
        "indicator": {
            "pctr": -75.0,
            "pctr_slow": -80.0,
            "pctr_rising": True,
            "cm_rsi_rising": True,
        },
    }
    # 65s after left_os_since with max_age = 60s
    ok, why = ew.exhaustion_allows_buy(rec, cfg, now=1065.0)
    assert ok is False
    assert why == "stale_oversold_triangle"


def test_evaluate_arm_ready_with_os_triangle():
    cfg = _base_cfg()
    rec = {
        "symbol": "XYZ",
        "price": 10.0,
        "last_ask_src": "stream",
        "last_ask_age_sec": 1.0,
        "exh_was_oversold": True,
        "indicator": {
            "pctr": -75.0,
            "pctr_slow": -80.0,
            "pctr_rising": True,
            "cm_rsi": 32.0,
            "cm_rsi_rising": True,
        },
    }
    ready, why = ew.evaluate_arm_ready(rec, cfg)
    assert ready is True
    assert why == "ok"


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
