"""Unit tests for the hard profit capture exit in apply_local_trail."""
from __future__ import annotations

import sys
import time
from pathlib import Path
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import ai_positions as ap


def _pos(**kw):
    base = {
        "symbol": "TEST",
        "entry_confirmed": True,
        "entry_confirmed_at": time.time() - 60.0,
        "entry_price": 30.00,
        "risk_per_share": 1.50,
        "local_stop_price": 29.80,
        "last_seen_price": 30.00,
        "peak_price": 30.00,
        "mfe_r": 0.0,
        "total_qty": 33.0,
    }
    base.update(kw)
    return base


def test_profit_capture_triggers_on_pct(monkeypatch):
    """When price gain reaches ai_profit_capture_pct (0.50%), sells immediately."""
    cfg = {
        "ai_local_trail_enabled": True,
        "ai_profit_capture_pct": 0.50,
        "ai_profit_capture_px": 0.0,
        "ai_local_trail_arm_r": 0.06,
        "ai_local_trail_give_r": 0.12,
    }
    monkeypatch.setattr(ap, "_cfg_all", lambda: cfg)
    monkeypatch.setattr(ap, "_cfg_flag", lambda k, d=False: bool(cfg.get(k, d)))
    monkeypatch.setattr(ap, "handoff_working_sell_to_rth", lambda *a, **k: False)
    monkeypatch.setattr(ap, "soft_exit_held_back", lambda *a, **k: False)
    monkeypatch.setattr(ap, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_premarket_working_sell_on", lambda: False)

    closed_calls = []

    class _Alp:
        def cancel_open_orders(self, t):
            return None

        def close_out(self, t, **kwargs):
            closed_calls.append(t)
            return {"order_id": "test-profit-cap-1"}

    monkeypatch.setitem(sys.modules, "alpaca_trader", _Alp())

    pos = _pos(entry_price=30.00)
    events = []
    exit_why = {}

    # 30.00 -> 30.15 is exactly +0.50%
    ch, closed = ap.apply_local_trail("TEST", pos, 30.15, events, exit_why)
    assert closed is True
    assert closed_calls == ["TEST"]
    assert pos.get("closing_reason") == "profit_capture"
    assert exit_why.get("TEST") == "profit_capture"
    assert any(e.get("event") == "profit_capture" for e in events)


def test_profit_capture_triggers_on_px(monkeypatch):
    """When dollar gain per share reaches ai_profit_capture_px (0.20), sells immediately."""
    cfg = {
        "ai_local_trail_enabled": True,
        "ai_profit_capture_pct": 0.0,
        "ai_profit_capture_px": 0.20,
    }
    monkeypatch.setattr(ap, "_cfg_all", lambda: cfg)
    monkeypatch.setattr(ap, "_cfg_flag", lambda k, d=False: bool(cfg.get(k, d)))
    monkeypatch.setattr(ap, "handoff_working_sell_to_rth", lambda *a, **k: False)
    monkeypatch.setattr(ap, "soft_exit_held_back", lambda *a, **k: False)
    monkeypatch.setattr(ap, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_premarket_working_sell_on", lambda: False)

    closed_calls = []

    class _Alp:
        def cancel_open_orders(self, t):
            return None

        def close_out(self, t, **kwargs):
            closed_calls.append(t)
            return {"order_id": "test-profit-cap-2"}

    monkeypatch.setitem(sys.modules, "alpaca_trader", _Alp())

    pos = _pos(entry_price=30.00)
    events = []
    exit_why = {}

    # 30.00 -> 30.20 is +$0.20/share
    ch, closed = ap.apply_local_trail("TEST", pos, 30.20, events, exit_why)
    assert closed is True
    assert closed_calls == ["TEST"]
    assert pos.get("closing_reason") == "profit_capture"


def test_profit_capture_does_not_fire_below_threshold(monkeypatch):
    """When gain is below threshold, profit capture does not fire."""
    cfg = {
        "ai_local_trail_enabled": True,
        "ai_profit_capture_pct": 0.50,
        "ai_profit_capture_px": 0.0,
        "ai_local_trail_arm_r": 0.06,
        "ai_local_trail_give_r": 0.12,
        "ai_local_trail_time_decay_enabled": False,
    }
    monkeypatch.setattr(ap, "_cfg_all", lambda: cfg)
    monkeypatch.setattr(ap, "_cfg_flag", lambda k, d=False: bool(cfg.get(k, d)))
    monkeypatch.setattr(ap, "handoff_working_sell_to_rth", lambda *a, **k: False)
    monkeypatch.setattr(ap, "soft_exit_held_back", lambda *a, **k: False)
    monkeypatch.setattr(ap, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_premarket_working_sell_on", lambda: False)

    class _Alp:
        def cancel_open_orders(self, t):
            return None

        def close_out(self, t, **kwargs):
            pytest.fail("close_out should not be called below threshold")

    monkeypatch.setitem(sys.modules, "alpaca_trader", _Alp())

    pos = _pos(entry_price=30.00, local_stop_price=29.50)
    events = []
    exit_why = {}

    # 30.10 is +0.33%, below +0.50%
    ch, closed = ap.apply_local_trail("TEST", pos, 30.10, events, exit_why)
    assert closed is False
    assert pos.get("closing_reason") is None


def test_profit_capture_disabled_when_zero(monkeypatch):
    """When both thresholds are 0, profit capture is disabled."""
    cfg = {
        "ai_local_trail_enabled": True,
        "ai_profit_capture_pct": 0.0,
        "ai_profit_capture_px": 0.0,
        "ai_local_trail_time_decay_enabled": False,
    }
    monkeypatch.setattr(ap, "_cfg_all", lambda: cfg)
    monkeypatch.setattr(ap, "_cfg_flag", lambda k, d=False: bool(cfg.get(k, d)))
    monkeypatch.setattr(ap, "handoff_working_sell_to_rth", lambda *a, **k: False)
    monkeypatch.setattr(ap, "soft_exit_held_back", lambda *a, **k: False)
    monkeypatch.setattr(ap, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_premarket_working_sell_on", lambda: False)

    class _Alp:
        def cancel_open_orders(self, t):
            return None

        def close_out(self, t, **kwargs):
            pytest.fail("close_out should not be called when disabled")

    monkeypatch.setitem(sys.modules, "alpaca_trader", _Alp())

    pos = _pos(entry_price=30.00, local_stop_price=29.50)
    events = []
    exit_why = {}

    ch, closed = ap.apply_local_trail("TEST", pos, 31.00, events, exit_why)
    assert closed is False


def test_profit_capture_ignores_raise_only(monkeypatch):
    """raise_only=True computes shelf but cannot sell."""
    cfg = {
        "ai_local_trail_enabled": True,
        "ai_profit_capture_pct": 0.50,
    }
    monkeypatch.setattr(ap, "_cfg_all", lambda: cfg)
    monkeypatch.setattr(ap, "_cfg_flag", lambda k, d=False: bool(cfg.get(k, d)))
    monkeypatch.setattr(ap, "handoff_working_sell_to_rth", lambda *a, **k: False)

    pos = _pos(entry_price=30.00)
    ch, closed = ap.apply_local_trail("TEST", pos, 30.50, [], {}, raise_only=True)
    assert closed is False
    assert pos.get("closing_reason") is None
