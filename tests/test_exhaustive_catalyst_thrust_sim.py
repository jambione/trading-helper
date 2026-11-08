#!/usr/bin/env python3
"""tests/test_exhaustive_catalyst_thrust_sim.py: Unit tests for exhaustive catalyst thrust simulation."""
import math
import pytest
from tools.studies.exhaustive_catalyst_thrust_sim import (
    simulate_thrust_trade,
    fixed_hold_trade,
    compute_metrics,
)


def test_simulate_thrust_trade_ratchet_exit():
    # 5 bars:
    # bar 0: trigger bar (o=100, h=102, l=99, c=101) -> green
    # bar 1: entry bar (o=101, h=103, l=100.5, c=102) -> initial stop = 99
    # bar 2: runner bar (o=102, h=105, l=101.5, c=104) -> stop becomes 100.5
    # bar 3: stop out bar (o=104, h=104, l=100.0, c=100.2) -> hits stop 100.5!
    bars = [
        {"o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0},
        {"o": 101.0, "h": 103.0, "l": 100.5, "c": 102.0},
        {"o": 102.0, "h": 105.0, "l": 101.5, "c": 104.0},
        {"o": 104.0, "h": 104.0, "l": 100.0, "c": 100.2},
        {"o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0},
    ]

    res = simulate_thrust_trade(bars, start_idx=0, H=4, ratchet_lookback=1, cost_bp=12.0)
    assert res is not None
    assert res["entry_px"] == 101.0
    # Stop ratcheted to Bar 2's low (101.5) at the end of Bar 2
    assert res["exit_px"] == 101.5
    assert res["exit_reason"] == "stop"
    assert res["exit_bar"] == 3
    assert res["hold_bars"] == 3
    assert math.isclose(res["gross_bp"], ((101.5 - 101.0) / 101.0) * 10000.0, rel_tol=1e-4)


def test_simulate_thrust_trade_target_cap():
    # Target at +100 bp
    bars = [
        {"o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0},
        {"o": 100.0, "h": 102.0, "l": 99.5, "c": 101.5},
        {"o": 101.5, "h": 103.0, "l": 101.0, "c": 102.5},
    ]
    res = simulate_thrust_trade(bars, start_idx=0, H=2, ratchet_lookback=1, target_bp=100.0, cost_bp=12.0)
    assert res is not None
    assert res["exit_reason"] == "target"
    assert res["exit_bar"] == 1
    assert math.isclose(res["exit_px"], 101.0, rel_tol=1e-4)


def test_compute_metrics_summary():
    trades = [
        {"day": "2024-01-02", "half": "A", "net": 20.0, "exit_reason": "stop", "mfe_bp": 50.0, "mae_bp": -10.0},
        {"day": "2024-01-02", "half": "A", "net": 30.0, "exit_reason": "time", "mfe_bp": 60.0, "mae_bp": -5.0},
        {"day": "2025-08-01", "half": "B", "net": -10.0, "exit_reason": "stop", "mfe_bp": 10.0, "mae_bp": -15.0},
    ]
    m = compute_metrics(trades)
    assert m["n_trades"] == 3
    assert m["n_trades_a"] == 2
    assert m["n_trades_b"] == 1
    assert m["n_days"] == 2
    assert m["net_a_bp"] == 25.0
    assert m["net_b_bp"] == -10.0
    assert m["win_rate_pct"] == pytest.approx(66.67, 0.1)
