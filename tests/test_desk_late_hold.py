"""Last-hour hold paper path — not a daytime overlay."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import desk_late_hold as lh  # noqa: E402
import ai_entry_watch as ew  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = "2026-08-21"


def _ts(hour: int, minute: int, day: str = DAY) -> float:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(
        tzinfo=ET).timestamp()


def _cfg(**over):
    cfg = {
        "ai_late_hold_paper": True,
        "ai_late_hold_start": "14:00",
        "ai_late_hold_end": "15:30",
        "ai_late_hold_stop_pct": 2.0,
        "ai_watch_arm_mode": "last",
        "ai_watch_exhaustion_rules": True,
        "ai_watch_exhaustion_heat_min_pct": 40.0,
        "ai_watch_arm_require_cm_rsi": True,
        "ai_min_reward_risk": 0.5,
        "ai_watch_synth_rr": 1.0,
        "ai_watch_synth_stop_pct": 5.0,
    }
    cfg.update(over)
    return cfg


def _rec(*, admit_h=14, admit_m=10, **extra):
    rec = {
        "symbol": "AAA",
        "status": "watching",
        "admit_ts": _ts(admit_h, admit_m),
        "structure": {
            "decision": "BUY",
            "entry_low": 9.5, "entry_high": 10.5,
            "stop_price": 9.5, "target_1": 11.0, "reward_risk": 1.0,
            "zone_kind": "at_last", "synthetic": True,
        },
        # Would fail heat + RSI if the daytime gates ran.
        "indicator": {
            "pctr": -80.0, "pctr_rising": False, "pctr_falling": True,
            "pctr_ok": False, "cm_ok": False, "cm_rsi": 90.0,
            "cm_rsi_rising": False,
        },
    }
    rec.update(extra)
    return rec


def test_window_and_arm_why():
    cfg = _cfg()
    assert lh.in_window(cfg, _ts(14, 10)) is True
    assert lh.in_window(cfg, _ts(10, 0)) is False
    assert lh.in_window(cfg, _ts(15, 30)) is False
    assert lh.arm_why(cfg, _ts(10, 0), _ts(14, 10)) == "late_hold_closed"
    assert lh.arm_why(cfg, _ts(14, 10), _ts(10, 0)) == "late_hold_not_late_admit"
    assert lh.arm_why(cfg, _ts(14, 10), _ts(14, 5)) is None
    assert lh.arm_why(_cfg(ai_late_hold_paper=False), _ts(10, 0), _ts(10, 0)) is None


def test_daytime_arm_suppressed_when_experiment_on():
    ok, why = ew.should_arm_buy(
        _rec(), ask=10.0, bid=9.99, cfg=_cfg(), now=_ts(10, 15))
    assert ok is False and why == "late_hold_closed"


def test_morning_admit_does_not_arm_at_1400():
    ok, why = ew.should_arm_buy(
        _rec(admit_h=10, admit_m=0), ask=10.0, bid=9.99, cfg=_cfg(),
        now=_ts(14, 10))
    assert ok is False and why == "late_hold_not_late_admit"


def test_late_admit_arms_without_heat_or_rsi():
    ok, why = ew.should_arm_buy(
        _rec(), ask=10.0, bid=9.99, cfg=_cfg(), now=_ts(14, 10))
    assert ok is True and why == "last_late_hold"


def test_off_does_not_change_daytime_gates():
    """Experiment off: a cold/overbought last-mode name still fails heat."""
    ok, why = ew.should_arm_buy(
        _rec(), ask=10.0, bid=9.99, cfg=_cfg(ai_late_hold_paper=False),
        now=_ts(14, 10))
    assert ok is False
    assert why != "last_late_hold"


def test_place_decision_uses_two_percent_stop():
    d = ew._decision_for_place(
        _rec()["structure"], ask=10.0, cfg=_cfg(), late_hold=True)
    assert d["late_hold"] is True
    assert d["strategy"] == "late_hold"
    assert d["scale_out_pct"] == 0.0
    assert d["stop_price"] == pytest.approx(9.8)
    # Daytime 5% would have been 9.50
    d2 = ew._decision_for_place(
        _rec()["structure"], ask=10.0, cfg=_cfg(), late_hold=False)
    assert d2.get("late_hold") is not True
    assert d2["stop_price"] == pytest.approx(9.5)


def test_flatten_at_hard_stop_not_working_shelf(monkeypatch):
    import ai_positions as cp

    closed = []
    monkeypatch.setattr("alpaca_trader.cancel_open_orders", lambda *a, **k: None)
    monkeypatch.setattr(
        "alpaca_trader.close_out",
        lambda t: closed.append(t) or {"ok": True, "order_id": "x"},
    )
    pos = {
        "late_hold": True, "entry_confirmed": True,
        "entry_stop_price": 9.80, "stop_price": 9.80,
        "last_seen_price": 9.79, "entry_price": 10.0,
    }
    _ch, done = cp.apply_local_trail("AAA", pos, 9.79, [], {})
    assert done is True
    assert pos["closing_reason"] == "late_hold_stop"
    assert closed == ["AAA"]

    pos2 = {
        "late_hold": True, "entry_confirmed": True, "closing_reason": None,
        "entry_stop_price": 9.80, "stop_price": 9.80,
        "last_seen_price": 10.0, "entry_price": 10.0,
    }
    _ch, done = cp.apply_local_trail("AAA", pos2, 10.0, [], {})
    assert done is False
    assert pos2.get("closing_reason") is None
    # A 0.10R shelf at 5% 1R would be $9.95. We must not flatten there.
    _ch, done = cp.apply_local_trail("AAA", pos2, 9.94, [], {})
    assert done is False
