"""Stability rules: block on pre-fill RSI/EXH thrash, never invent R."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

ss = pytest.importorskip("rsi_exh_stability_screen")


def _tick(ts, rsi=None, exh=None):
    row = {"ts": ts}
    if rsi is not None:
        row["cm_rsi"] = rsi
    if exh is not None:
        row["exhaustion"] = exh
    return row


def test_max_rsi_blocks_when_window_already_extended():
    et = 1_000_000.0
    pre = [_tick(et - 200, 40), _tick(et - 60, 85), _tick(et, 35)]
    assert ss.rule_max_rsi(pre, 70.0) is True


def test_max_rsi_keeps_when_window_stayed_calm():
    et = 1_000_000.0
    pre = [_tick(et - 200, 20), _tick(et - 60, 35), _tick(et, 40)]
    assert ss.rule_max_rsi(pre, 70.0) is False


def test_max_rsi_unknown_without_ticks():
    assert ss.rule_max_rsi([], 70.0) is None


def test_hold_60s_needs_min_ticks_else_unknown():
    et = 1_000_000.0
    pre = [_tick(et - 10, 40), _tick(et, 41)]
    assert ss.rule_rsi_hold_sec(pre, et, 60.0, 50.0, min_ticks=3) is None


def test_hold_60s_blocks_if_any_tick_extended():
    et = 1_000_000.0
    pre = [_tick(et - 50, 20), _tick(et - 20, 55), _tick(et, 30)]
    assert ss.rule_rsi_hold_sec(pre, et, 60.0, 50.0, min_ticks=3) is True


def test_hold_3ticks_uses_only_the_last_three():
    et = 1_000_000.0
    pre = [
        _tick(et - 200, 99),
        _tick(et - 30, 20),
        _tick(et - 20, 25),
        _tick(et - 10, 30),
    ]
    assert ss.rule_rsi_hold_ticks(pre, 3, 50.0) is False
    pre[-1] = _tick(et - 10, 60)
    assert ss.rule_rsi_hold_ticks(pre, 3, 50.0) is True


def test_max_exh_blocks_prior_overbought_print():
    et = 1_000_000.0
    pre = [_tick(et - 100, exh=40), _tick(et - 20, exh=90), _tick(et, exh=50)]
    assert ss.rule_max_exh(pre, 80.0) is True


def test_summarize_splits_blocked_and_kept_r():
    rows = [
        {"blocks": {"max_rsi_5m>=70": True, "rsi_hold_60s<50": True,
                    "rsi_hold_3ticks<50": False, "max_exh_5m>=80": False},
         "realized_r": -0.2},
        {"blocks": {"max_rsi_5m>=70": False, "rsi_hold_60s<50": False,
                    "rsi_hold_3ticks<50": False, "max_exh_5m>=80": False},
         "realized_r": 0.1},
        {"blocks": {"max_rsi_5m>=70": None, "rsi_hold_60s<50": None,
                    "rsi_hold_3ticks<50": None, "max_exh_5m>=80": None},
         "realized_r": 0.0},
    ]
    s = ss.summarize(rows)
    assert s["rules"]["max_rsi_5m>=70"]["blocked"]["n"] == 1
    assert s["rules"]["max_rsi_5m>=70"]["kept"]["n"] == 1
    assert s["rules"]["max_rsi_5m>=70"]["blocked"]["sum_r"] == pytest.approx(-0.2)
    assert s["rules"]["max_rsi_5m>=70"]["kept"]["sum_r"] == pytest.approx(0.1)
