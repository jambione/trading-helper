"""Unit tests for the %R sell double-check loss cut.

The desk rule: at 60s, if last is not over entry, read every 30s and sell
unless both %R lines are trending up. Mode "both" is the older gate.
"""
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
        "ai_double_check_mode": "both",
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
        "ai_double_check_mode": "both",
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


def _loss_cut_cfg(**extra):
    cfg = {
        "ai_double_check_sell_enabled": True,
        "ai_double_check_min_sec": 60.0,
        "ai_double_check_interval_sec": 30.0,
        "ai_double_check_under_entry_only": True,
        "ai_double_check_mode": "unless_up",
    }
    cfg.update(extra)
    return cfg


def _pos(start, last, entry=100.0):
    return {
        "entry_confirmed": True,
        "entry_confirmed_at": start,
        "entry_price": entry,
        "last_seen_price": last,
        "local_stop_price": 95.0,
    }


def _reading(trend, sell):
    return {
        "symbol": "TEST",
        "price": 99.5,
        "sell": sell,
        "trend": trend,
        "fast_trend": trend,
        "slow_trend": trend,
        "reason": trend,
    }


def test_loss_cut_waits_until_the_position_is_a_minute_old():
    start = 1_000_000.0
    pos = _pos(start, 99.5)
    with patch("tools.sell_double_check.evaluate_symbol", return_value=_reading("sideways", "no")) as ev:
        due, info = ai_positions.double_check_sell_due(pos, "TEST", start + 59.0, _loss_cut_cfg())
    assert not due
    assert info == {}
    assert "double_check_next_at" not in pos
    ev.assert_not_called()


def test_loss_cut_sells_sideways_at_one_minute_and_a_failed_close_retries():
    start = 1_000_000.0
    pos = _pos(start, 99.5)
    with patch("tools.sell_double_check.evaluate_symbol", return_value=_reading("sideways", "no")) as ev:
        due, info = ai_positions.double_check_sell_due(pos, "TEST", start + 60.0, _loss_cut_cfg())
        assert due is True
        assert info["trend"] == "sideways"
        assert "double_check_next_at" not in pos
        due_again, _info = ai_positions.double_check_sell_due(pos, "TEST", start + 61.0, _loss_cut_cfg())
        assert due_again is True
    assert ev.call_count == 2


def test_loss_cut_holds_an_uptrend_and_rereads_thirty_seconds_later():
    start = 1_000_000.0
    pos = _pos(start, 99.5)
    readings = [_reading("up", "no"), _reading("sideways", "no")]

    def _eval(*_a, **_k):
        return readings.pop(0)

    with patch("tools.sell_double_check.evaluate_symbol", side_effect=_eval) as ev:
        due, info = ai_positions.double_check_sell_due(pos, "TEST", start + 60.0, _loss_cut_cfg())
        assert due is False
        assert info["trend"] == "up"
        assert pos["double_check_next_at"] == start + 90.0
        due_early, early = ai_positions.double_check_sell_due(pos, "TEST", start + 70.0, _loss_cut_cfg())
        assert due_early is False
        assert early == {}
        assert ev.call_count == 1
        due_next, info_next = ai_positions.double_check_sell_due(pos, "TEST", start + 90.0, _loss_cut_cfg())
        assert due_next is True
        assert info_next["trend"] == "sideways"
    assert ev.call_count == 2


def test_loss_cut_at_the_entry_price_starts_the_check():
    start = 1_000_000.0
    pos = _pos(start, 100.0)
    with patch("tools.sell_double_check.evaluate_symbol", return_value=_reading("down", "yes")):
        due, info = ai_positions.double_check_sell_due(pos, "TEST", start + 60.0, _loss_cut_cfg())
    assert due is True
    assert info["trend"] == "down"


def test_price_over_entry_does_not_block_a_later_check_under_entry():
    start = 1_000_000.0
    pos = _pos(start, 100.50)
    with patch("tools.sell_double_check.evaluate_symbol", return_value=_reading("sideways", "no")) as ev:
        due, _info = ai_positions.double_check_sell_due(pos, "TEST", start + 60.0, _loss_cut_cfg())
        assert not due
        assert "double_check_next_at" not in pos
        ev.assert_not_called()
        pos["last_seen_price"] = 99.5
        due_under, info = ai_positions.double_check_sell_due(pos, "TEST", start + 61.0, _loss_cut_cfg())
    assert due_under is True
    assert info["trend"] == "sideways"


def test_a_scheduled_hold_is_not_extended_by_a_print_over_entry():
    start = 1_000_000.0
    pos = _pos(start, 99.5)
    with patch("tools.sell_double_check.evaluate_symbol", return_value=_reading("up", "no")):
        due, _info = ai_positions.double_check_sell_due(pos, "TEST", start + 60.0, _loss_cut_cfg())
    assert due is False
    assert pos["double_check_next_at"] == start + 90.0
    pos["last_seen_price"] = 100.50
    due_over, _info = ai_positions.double_check_sell_due(pos, "TEST", start + 70.0, _loss_cut_cfg())
    assert not due_over
    assert pos["double_check_next_at"] == start + 90.0
    pos["last_seen_price"] = 99.4
    with patch("tools.sell_double_check.evaluate_symbol", return_value=_reading("sideways", "no")) as ev:
        due_early, _info = ai_positions.double_check_sell_due(pos, "TEST", start + 75.0, _loss_cut_cfg())
        assert not due_early
        ev.assert_not_called()
        due_later, info = ai_positions.double_check_sell_due(pos, "TEST", start + 90.0, _loss_cut_cfg())
    assert due_later is True
    assert info["trend"] == "sideways"


def test_mode_both_still_holds_a_sideways_reading():
    start = 1_000_000.0
    pos = _pos(start, 99.5)
    cfg = _loss_cut_cfg(ai_double_check_mode="both", ai_double_check_min_sec=180.0)
    with patch("tools.sell_double_check.evaluate_symbol", return_value=_reading("sideways", "no")):
        due, info = ai_positions.double_check_sell_due(pos, "TEST", start + 180.0, cfg)
    assert due is False
    assert info["sell"] == "no"
    assert pos["double_check_next_at"] == start + 210.0


def test_green_locked_shelf_does_not_start_the_cadence():
    start = 1_000_000.0
    pos = _pos(start, 99.5)
    pos["local_stop_price"] = 100.10
    with patch("tools.sell_double_check.evaluate_symbol", return_value=_reading("down", "yes")) as ev:
        due, info = ai_positions.double_check_sell_due(pos, "TEST", start + 120.0, _loss_cut_cfg())
    assert not due
    assert info == {}
    assert "double_check_next_at" not in pos
    ev.assert_not_called()


def test_desk_config_is_the_one_minute_loss_cut():
    import json
    import config
    live = json.loads((_ROOT / "config" / "bot_config.json").read_text(encoding="utf-8"))
    # Operator 11/03 after the close (6be13747): the cut is OFF live; the 7-session replay A/B (running list
    # row 75) found every version worse than no cut. The rest of the knobs keep their shape for a re-test.
    assert config.DEFAULT_CONFIG["ai_double_check_sell_enabled"] is True
    assert live["ai_double_check_sell_enabled"] is False
    for src in (config.DEFAULT_CONFIG, live):
        assert src["ai_double_check_min_sec"] == 60.0
        assert src["ai_double_check_interval_sec"] == 30.0
        assert src["ai_double_check_under_entry_only"] is True
        assert src["ai_double_check_mode"] == "unless_up"
    assert "ai_double_check_interval_sec" in config.SAFE_CONFIG_KEYS
    baseline = json.loads(
        (_ROOT / "config" / "baselines" / "standard_2026-10-30.json").read_text(encoding="utf-8")
    )
    assert baseline["ai_double_check_sell_enabled"] is False
    assert baseline["ai_double_check_mode"] == "both"


def test_double_check_knobs_change_the_fingerprint():
    import learn_stamps as ls
    keys = set(ls._FINGERPRINT_KEYS)
    for k in (
        "ai_double_check_sell_enabled",
        "ai_double_check_min_sec",
        "ai_double_check_interval_sec",
        "ai_double_check_lookback_min",
        "ai_double_check_under_entry_only",
        "ai_double_check_mode",
    ):
        assert k in keys, k
    base = {"ai_double_check_mode": "both", "ai_double_check_min_sec": 180.0, "paper": True}
    moved = dict(base, ai_double_check_mode="unless_up", ai_double_check_min_sec=60.0)
    assert ls.config_fingerprint(base) != ls.config_fingerprint(moved)


def test_replay_keeps_the_thirty_second_cadence_on_the_position(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from tools.replay_session import FakeBroker

    start = datetime(2026, 11, 3, 14, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
    calls = {"n": 0}

    def _eval(*_a, **_k):
        calls["n"] += 1
        trend = "up" if calls["n"] == 1 else "sideways"
        sell = "no" if trend == "up" else "yes"
        return _reading(trend, sell)

    monkeypatch.setattr(ai_positions, "_symbol_bars_for_double_check", lambda *_a, **_k: None)
    monkeypatch.setattr("tools.sell_double_check.evaluate_symbol", _eval)
    inner = {
        "entry": 100.0, "entry_ts": start, "entry_price": 100.0,
        "entry_stop_price": 95.0, "risk_per_share": 5.0,
        "local_stop_price": 90.0, "target_1": 0.0, "scale": 0.0,
        "why_arm": "replay", "peak_price": 100.0, "mfe_r": 0.0, "mae_r": 0.0,
        "t1_hit": False, "t1_r": None, "trail_prints": [100.0], "trail_last": None,
        "features": {}, "spread_r": None,
    }
    pos = {"symbol": "TEST", "pos": inner, "entry_ts": start}
    cfg = _loss_cut_cfg(
        ai_exit_rsi_dump_enabled=False,
        ai_exit_supertrend=False,
        ai_exit_range=False,
        ai_profit_capture_pct=0.0,
        ai_profit_capture_px=0.0,
        ai_no_progress_flatten_enabled=False,
        ai_dead_trade_min=22.0,
        ai_local_trail_enabled=False,
    )
    broker = FakeBroker(9999, 1, 10000.0, mode="live")
    dash = {"tickers": [{"ticker": "TEST", "price": 99.5}]}
    assert broker._live_exit(pos, 99.5, start + 60.0, dash, cfg, None) is None
    assert inner["double_check_next_at"] == start + 90.0
    assert calls["n"] == 1
    assert broker._live_exit(pos, 99.5, start + 70.0, dash, cfg, None) is None
    assert calls["n"] == 1
    assert broker._live_exit(pos, 99.4, start + 90.0, dash, cfg, None) == "double_check"
    assert calls["n"] == 2
