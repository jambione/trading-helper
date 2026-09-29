"""Leave-oversold arm, seat class, and book closeness."""
from __future__ import annotations

import ai_entry_watch as ew
import book_server as bs


def _cfg(**over):
    c = {
        "ai_watch_exhaustion_rules": True,
        "ai_watch_exh_square_arm": True,
        "ai_watch_exh_oversold_arm": True,
        "ai_watch_exh_heating_with_square": False,
        "ai_watch_exh_mid_rise_arm": False,
        "ai_watch_square_min_count": 2,
        "ai_watch_os_leave_max_age_sec": 60.0,
        "ai_watch_require_exh_rising": True,
        "ai_watch_require_exhaustion_data": True,
        "ai_watch_tv_exh_rsi": False,
        "rte_threshold": 20,
        "rte_confluence_max": 15.0,
        "ai_watch_exh_pre_thr": 35.0,
        "ai_watch_ob_allow_hot": False,
    }
    c.update(over)
    return c


def _rec(sym, fast, slow, *, rising=True):
    return {
        "symbol": sym,
        "source": "movers",
        "indicator": {
            "pctr": float(fast),
            "pctr_slow": float(slow),
            "pctr_rising": rising,
            "pctr_falling": not rising,
            "pctr_slow_rising": rising,
            "pctr_src": "live",
        },
    }


def setup_function():
    ew._SQUARE_STREAK.clear()
    ew._OS_STREAK.clear()
    ew._OS_QUALIFIED.clear()
    ew._OS_LEFT.clear()
    ew._MID_RISE_PEEK.on = False


def teardown_function():
    setup_function()


def test_leave_oversold_opens_after_two_triangles():
    cfg = _cfg()
    t0 = 1_000_000.0
    deep = _rec("AAL", -90, -85, rising=False)
    ok, why = ew.exhaustion_allows_buy(deep, cfg, now=t0)
    assert ok is False and why == "os_confirm"
    ok, why = ew.exhaustion_allows_buy(deep, cfg, now=t0 + 0.05)
    assert ok is False and why == "os_confirm"  # same-poll debounce
    ok, why = ew.exhaustion_allows_buy(deep, cfg, now=t0 + 1.0)
    assert ok is False and why == "wait_os_leave"
    assert deep.get("os_qualified") is True
    leave = _rec("AAL", -70, -74, rising=True)
    leave["os_qualified"] = True
    ok, why = ew.exhaustion_allows_buy(leave, cfg, now=t0 + 2.0)
    assert ok is True and why == "oversold_leave"


def test_wide_gap_while_deep_is_not_a_leave():
    cfg = _cfg()
    t0 = 1_000_000.0
    ew.exhaustion_allows_buy(_rec("BB", -92, -88), cfg, now=t0)
    ew.exhaustion_allows_buy(_rec("BB", -92, -88), cfg, now=t0 + 1.0)
    # Both still ≤ −80, gap 17. Widening is not a leave.
    wide = _rec("BB", -98, -81, rising=True)
    ok, why = ew.exhaustion_allows_buy(wide, cfg, now=t0 + 2.0)
    assert ok is False and why == "exh_not_tight"
    assert "BB" not in ew._OS_LEFT


def test_leave_requires_rising_and_expires():
    cfg = _cfg()
    t0 = 1_000_000.0
    ew.exhaustion_allows_buy(_rec("CC", -90, -86), cfg, now=t0)
    ew.exhaustion_allows_buy(_rec("CC", -90, -86), cfg, now=t0 + 1.0)
    flat = _rec("CC", -70, -72, rising=False)
    ok, why = ew.exhaustion_allows_buy(flat, cfg, now=t0 + 2.0)
    assert ok is False and why == "os_leave_not_rising"
    # Still in the low band, but the leave window has closed.
    late = _rec("CC", -78, -74, rising=True)
    ok, why = ew.exhaustion_allows_buy(late, cfg, now=t0 + 2.0 + 61.0)
    assert ok is False and why == "stale_os_leave"


def test_square_still_wins_when_both_arms_on():
    cfg = _cfg()
    rec = _rec("SMCI", -13, -4)
    ok, why = ew.exhaustion_allows_buy(rec, cfg, now=1_000_000.0)
    assert ok is False and why == "square_confirm"
    ok, why = ew.exhaustion_allows_buy(rec, cfg, now=1_000_001.0)
    assert ok is True and why == "overbought"


def test_missing_os_key_keeps_square_only():
    cfg = _cfg()
    del cfg["ai_watch_exh_oversold_arm"]
    rec = _rec("OS", -90, -85, rising=True)
    ok, why = ew.exhaustion_allows_buy(rec, cfg, now=1_000_000.0)
    assert ok is False
    assert why != "os_confirm"
    assert "OS" not in ew._OS_STREAK


def test_paint_peek_does_not_advance_os_streak():
    cfg = _cfg()
    rec = _rec("PEEK", -91, -84)
    ew._MID_RISE_PEEK.on = True
    ew.exhaustion_allows_buy(rec, cfg, now=1_000_000.0)
    ew.exhaustion_allows_buy(rec, cfg, now=1_000_010.0)
    assert "PEEK" not in ew._OS_STREAK
    ew._MID_RISE_PEEK.on = False


def test_classify_keeps_square_pre_far_and_adds_os():
    cfg = _cfg()
    assert ew.classify_exh_seat(
        {"indicator": _rec("A", -13, -4)["indicator"]}, cfg)[0] == "square"
    assert ew.classify_exh_seat(
        {"indicator": _rec("A", -32, -32)["indicator"]}, cfg)[0] == "pre_square"
    assert ew.classify_exh_seat(
        {"indicator": _rec("A", -39, -64)["indicator"]}, cfg)[0] == "far"
    assert ew.classify_exh_seat(
        {"indicator": _rec("A", -90, -85)["indicator"]}, cfg)[0] == "os_triangle"
    assert ew.classify_exh_seat(
        {"indicator": _rec("A", -70, -68, rising=True)["indicator"]}, cfg,
    )[0] == "os_leave"


def test_os_only_rank_prefers_low_exh():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 9, 24, 10, 30, tzinfo=ZoneInfo("America/New_York")).timestamp()
    rows = [
        {"symbol": "HIGH", "source": "movers", "day_chg_pct": 8.0,
         "indicator": {"pctr": -10.0, "pctr_slow": -8.0,
                       "pctr_rising": True, "pctr_slow_rising": True},
         "rvol_pace_sip": 3.0},
        {"symbol": "LOW", "source": "research", "day_chg_pct": 1.0,
         "indicator": {"pctr": -90.0, "pctr_slow": -85.0,
                       "pctr_rising": True, "pctr_slow_rising": True},
         "rvol_pace_sip": 1.2},
    ]
    ranked = bs.rank_candidates(rows, now=now, cfg={
        "ai_watch_exh_square_arm": False,
        "ai_watch_exh_oversold_arm": True,
        "rte_threshold": 20,
        "ai_watch_exh_pre_thr": 35,
        "rte_confluence_max": 15,
    })
    assert [r["symbol"] for r in ranked][0] == "LOW"


def test_missing_os_flag_still_prefers_pre_square():
    """Partial cfg: −90 must not outrank a pre-square."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 9, 24, 10, 30, tzinfo=ZoneInfo("America/New_York")).timestamp()
    rows = [
        {"symbol": "FAR", "source": "research", "day_chg_pct": 2.0,
         "indicator": {"pctr": -90.0, "pctr_slow": -88.0}, "rvol_pace_sip": 0.5},
        {"symbol": "PRE", "source": "movers", "day_chg_pct": 6.0,
         "indicator": {
             "pctr": -28.0, "pctr_slow": -30.0,
             "pctr_rising": True, "pctr_slow_rising": True,
         }, "rvol_pace_sip": 2.0},
    ]
    ranked = bs.rank_candidates(rows, now=now, cfg={
        "ai_watch_exh_square_arm": True,
        "rte_threshold": 20,
        "ai_watch_exh_pre_thr": 35.0,
        "rte_confluence_max": 15.0,
    })
    assert ranked[0]["symbol"] == "PRE"
