"""One arm (fast %R -50 cross) and the peak-based ratchet — 2026-09-23."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_entry_watch as ew  # noqa: E402
import ai_positions as cp  # noqa: E402

ON = {"ai_watch_exh_mid_rise_arm": True, "ai_watch_mid_rise_level": -50.0,
      "ai_watch_mid_rise_max_age_sec": 60.0}


@pytest.fixture(autouse=True)
def _fresh_state():
    ew._MID_RISE_STATE.clear()
    yield
    ew._MID_RISE_STATE.clear()


def _rec(fast, slow_rising=True, falling=False, sym="ACME"):
    return {"symbol": sym, "indicator": {
        "pctr": fast, "pctr_slow": -70.0, "pctr_slow_rising": slow_rising,
        "pctr_falling": falling}}


def test_cross_up_through_minus_50_arms():
    assert ew._mid_rise_allows_buy(_rec(-60), ON, now=100) == (False, "wait_mid_rise")
    assert ew._mid_rise_allows_buy(_rec(-45), ON, now=110) == (True, "mid_rise")


def test_cross_needs_slow_line_rising():
    ew._mid_rise_allows_buy(_rec(-60), ON, now=100)
    assert ew._mid_rise_allows_buy(_rec(-45, slow_rising=False), ON, now=110) == (
        False, "wait_mid_rise")


def test_already_above_is_not_a_cross():
    ew._mid_rise_allows_buy(_rec(-40), ON, now=100)
    assert ew._mid_rise_allows_buy(_rec(-30), ON, now=110) == (False, "wait_mid_rise")


def test_cross_goes_stale_after_max_age():
    ew._mid_rise_allows_buy(_rec(-60), ON, now=100)
    ew._mid_rise_allows_buy(_rec(-45), ON, now=110)
    assert ew._mid_rise_allows_buy(_rec(-40), ON, now=171) == (False, "mid_rise_stale")


def test_falling_back_under_cancels():
    ew._mid_rise_allows_buy(_rec(-60), ON, now=100)
    ew._mid_rise_allows_buy(_rec(-45), ON, now=110)
    assert ew._mid_rise_allows_buy(_rec(-55), ON, now=120) == (False, "mid_rise_lost")


def test_fast_turning_down_refuses():
    ew._mid_rise_allows_buy(_rec(-60), ON, now=100)
    ew._mid_rise_allows_buy(_rec(-45), ON, now=110)
    assert ew._mid_rise_allows_buy(_rec(-44, falling=True), ON, now=115) == (
        False, "exh_falling")


def test_state_is_per_symbol():
    ew._mid_rise_allows_buy(_rec(-60, sym="AAA"), ON, now=100)
    assert ew._mid_rise_allows_buy(_rec(-45, sym="BBB"), ON, now=110) == (
        False, "wait_mid_rise")


def test_one_arm_is_exclusive_even_with_square_on():
    # The heating lane below mid_rise is never consulted while it is on.
    cfg = {**ON, "ai_watch_exhaustion_rules": True, "ai_watch_exh_square_arm": True}
    ok, why = ew.exhaustion_allows_buy(_rec(-60), cfg)
    assert (ok, why) == (False, "wait_mid_rise")


def test_off_by_default():
    assert ew.exh_mid_rise_arm_enabled({}) is False


# ── peak ratchet ────────────────────────────────────────────────────────────

def _vktx(**over):
    pos = {"entry_price": 41.90, "entry_stop_price": 39.805, "stop_price": 39.805,
           "risk_per_share": 2.095, "last_seen_price": 41.71, "mfe_r": 0.105,
           "peak_price": 42.12, "local_stop_price": 41.49}
    pos.update(over)
    return pos


CFG = {"ai_local_trail_enabled": True, "ai_local_trail_arm_r": 0.06,
       "ai_local_trail_give_r": 0.35, "ai_local_trail_give_max_pct": 1.0,
       "ai_local_trail_min_give_px": 0, "ai_local_trail_entry_catchup_sec": 0,
       "ai_local_trail_be_at_r": 0.15}


def test_vktx_bug_reproduces_without_peak_give():
    # Damped last (41.71 bid) minus give stays under the seed: stop frozen.
    assert cp.local_profit_stop(_vktx(), {**CFG, "ai_local_trail_peak_give_pct": 0}) \
        == pytest.approx(41.49)


def test_peak_give_ratchets_off_the_high_water_mark():
    # 42.12 * (1 - 0.5%) = 41.9094 -> 41.90
    got = cp.local_profit_stop(_vktx(), {**CFG, "ai_local_trail_peak_give_pct": 0.5})
    assert got == pytest.approx(41.90)


def test_peak_give_waits_for_the_arm():
    # mfe under arm_r: the unarmed branch returns the working shelf.
    pos = _vktx(mfe_r=0.03, peak_price=41.96, last_seen_price=41.90)
    got = cp.local_profit_stop(pos, {**CFG, "ai_local_trail_peak_give_pct": 0.5})
    assert got == pytest.approx(41.49)


def test_peak_give_never_lowers_the_stop():
    pos = _vktx(local_stop_price=42.00)
    got = cp.local_profit_stop(pos, {**CFG, "ai_local_trail_peak_give_pct": 0.5})
    assert got >= 42.00


def test_a_display_read_cannot_consume_the_poll_cross():
    """SMCI 2026-09-25 12:00: the book paint read the cross first (without the
    slow flag), advanced 'last reading' above -50, and the poll never armed."""
    ew._mid_rise_allows_buy(_rec(-56.6), ON, now=100)            # poll
    ew._MID_RISE_PEEK.on = True
    try:                                                          # paint
        ew._mid_rise_allows_buy(_rec(-49.6, slow_rising=False), ON, now=102)
    finally:
        ew._MID_RISE_PEEK.on = False
    assert ew._MID_RISE_STATE["ACME"] == (-56.6, None)            # untouched
    assert ew._mid_rise_allows_buy(_rec(-45.1), ON, now=110) == (True, "mid_rise")


def test_the_book_paint_never_advances_the_latch(monkeypatch):
    ew._mid_rise_allows_buy(_rec(-56.6, sym="SMCI"), ON, now=100)
    seen = []

    def arm(rec, **kw):
        seen.append(ew._mid_rise_allows_buy(_rec(-49.6, slow_rising=False, sym="SMCI"), ON, now=101))
        return False, seen[-1][1]
    monkeypatch.setattr(ew, "should_arm_buy", arm)
    monkeypatch.setattr(ew, "_push_cfg", lambda: ON)
    ew._row_arm_refuse({"ticker": "SMCI", "pctr": -49.6}, 42.85)
    assert seen, "paint must evaluate the arm"
    assert ew._MID_RISE_STATE["SMCI"] == (-56.6, None)
    assert not getattr(ew._MID_RISE_PEEK, "on", False)             # flag cleared
