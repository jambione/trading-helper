"""Unit tests for the 3m %R sell double-check exit gate."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_positions


def test_double_check_sell_disabled_by_default_if_flag_false():
    pos = {
        "entry_confirmed": True,
        "entry_confirmed_at": time.time() - 300,
        "entry_price": 100.0,
        "last_seen_price": 99.0,
        "local_stop_price": 95.0,
    }
    cfg = {"ai_double_check_sell_enabled": False}
    due, info = ai_positions.double_check_sell_due(pos, "TEST", cfg=cfg)
    assert not due
    assert info == {}


def test_double_check_sell_ignores_young_trades():
    pos = {
        "entry_confirmed": True,
        "entry_confirmed_at": time.time() - 60,  # 1 min old
        "entry_price": 100.0,
        "last_seen_price": 99.0,
        "local_stop_price": 95.0,
    }
    cfg = {"ai_double_check_sell_enabled": True, "ai_double_check_min_sec": 180.0}
    due, info = ai_positions.double_check_sell_due(pos, "TEST", cfg=cfg)
    assert not due


def test_double_check_sell_ignores_trades_above_entry():
    pos = {
        "entry_confirmed": True,
        "entry_confirmed_at": time.time() - 240,  # 4 min old
        "entry_price": 100.0,
        "last_seen_price": 100.50,  # above entry
        "local_stop_price": 99.0,
    }
    cfg = {
        "ai_double_check_sell_enabled": True,
        "ai_double_check_min_sec": 180.0,
        "ai_double_check_under_entry_only": True,
    }
    due, info = ai_positions.double_check_sell_due(pos, "TEST", cfg=cfg)
    assert not due


def test_double_check_sell_ignores_green_locked_positions():
    pos = {
        "entry_confirmed": True,
        "entry_confirmed_at": time.time() - 240,
        "entry_price": 100.0,
        "last_seen_price": 99.50,
        "local_stop_price": 100.10,  # stop already locked in profit
    }
    cfg = {"ai_double_check_sell_enabled": True, "ai_double_check_min_sec": 180.0}
    due, info = ai_positions.double_check_sell_due(pos, "TEST", cfg=cfg)
    assert not due


def test_double_check_sell_fires_when_under_entry_and_lines_down():
    pos = {
        "entry_confirmed": True,
        "entry_confirmed_at": time.time() - 200,
        "entry_price": 100.0,
        "last_seen_price": 99.50,
        "local_stop_price": 95.0,
    }
    cfg = {
        "ai_double_check_sell_enabled": True,
        "ai_double_check_min_sec": 180.0,
        "ai_double_check_under_entry_only": True,
    }
    mock_eval = {
        "symbol": "TEST",
        "price": 99.50,
        "sell": "yes",
        "trend": "down",
        "fast_trend": "down",
        "slow_trend": "down",
        "reason": "both %R lines trending down",
    }
    with patch("tools.sell_double_check.evaluate_symbol", return_value=mock_eval):
        due, info = ai_positions.double_check_sell_due(pos, "TEST", cfg=cfg)
        assert due is True
        assert info["sell"] == "yes"
        assert info["fast_trend"] == "down"


def test_double_check_sell_holds_when_momentum_rising():
    pos = {
        "entry_confirmed": True,
        "entry_confirmed_at": time.time() - 200,
        "entry_price": 100.0,
        "last_seen_price": 99.50,
        "local_stop_price": 95.0,
    }
    cfg = {
        "ai_double_check_sell_enabled": True,
        "ai_double_check_min_sec": 180.0,
        "ai_double_check_under_entry_only": True,
    }
    mock_eval = {
        "symbol": "TEST",
        "price": 99.50,
        "sell": "no",
        "trend": "up",
        "fast_trend": "up",
        "slow_trend": "up",
        "reason": "both %R lines trending up",
    }
    with patch("tools.sell_double_check.evaluate_symbol", return_value=mock_eval):
        due, info = ai_positions.double_check_sell_due(pos, "TEST", cfg=cfg)
        assert due is False
        assert info["sell"] == "no"
