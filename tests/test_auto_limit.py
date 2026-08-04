"""buy_zone auto-limit gates and rising-edge detection."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from auto_limit import (  # noqa: E402
    AutoLimitState,
    gate_auto_limit,
    process_rows,
    rising_buy_zone,
)


def test_rising_buy_zone_not_on_first_sight():
    prev = {}
    assert rising_buy_zone("AAA", "buy_zone", prev) is False
    prev["AAA"] = "watching"
    assert rising_buy_zone("AAA", "buy_zone", prev) is True
    prev["AAA"] = "buy_zone"
    assert rising_buy_zone("AAA", "buy_zone", prev) is False


def test_gate_defaults_block():
    ok, why = gate_auto_limit(
        enabled=False, live_ok=False, trader_mode="paper",
        has_position=False, cooldown_sec=900, max_per_session=3,
        last_fire=None, session_fires=0, now=1000.0,
    )
    assert not ok and why == "disabled"

    ok, why = gate_auto_limit(
        enabled=True, live_ok=False, trader_mode="live",
        has_position=False, cooldown_sec=900, max_per_session=3,
        last_fire=None, session_fires=0, now=1000.0,
    )
    assert not ok and why == "live_blocked"

    ok, why = gate_auto_limit(
        enabled=True, live_ok=False, trader_mode="paper",
        has_position=False, cooldown_sec=900, max_per_session=3,
        last_fire=None, session_fires=0, now=1000.0,
    )
    assert ok


def test_process_rows_fires_once_on_edge():
    state = AutoLimitState()
    state.prev_status["XYZ"] = "aligning"
    fired = []

    def buy_fn(sym, row):
        fired.append(sym)
        return f"BUY {sym} ok"

    cfg = {
        "auto_limit_enabled": True,
        "auto_limit_live": False,
        "auto_limit_cooldown_sec": 900,
        "auto_limit_max_per_session": 3,
        "auto_limit_require_constructive": True,
        "auto_limit_min_proximity_pct": 67,
    }
    rows = [{
        "ticker": "XYZ",
        "signal_proximity": {
            "status": "buy_zone",
            "proximity_pct": 100,
            "bars_fetched": True,
            "sell_signal": False,
            "pctr_falling": False,
            "pctr_slow_falling": False,
        },
        "rvol": 2.0,
    }]
    ev = process_rows(
        rows, state, cfg=cfg, trader_mode="paper",
        position_symbols=set(), buy_fn=buy_fn, now=1_700_000_000.0,
    )
    assert fired == ["XYZ"]
    assert any(e["kind"] == "auto_limit" for e in ev)

    # Same status next poll — no re-fire
    fired.clear()
    ev2 = process_rows(
        rows, state, cfg=cfg, trader_mode="paper",
        position_symbols=set(), buy_fn=buy_fn, now=1_700_000_010.0,
    )
    assert fired == []
    assert not any(e.get("kind") == "auto_limit" for e in ev2)


def test_process_rows_respects_position():
    state = AutoLimitState()
    state.prev_status["ABC"] = "watching"
    fired = []
    cfg = {
        "auto_limit_enabled": True,
        "auto_limit_live": False,
        "auto_limit_require_constructive": False,
    }
    rows = [{"ticker": "ABC", "signal_proximity": {"status": "buy_zone"}}]
    process_rows(
        rows, state, cfg=cfg, trader_mode="paper",
        position_symbols={"ABC"}, buy_fn=lambda s, r: fired.append(s) or "x",
        now=1_700_000_000.0,
    )
    assert fired == []


def test_constructive_setup_blocks_sell_and_falling():
    from auto_limit import constructive_setup

    base = {
        "signal_proximity": {
            "status": "buy_zone",
            "bars_fetched": True,
            "proximity_pct": 100,
            "sell_signal": False,
            "pctr_falling": False,
            "pctr_slow_falling": False,
        }
    }
    ok, why = constructive_setup(base, require=True)
    assert ok and why == "ok"

    bad_sell = {
        "signal_proximity": {**base["signal_proximity"], "sell_signal": True}
    }
    ok, why = constructive_setup(bad_sell)
    assert not ok and why == "sell_signal"

    bad_pctr = {
        "signal_proximity": {
            **base["signal_proximity"],
            "pctr_falling": True,
            "pctr_slow_falling": True,
        }
    }
    ok, why = constructive_setup(bad_pctr)
    assert not ok and why == "pctr_dual_falling"

    low_prox = {
        "signal_proximity": {**base["signal_proximity"], "proximity_pct": 33}
    }
    ok, why = constructive_setup(low_prox, min_proximity_pct=67)
    assert not ok and "proximity" in why

    ok, _ = constructive_setup(bad_sell, require=False)
    assert ok


def test_process_rows_skips_non_constructive():
    state = AutoLimitState()
    state.prev_status["ZZZ"] = "aligning"
    fired = []
    cfg = {
        "auto_limit_enabled": True,
        "auto_limit_live": False,
        "auto_limit_require_constructive": True,
        "auto_limit_min_proximity_pct": 67,
    }
    rows = [{
        "ticker": "ZZZ",
        "signal_proximity": {
            "status": "buy_zone",
            "bars_fetched": True,
            "proximity_pct": 100,
            "sell_signal": True,
            "pctr_falling": False,
            "pctr_slow_falling": False,
        },
    }]
    ev = process_rows(
        rows, state, cfg=cfg, trader_mode="paper",
        position_symbols=set(),
        buy_fn=lambda s, r: fired.append(s) or "BUY",
        now=1_700_000_000.0,
    )
    assert fired == []
    assert any(e.get("reason") == "sell_signal" for e in ev)
