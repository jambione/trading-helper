"""Triangle-first exit: trail yields while dual OB; leave-OB owns the race."""
from __future__ import annotations

import time

import pytest

import ai_positions as ap


def _cfg(**over):
    c = {
        "ai_watch_exh_square_arm": True,
        "ai_exit_left_overbought": True,
        "ai_watch_exhaustion_rules": True,
        "ai_local_trail_enabled": True,
        "ai_local_trail_arm_r": 0.15,
        "ai_local_trail_be_at_r": 0.15,
        "ai_local_trail_ob_hold_mae_r": -1.0,
        "ai_exit_left_overbought_confirm_sec": 3.0,
        "ai_exit_min_hold_sec": 0.0,
        "rte_threshold": 20,
        "rte_confluence_max": 15.0,
        "ai_watch_tv_exh_rsi": False,
    }
    c.update(over)
    return c


def _pos(**over):
    now = time.time()
    p = {
        "symbol": "NUAI",
        "entry_confirmed": True,
        "entry_price": 10.0,
        "entry_time": now - 120.0,
        "risk_per_share": 0.5,
        "local_stop_price": 10.05,  # BE shelf above entry
        "last_seen_price": 10.04,   # print under shelf → trail hit
        "peak_price": 10.10,
        "mfe_r": 0.20,
        "mae_r": -0.05,
        "exh_was_overbought": True,
        "qty_a": 10,
        "qty_b": 0,
        "indicator": {
            "pctr": -10.0, "pctr_slow": -8.0,
            "pctr_ob": True, "pctr_tight": True,
        },
    }
    p.update(over)
    return p


def test_trail_yields_while_still_dual_ob(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(ap, "_cfg_all", lambda: cfg)
    monkeypatch.setattr(ap, "_cfg_flag", lambda k, d=False: bool(cfg.get(k, d)))
    # Skip live refresh — use stamped indicator.
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "apply_live_exhaustion", lambda *a, **k: False)

    yield_now, why = ap.trail_yields_to_triangle(_pos(), cfg)
    assert yield_now is True
    assert why == "still_dual_ob"


def test_trail_mae_escape_while_dual_ob(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(ap, "_cfg_all", lambda: cfg)
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "apply_live_exhaustion", lambda *a, **k: False)

    yield_now, why = ap.trail_yields_to_triangle(
        _pos(mae_r=-1.2), cfg)
    assert yield_now is False
    assert why == "mae_escape"


def test_trail_yields_during_leave_ob_race(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(ap, "_cfg_all", lambda: cfg)
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "apply_live_exhaustion", lambda *a, **k: False)

    now = time.time()
    pos = _pos(
        left_ob_since=now - 1.0,
        indicator={
            "pctr": -40.0, "pctr_slow": -30.0,
            "pctr_ob": False, "pctr_tight": False,
        },
    )
    yield_now, why = ap.trail_yields_to_triangle(pos, cfg, now=now)
    assert yield_now is True
    assert why in ("left_ob_pending", "left_ob_race")


def test_apply_local_trail_defers_while_dual_ob(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(ap, "_cfg_all", lambda: cfg)
    monkeypatch.setattr(ap, "_cfg_flag", lambda k, d=False: bool(cfg.get(k, d)))
    monkeypatch.setattr(ap, "handoff_working_sell_to_rth", lambda *a, **k: False)
    monkeypatch.setattr(ap, "soft_exit_held_back", lambda *a, **k: False)
    monkeypatch.setattr(ap, "log_event", lambda *a, **k: None)
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "apply_live_exhaustion", lambda *a, **k: False)

    closed_calls = []

    class _Alp:
        def cancel_open_orders(self, t):
            return None

        def close_out(self, t):
            closed_calls.append(t)
            return {"order_id": "x"}

    import sys
    monkeypatch.setitem(sys.modules, "alpaca_trader", _Alp())

    pos = _pos()
    events = []
    exit_why = {}
    # trigger under shelf
    _ch, closed = ap.apply_local_trail(
        "NUAI", pos, 10.04, events, exit_why)
    assert closed is False
    assert closed_calls == []
    assert pos.get("closing_reason") is None
    assert str(exit_why.get("NUAI") or "").startswith("local_trail_deferred")


def test_apply_local_trail_fires_when_square_mode_off(monkeypatch):
    cfg = _cfg(ai_watch_exh_square_arm=False, ai_exit_left_overbought=False)
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

        def close_out(self, t):
            closed_calls.append(t)
            return {"order_id": "x"}

    import sys
    monkeypatch.setitem(sys.modules, "alpaca_trader", _Alp())

    pos = _pos(exh_was_overbought=False)
    events = []
    exit_why = {}
    _ch, closed = ap.apply_local_trail(
        "NUAI", pos, 10.04, events, exit_why)
    assert closed is True
    assert closed_calls == ["NUAI"]
    assert pos.get("closing_reason") == "local_trail"


def test_leave_ob_still_wins_when_confirmed():
    """Regression: dual leave-OB confirmed → left_overbought (existing path)."""
    import ai_entry_watch as ew

    cfg = _cfg(ai_exit_left_overbought_confirm_sec=0.0)
    leave = {
        "symbol": "SMCI",
        "exh_was_overbought": True,
        "indicator": {
            "pctr": -40.0, "pctr_slow": -25.0,
            "pctr_ob": False,
        },
    }
    hit, why = ew.exhaustion_exit_now(leave, cfg, now=1_000.0)
    assert hit is True and why == "left_overbought"


def test_leave_ob_exempt_from_min_hold(monkeypatch):
    """In square mode, leave-OB triangle is exempt from 90s min-hold deferral."""
    import ai_entry_watch as ew

    cfg = _cfg(
        ai_exit_min_hold_sec=90.0,
        ai_exit_left_overbought_confirm_sec=0.0,
        ai_exit_left_ob_exempt_min_hold=True,
    )
    monkeypatch.setattr(ap, "_cfg_all", lambda: cfg)

    now = 1_000.0
    pos = _pos(
        symbol="SMCI",
        entry_time=now - 20.0,  # 20s old < 90s min_hold
        exh_was_overbought=True,
        indicator={"pctr": -40.0, "pctr_slow": -25.0, "pctr_ob": False},
    )

    # Directly verify soft_exit_held_back is True (min_hold active)
    assert ap.soft_exit_held_back(pos, now, cfg) is True

    # But exhaustion_exit_now fires leave_overbought
    hit, why = ew.exhaustion_exit_now(pos, cfg, now=now)
    assert hit is True and why == "left_overbought"


def test_stale_square_gate_refuses_late_entry():
    """Age-of-square gate blocks entering a climax after square ran >= max_age."""
    import ai_entry_watch as ew

    cfg = _cfg(ai_watch_square_max_age_sec=60.0)
    now = 1_000.0

    # Young square (10s old) -> passes
    young_rec = {
        "symbol": "SMCI",
        "square_since": now - 10.0,
        "indicator": {"pctr": -10.0, "pctr_slow": -8.0, "pctr_ob": True},
    }
    ok, why = ew._square_exh_allows_buy(young_rec, cfg, require_rising=False, now=now)
    assert ok is True and why == "overbought"

    # Stale square (75s old >= 60s) -> refused
    stale_rec = {
        "symbol": "SMCI",
        "square_since": now - 75.0,
        "indicator": {"pctr": -10.0, "pctr_slow": -8.0, "pctr_ob": True},
    }
    ok, why = ew._square_exh_allows_buy(stale_rec, cfg, require_rising=False, now=now)
    assert ok is False and why == "stale_square"
    assert "square age 75s >= 60s" in stale_rec.get("block_detail", "")

    # Disabled gate (0) -> passes
    cfg_off = _cfg(ai_watch_square_max_age_sec=0)
    ok_off, why_off = ew._square_exh_allows_buy(stale_rec, cfg_off, require_rising=False, now=now)
    assert ok_off is True and why_off == "overbought"
