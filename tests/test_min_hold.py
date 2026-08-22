"""ai_exit_min_hold_sec: hold back discretionary exits, never protection.

Realized R is monotone in how long a trade was allowed to live (hold <10s
returns -0.088 R at a 2% win rate; hold >10m returns +0.055 R at 52%).
Survival is an outcome and cannot be chosen — the delay can.

The whole trade being made is that max loss becomes the 1R disaster stop
instead of the 0.10R shelf. So the two things that make the wait survivable
— the hard stop and the 15:50 flatten — must never be gated, and the
default must be off.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

cp = pytest.importorskip("ai_positions")

NOW = 1_787_000_000.0


def _pos(age_sec):
    return {"entry_time": NOW - age_sec}


def test_off_by_default():
    """Shipped behaviour: every exit armed from the first tick."""
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_exit_min_hold_sec"] == 0
    assert cp.soft_exit_held_back(_pos(1), NOW, {}) is False


def test_young_fill_is_held_back():
    cfg = {"ai_exit_min_hold_sec": 1800}
    assert cp.soft_exit_held_back(_pos(60), NOW, cfg) is True
    assert cp.soft_exit_held_back(_pos(1799), NOW, cfg) is True


def test_matured_fill_is_released():
    cfg = {"ai_exit_min_hold_sec": 1800}
    assert cp.soft_exit_held_back(_pos(1800), NOW, cfg) is False
    assert cp.soft_exit_held_back(_pos(3600), NOW, cfg) is False


def test_zero_and_negative_never_hold():
    for v in (0, 0.0, -1, "", None):
        assert cp.soft_exit_held_back(_pos(1), NOW, {"ai_exit_min_hold_sec": v}) is False


def test_unknown_age_fails_open():
    """No entry_time must not read as 'young' and pin a position open."""
    cfg = {"ai_exit_min_hold_sec": 1800}
    assert cp.soft_exit_held_back({}, NOW, cfg) is False
    assert cp.soft_exit_held_back({"entry_time": None}, NOW, cfg) is False
    assert cp.soft_exit_held_back({"entry_time": "junk"}, NOW, cfg) is False
    assert cp.soft_exit_held_back(None, NOW, cfg) is False


def test_garbage_config_fails_open():
    assert cp.soft_exit_held_back(_pos(1), NOW, {"ai_exit_min_hold_sec": "soon"}) is False


def test_it_gates_the_three_discretionary_exits():
    """Shelf, dead-trade and left-overbought — the desk's opinions."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "ai_positions.py"), encoding="utf-8").read()
    # the trailing shelf sale
    assert "and not soft_exit_held_back(pos)):" in src
    # dead trade
    assert "and mfe_ok and not soft_exit_held_back(pos, now):" in src
    # exhaustion / left_overbought
    assert 'if hit and soft_exit_held_back(pos, now):' in src
    assert 'hit, why = False, "min_hold"' in src


def test_the_disaster_stop_is_never_gated():
    """1R stop and the 15:50 flatten are what make the wait survivable.

    They are placed with the broker and run through liquidate_all, neither
    of which consults soft_exit_held_back — pinned here because gating one
    of them later would turn a bounded loss into an unbounded one.
    """
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "ai_positions.py"), encoding="utf-8").read()
    i = src.index("def soft_exit_held_back")
    j = src.index("def _pos_spread_r")
    body = src[i:j]
    for forbidden in ("entry_stop_price", "liquidate_all", "eod"):
        assert forbidden not in body, (
            f"{forbidden} must stay outside the min-hold gate")
