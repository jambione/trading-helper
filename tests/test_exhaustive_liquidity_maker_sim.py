#!/usr/bin/env python3
"""tests/test_exhaustive_liquidity_maker_sim.py: Unit tests for exhaustive liquidity maker simulation."""
import numpy as np
import pytest
from tools.studies.exhaustive_liquidity_maker_sim import (
    simulate_maker_trade,
    compute_grid_metrics,
)


def test_simulate_maker_trade_target_hit():
    # t=10. Order placed at c[10] * (1 - 2 bp) = 100.0 * 0.9998 = 99.98
    # Fill on bar 11 requires l[11] <= 99.98 * 0.9998 = 99.96
    # Target at 40 bp = 99.98 * 1.0040 = 100.38. With 2 bp trade-through = 100.40
    # Stop at 30 bp = 99.98 * (1 - 0.0030) = 99.68
    o = np.array([100.0] * 25)
    h = np.array([100.2] * 25)
    l = np.array([99.8] * 25)
    c = np.array([100.0] * 25)

    # Trigger bar t=10
    c[10] = 100.0
    # Entry bar t+1 = 11: dips to 99.85 to fill (below 99.96, above stop 99.68)
    l[11] = 99.85
    # Bar 12 hits target
    h[12] = 101.0

    res = simulate_maker_trade(o, h, l, c, t=10, stop_bp=30.0, target_bp=40.0, H=5)
    assert res is not None
    assert res["exit_reason"] == "target"
    assert res["exit_bar"] == 12
    assert res["hold_bars"] == 1
    assert res["net"] == pytest.approx(40.0, 0.1)


def test_simulate_maker_trade_stop_hit():
    o = np.array([100.0] * 25)
    h = np.array([100.2] * 25)
    l = np.array([99.8] * 25)
    c = np.array([100.0] * 25)

    c[10] = 100.0
    # Entry bar t+1 = 11: fills at 99.85 without stopping out
    l[11] = 99.85
    # Bar 12 crashes through stop (below 99.68)
    l[12] = 95.0

    res = simulate_maker_trade(o, h, l, c, t=10, stop_bp=30.0, target_bp=40.0, H=5)
    assert res is not None
    assert res["exit_reason"] == "stop"
    assert res["exit_bar"] == 12
    # -30 bp - 3 bp taker fee = -33 bp
    assert res["net"] == pytest.approx(-33.0, 0.1)


def test_simulate_maker_trade_unfilled():
    o = np.array([100.0] * 25)
    h = np.array([100.5] * 25)
    l = np.array([100.1] * 25)  # Never dips low enough
    c = np.array([100.0] * 25)

    res = simulate_maker_trade(o, h, l, c, t=10, stop_bp=30.0, target_bp=40.0, H=5)
    assert res is None


def test_compute_grid_metrics():
    trades = [
        {"day": "2026-05-01", "net": 40.0, "exit_reason": "target", "hold_bars": 2},
        {"day": "2026-05-01", "net": -33.0, "exit_reason": "stop", "hold_bars": 1},
        {"day": "2026-09-01", "net": 40.0, "exit_reason": "target", "hold_bars": 3},
    ]
    m = compute_grid_metrics(trades, half_cutoff_day="2026-07-01")
    assert m["n_trades"] == 3
    assert m["n_trades_a"] == 2
    assert m["n_trades_b"] == 1
    assert m["win_rate_pct"] == pytest.approx(66.67, 0.1)
    assert m["reasons"]["target"] == 2
    assert m["reasons"]["stop"] == 1
