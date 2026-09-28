"""Square entry arm: dual-OB+tight with min consecutive square polls."""
from __future__ import annotations

import time

import pytest

import ai_entry_watch as ew


def _cfg(**over):
    c = {
        "ai_watch_exhaustion_rules": True,
        "ai_watch_exh_square_arm": True,
        "ai_watch_exh_heating_with_square": False,
        "ai_watch_exh_mid_rise_arm": False,
        "ai_watch_square_min_count": 2,
        "ai_watch_square_max_age_sec": 60.0,
        "ai_watch_require_exh_rising": True,
        "ai_watch_require_exhaustion_data": True,
        "ai_watch_tv_exh_rsi": False,
        "rte_threshold": 20,
        "rte_confluence_max": 15.0,
        "ai_watch_ob_allow_hot": False,
    }
    c.update(over)
    return c


def _rec(sym: str, fast: float, slow: float, **extra):
    rising = extra.pop("rising", True)
    falling = extra.pop("falling", False)
    ind = {
        "pctr": float(fast),
        "pctr_slow": float(slow),
        "pctr_rising": rising,
        "pctr_falling": falling,
        "pctr_slow_rising": rising,
        "pctr_src": "live",
    }
    ind.update(extra.pop("ind", {}))
    row = {"symbol": sym, "indicator": ind, "source": "movers"}
    row.update(extra)
    return row


@pytest.fixture(autouse=True)
def _clear_square_streak():
    ew._SQUARE_STREAK.clear()
    yield
    ew._SQUARE_STREAK.clear()


def test_smci_class_arms_after_two_square_polls():
    cfg = _cfg()
    rec = _rec("SMCI", -13.0, -4.0)
    t0 = 1_000_000.0
    ok1, why1 = ew.exhaustion_allows_buy(rec, cfg, now=t0)
    assert ok1 is False and why1 == "square_confirm"
    # Same-poll double call must not count as a second square.
    ok_same, why_same = ew.exhaustion_allows_buy(rec, cfg, now=t0 + 0.05)
    assert ok_same is False and why_same == "square_confirm"
    ok2, why2 = ew.exhaustion_allows_buy(rec, cfg, now=t0 + 1.0)
    assert ok2 is True and why2 == "overbought"
    assert int(rec.get("square_streak") or 0) >= 2


def test_rklb_class_refuses_wide_gap():
    cfg = _cfg()
    rec = _rec("RKLB", -39.0, -64.0)
    ok, why = ew.exhaustion_allows_buy(rec, cfg, now=1_000_000.0)
    assert ok is False and why == "exh_not_tight"
    assert "RKLB" not in ew._SQUARE_STREAK


def test_one_square_is_not_enough():
    cfg = _cfg(ai_watch_square_min_count=2)
    rec = _rec("AAA", -10.0, -8.0)
    ok, why = ew.exhaustion_allows_buy(rec, cfg, now=1_000_000.0)
    assert ok is False and why == "square_confirm"


def test_leave_square_resets_streak():
    cfg = _cfg()
    rec = _rec("BBB", -10.0, -8.0)
    ew.exhaustion_allows_buy(rec, cfg, now=1_000_000.0)
    assert ew._SQUARE_STREAK.get("BBB", (0, 0))[0] == 1
    left = _rec("BBB", -40.0, -35.0)
    ok, why = ew.exhaustion_allows_buy(left, cfg, now=1_000_001.0)
    assert ok is False
    assert why in ("wait_exh", "exh_not_tight")
    assert "BBB" not in ew._SQUARE_STREAK


def test_paint_peek_does_not_advance_streak():
    cfg = _cfg()
    rec = _rec("CCC", -12.0, -9.0)
    ew._MID_RISE_PEEK.on = True
    try:
        ok, why = ew.exhaustion_allows_buy(rec, cfg, now=1_000_000.0)
        assert ok is False
        assert why in ("wait_square", "square_confirm")
        assert ew._SQUARE_STREAK.get("CCC", (0, 0))[0] == 0
    finally:
        ew._MID_RISE_PEEK.on = False
    ok2, why2 = ew.exhaustion_allows_buy(rec, cfg, now=1_000_000.0)
    assert ok2 is False and why2 == "square_confirm"
    assert ew._SQUARE_STREAK.get("CCC", (0, 0))[0] == 1


def test_mid_rise_still_exclusive_when_on():
    cfg = _cfg(ai_watch_exh_mid_rise_arm=True, ai_watch_exh_square_arm=True)
    # Dual OB square would pass square arm, but mid-rise owns the lane.
    rec = _rec("DDD", -10.0, -8.0)
    ok, why = ew.exhaustion_allows_buy(rec, cfg, now=1_000_000.0)
    assert ok is False
    assert why in ("wait_mid_rise", "mid_rise_stale", "mid_rise_lost", "exh_falling")


def test_stale_square_refuses_late_climax():
    cfg = _cfg(ai_watch_square_min_count=1, ai_watch_square_max_age_sec=5.0)
    now = 1_000_000.0
    rec = _rec("EEE", -10.0, -8.0, square_since=now - 30.0)
    ok, why = ew.exhaustion_allows_buy(rec, cfg, now=now)
    assert ok is False and why == "stale_square"
