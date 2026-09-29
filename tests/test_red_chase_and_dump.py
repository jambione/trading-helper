"""Underwater stop creep and the upper-band %R dump exit."""
from __future__ import annotations

import ai_positions as cp


def test_red_chase_creeps_toward_last_minus_cent():
    """entry 10, stop 9.50, last 9.95, R=0.50 → +$0.01 per 8s, cap 9.94."""
    pos = {
        "entry_price": 10.00,
        "entry_stop_price": 9.50,
        "risk_per_share": 0.50,
        "last_seen_price": 9.95,
        "mfe_r": 0.0,
        "local_stop_price": 9.50,
    }
    cfg = {
        "ai_local_trail_enabled": True,
        "ai_local_trail_arm_r": 0.06,
        "ai_local_trail_give_r": 0.12,
        "ai_local_trail_time_decay_enabled": True,
        "ai_local_trail_decay_idle_sec": 8.0,
        "ai_local_trail_decay_step_r": 0.05,
        "ai_local_trail_decay_red_step_r": 0.02,
        "ai_local_trail_min_give_px": 0.0,
        "ai_local_trail_peak_give_pct": 0.0,
    }
    t0 = 1_000_000.0
    assert cp.local_profit_stop(pos, cfg, now=t0) == 9.50
    pos["local_stop_price"] = 9.50
    got = cp.local_profit_stop(pos, cfg, now=t0 + 8.0)
    assert got == 9.51
    pos["local_stop_price"] = got
    # New high under water resets the idle clock.
    pos["last_seen_price"] = 9.96
    assert cp.local_profit_stop(pos, cfg, now=t0 + 12.0) == 9.51
    pos["last_seen_price"] = 9.96
    got = cp.local_profit_stop(pos, cfg, now=t0 + 20.0)
    assert got == 9.52


def test_red_chase_does_not_lower_a_stop_already_above_last():
    pos = {
        "entry_price": 10.00,
        "risk_per_share": 0.50,
        "last_seen_price": 9.90,
        "local_stop_price": 10.30,
        "mfe_r": 0.0,
        "trail_decay_peak": 10.50,
        "trail_decay_last_step_at": 1_000_000.0,
        "trail_decay_idle_since": 1_000_000.0,
    }
    cfg = {
        "ai_local_trail_time_decay_enabled": True,
        "ai_local_trail_decay_idle_sec": 8.0,
        "ai_local_trail_decay_red_step_r": 0.02,
        "ai_local_trail_min_give_px": 0.0,
    }
    got = cp.green_catchup_raise(pos, cfg, now=1_000_030.0)
    assert got is None
    assert pos["local_stop_price"] == 10.30


def test_unarmed_green_chase_steps_after_idle():
    pos = {
        "entry_price": 10.00,
        "entry_stop_price": 9.50,
        "risk_per_share": 0.50,
        "last_seen_price": 10.02,
        "peak_price": 10.02,
        "mfe_r": 0.02,  # 0.04R off the fill, under arm_r 0.06
        "local_stop_price": 9.50,
    }
    cfg = {
        "ai_local_trail_enabled": True,
        "ai_local_trail_arm_r": 0.06,
        "ai_local_trail_give_r": 0.12,
        "ai_local_trail_time_decay_enabled": True,
        "ai_local_trail_decay_idle_sec": 8.0,
        "ai_local_trail_decay_step_r": 0.05,
        "ai_local_trail_min_give_px": 0.0,
        "ai_local_trail_peak_give_pct": 0.0,
        "ai_local_trail_be_at_r": 0.0,
    }
    t0 = 2_000_000.0
    assert cp.local_profit_stop(pos, cfg, now=t0) == 9.50
    # 0.05R * 0.50 = $0.025 → ceil to $0.03
    got = cp.local_profit_stop(pos, cfg, now=t0 + 8.0)
    assert got == 9.53


def test_rsi_dump_needs_two_reads_and_a_real_drop():
    pos = {"entry_confirmed": True}
    cfg = {
        "ai_exit_rsi_dump_enabled": True,
        "ai_exit_rsi_dump_points": 30,
        "ai_exit_rsi_dump_sec": 60,
        "ai_exit_rsi_dump_confirm_ticks": 2,
        "rte_threshold": 20,
    }
    assert cp.rsi_dump_due(pos, {"pctr": -10}, {**cfg, "ai_exit_rsi_dump_enabled": False}, 1000) is False
    pos = {"entry_confirmed": True}
    assert cp.rsi_dump_due(pos, {"pctr": -10}, cfg, 1000) is False
    assert cp.rsi_dump_due(pos, {"pctr": -15}, cfg, 1010) is False  # 5-point wiggle
    pos = {"entry_confirmed": True}
    assert cp.rsi_dump_due(pos, {"pctr": -10}, cfg, 2000) is False
    assert cp.rsi_dump_due(pos, {"pctr": -45}, cfg, 2010) is False  # first confirming read
    assert cp.rsi_dump_due(pos, {"pctr": -48}, cfg, 2020) is True
    # Never visited the upper band.
    pos = {"entry_confirmed": True}
    assert cp.rsi_dump_due(pos, {"pctr": -50}, cfg, 3000) is False
    assert cp.rsi_dump_due(pos, {"pctr": -90}, cfg, 3010) is False
