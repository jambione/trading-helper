"""Premarket session adapter: working DAY sell, RTH path unchanged, flag off."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alpaca_trader as tr  # noqa: E402
import ai_positions as cp  # noqa: E402


@pytest.fixture(autouse=True)
def _allow_this_host(monkeypatch):
    monkeypatch.setattr(tr, "_host_allowed", lambda: True)


def _arm(fake, *, market_open=False):
    tr._mode = "paper"
    tr._client = fake
    tr._extended_hours = True
    tr._clock_cache = (0.0, False)
    tr.market_is_open = lambda: bool(market_open)


def test_working_sell_replace_is_day_ext_hours_limit():
    fake = MagicMock()
    fake.get_open_position.return_value = SimpleNamespace(qty="40")
    fake.get_orders.return_value = []
    fake.submit_order.return_value = SimpleNamespace(id="ws-1", status="accepted")
    _arm(fake)
    with patch.object(tr, "_log_action"):
        out = tr.working_sell_replace("AAA", 9.91)
    assert out["ok"] is True
    assert out["order_id"] == "ws-1"
    assert out["extended_hours"] is True
    req = fake.submit_order.call_args[0][0]
    assert float(req.limit_price) == 9.91
    assert int(req.qty) == 40
    assert bool(req.extended_hours) is True
    assert str(req.time_in_force).lower().endswith("day")


def test_place_limit_sell_can_set_extended_hours():
    fake = MagicMock()
    fake.get_open_position.return_value = SimpleNamespace(qty="10")
    fake.submit_order.return_value = SimpleNamespace(id="ls-1", status="accepted")
    _arm(fake, market_open=True)
    with patch.object(tr, "_log_action"):
        out = tr.place_limit_sell("AAA", 10, 10.50, time_in_force="day",
                                  extended_hours=True)
    assert out["ok"] is True
    req = fake.submit_order.call_args[0][0]
    assert bool(req.extended_hours) is True


def test_adapter_off_trail_still_market_flattens(monkeypatch):
    """RTH / flag-off: print through shelf still close_out. Must not regress."""
    monkeypatch.setattr(cp, "_cfg_all", lambda: {
        "ai_local_trail_enabled": True,
        "ai_premarket_working_sell": False,
        "ai_local_trail_give_r": 0.10,
        "ai_stranded_close_sec": 0,
        "ai_shelf_trace_sec": 0,
    })
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=True: {
        "ai_local_trail_enabled": True,
        "ai_premarket_working_sell": False,
    }.get(k, d))
    pos = {
        "entry_confirmed": True, "entry_price": 10.0, "risk_per_share": 0.5,
        "last_seen_price": 10.0, "local_stop_price": 9.95,
        "entry_time": 1.0, "mfe_r": 0.0,
    }
    called = {}

    def _close(sym, **k):
        called["close"] = sym
        return {"ok": True, "order_id": "c1"}

    monkeypatch.setattr("alpaca_trader.close_out", _close)
    monkeypatch.setattr("alpaca_trader.cancel_open_orders", lambda *_a, **_k: {"canceled": 0})
    monkeypatch.setattr("alpaca_trader.working_sell_replace", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("working sell")))
    _ch, closed = cp.apply_local_trail("AAA", pos, 9.90, [], {})
    assert closed is True
    assert called.get("close") == "AAA"
    assert pos.get("closing_reason") == "local_trail"


def test_adapter_on_trail_rests_working_sell_not_close_out(monkeypatch):
    monkeypatch.setattr(cp, "_cfg_all", lambda: {
        "ai_local_trail_enabled": True,
        "ai_premarket_working_sell": True,
        "ai_local_trail_give_r": 0.10,
        "ai_stranded_close_sec": 0,
        "ai_shelf_trace_sec": 0,
    })
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=True: {
        "ai_local_trail_enabled": True,
        "ai_premarket_working_sell": True,
    }.get(k, d))
    monkeypatch.setattr(cp, "_rth_now", lambda _n: False)
    monkeypatch.setattr(cp, "_premarket_bid", lambda _s: 9.88)
    pos = {
        "entry_confirmed": True, "entry_price": 10.0, "risk_per_share": 0.5,
        "last_seen_price": 10.0, "local_stop_price": 9.95,
        "entry_time": 1.0, "mfe_r": 0.0,
    }
    placed = {}

    def _ws(sym, px, **k):
        placed["sym"] = sym
        placed["px"] = px
        return {"ok": True, "order_id": "ws-9"}

    monkeypatch.setattr("alpaca_trader.working_sell_replace", _ws)
    monkeypatch.setattr("alpaca_trader.close_out", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("close_out")))
    events = []
    _ch, closed = cp.apply_local_trail("AAA", pos, 9.90, events, {})
    assert closed is False
    assert pos.get("closing_reason") is None
    assert pos.get("working_sell_id") == "ws-9"
    assert pos.get("working_sell_state") == "flatten"
    assert placed["px"] == 9.88
    assert any(e.get("event") == "local_trail_working" for e in events)


def test_handoff_cancels_working_sell_and_clears_state(monkeypatch):
    canceled = {}

    def _co(sym):
        canceled["sym"] = sym
        return {"canceled": 1}

    monkeypatch.setattr("alpaca_trader.cancel_open_orders", _co)
    monkeypatch.setattr(cp, "_rth_now", lambda _n: True)
    pos = {
        "working_sell_id": "ws-1", "working_sell_px": 9.90,
        "working_sell_state": "protect",
    }
    assert cp.handoff_working_sell_to_rth("AAA", pos) is True
    assert canceled["sym"] == "AAA"
    assert "working_sell_id" not in pos
    assert "working_sell_state" not in pos


def test_handoff_is_noop_before_the_bell(monkeypatch):
    monkeypatch.setattr(cp, "_rth_now", lambda _n: False)
    monkeypatch.setattr("alpaca_trader.cancel_open_orders", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("cancel")))
    pos = {"working_sell_id": "ws-1", "working_sell_state": "protect"}
    assert cp.handoff_working_sell_to_rth("AAA", pos) is False
    assert pos["working_sell_id"] == "ws-1"


def test_quote_is_live_premarket_requires_book(monkeypatch):
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=True: {
        "ai_premarket_working_sell": True,
    }.get(k, d))
    monkeypatch.setattr(cp, "_rth_now", lambda _n: False)
    monkeypatch.setattr(cp, "_premarket_book", lambda _s: (None, None))
    monkeypatch.setattr("ai_entry_watch.decision_price", lambda *_a, **_k: (10.0, "stream", 0.2))
    ok, why = cp.quote_is_live("AAA", {})
    assert ok is False
    assert why == "no_book"


def test_quote_is_live_premarket_passes_with_bid_ask(monkeypatch):
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=True: {
        "ai_premarket_working_sell": True,
    }.get(k, d))
    monkeypatch.setattr(cp, "_rth_now", lambda _n: False)
    monkeypatch.setattr(cp, "_premarket_book", lambda _s: (9.90, 9.95))
    monkeypatch.setattr("ai_entry_watch.decision_price", lambda *_a, **_k: (10.0, "stream", 0.2))
    ok, why = cp.quote_is_live("AAA", {"ai_stale_data_max_age_sec": 15})
    assert ok is True
    assert why == "stream"


def test_adapter_flag_defaults_off():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG.get("ai_premarket_working_sell") is False
