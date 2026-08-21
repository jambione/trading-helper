"""Product switch — observe blocks arms; omitted key keeps scalp tests working."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import desk_h4  # noqa: E402
import desk_product as dp  # noqa: E402
import ai_entry_watch as ew  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(hour=10, minute=0):
    return datetime.fromisoformat(f"2026-08-21T{hour:02d}:{minute:02d}:00").replace(
        tzinfo=ET).timestamp()


def _rec():
    return {
        "symbol": "AAA",
        "status": "watching",
        "admit_ts": _ts(10, 0),
        "structure": {
            "decision": "BUY",
            "entry_low": 9.5, "entry_high": 10.5,
            "stop_price": 9.5, "target_1": 11.0, "reward_risk": 1.0,
            "zone_kind": "at_last", "synthetic": True,
        },
        "indicator": {
            "pctr": -10.0, "pctr_rising": True, "pctr_falling": False,
            "pctr_ok": True, "cm_ok": True, "cm_rsi": 20.0,
            "cm_rsi_rising": True,
        },
    }


def test_missing_product_is_scalp_legacy():
    assert dp.product({}) == dp.SCALP_LEGACY
    assert dp.arm_block_reason({}) is None


def test_unknown_product_fails_closed():
    assert dp.product({"desk_product": "magic"}) == dp.OBSERVE
    assert dp.arm_block_reason({"desk_product": "magic"}) == dp.REASON_OBSERVE


def test_observe_blocks():
    assert dp.arm_block_reason({"desk_product": "observe"}) == dp.REASON_OBSERVE


def test_h4_off_until_paper_flag():
    cfg = {"desk_product": "h4_swing"}
    assert dp.arm_block_reason(cfg) == dp.REASON_H4_OFF
    cfg["ai_h4_paper"] = True
    assert dp.arm_block_reason(cfg) is None


def test_should_arm_buy_observe_vetoes_before_late_hold():
    cfg = {
        "desk_product": "observe",
        "ai_late_hold_paper": True,
        "ai_watch_arm_mode": "last",
        "ai_min_reward_risk": 0.5,
        "ai_watch_synth_rr": 1.0,
        "ai_watch_synth_stop_pct": 5.0,
    }
    ok, why = ew.should_arm_buy(_rec(), ask=10.0, bid=9.99, cfg=cfg, now=_ts(10, 15))
    assert ok is False and why == "desk_observe"


def test_h4_universe_price_and_volume():
    row = {"ticker": "AAA", "price": 12.0, "avg_dollar_vol_50d": 6e6, "rs_rating": 90}
    ok, why = desk_h4.universe_row_ok(row, {})
    assert ok and why == "ok"
    ok, why = desk_h4.universe_row_ok({**row, "price": 4.0}, {})
    assert not ok and why == "h4_price"
    ok, why = desk_h4.universe_row_ok({**row, "avg_dollar_vol_50d": 100}, {})
    assert not ok and why == "h4_dollar_vol"
    ok, why = desk_h4.universe_row_ok({**row, "rs_rating": 40}, {})
    assert not ok and why == "h4_rs"


def test_h4_spread_gate():
    ok, why = desk_h4.spread_ok(10.00, 10.005, {"h4_max_spread_pct": 0.10})
    assert ok
    ok, why = desk_h4.spread_ok(10.00, 10.05, {"h4_max_spread_pct": 0.10})
    assert not ok and why == "h4_spread"
    ok, why = desk_h4.spread_ok(None, 10.0, {})
    assert not ok and why == "h4_no_quote"


def test_h4_partition_and_held():
    state = {
        "AAA": {"strategy": "h4_swing", "qty": 10},
        "BBB": {"strategy": "day_scalp_v0", "qty": 5},
    }
    assert desk_h4.held_symbols(state) == {"AAA"}
    keep, drop = desk_h4.partition_state(state)
    assert "AAA" in keep and "BBB" in drop


def test_h4_stop_price():
    assert desk_h4.stop_price(100.0, {"h4_stop_pct": 2.0}) == pytest.approx(98.0)


def test_h4_paper_does_not_use_scalp_gates():
    cfg = {
        "desk_product": "h4_swing",
        "ai_h4_paper": True,
        "h4_min_price": 10.0,
        "h4_min_dollar_vol": 5_000_000.0,
        "h4_max_spread_pct": 0.10,
        "ai_watch_arm_mode": "last",
        "ai_min_reward_risk": 0.5,
        "ai_watch_synth_rr": 1.0,
        "ai_watch_synth_stop_pct": 5.0,
    }
    rec = _rec()
    # Squeeze watch row has no dollar volume — refuse, do not RSI-arm.
    ok, why = ew.should_arm_buy(
        rec, ask=12.0, bid=11.995, cfg=cfg, now=_ts(10, 15))
    assert ok is False
    assert why == "h4_dollar_vol"
    rec["avg_dollar_vol_50d"] = 8_000_000.0
    rec["rs_rating"] = 90
    ok, why = ew.should_arm_buy(
        rec, ask=12.0, bid=11.995, cfg=cfg, now=_ts(10, 15))
    assert ok is True and why == "h4_last"
