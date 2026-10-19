"""The any-%R-turn arm lane (ai_watch_exh_any_turn_arm): the prescreen BOTHUP turn, read live. Off by default."""
import ai_entry_watch as aew


def _rec(sym, fast, rising=True, slow_rising=True, falling=False):
    return {"symbol": sym, "indicator": {"pctr": fast, "pctr_rising": rising, "pctr_slow_rising": slow_rising,
                                         "pctr_falling": falling, "pctr_src": "live"}}


CFG = {"ai_watch_exh_any_turn_arm": True}


def setup_function(_f):
    aew._ANY_TURN_STATE.clear()


def test_off_by_default():
    assert aew.exh_any_turn_arm_enabled({}) is False
    assert aew.exh_any_turn_arm_enabled(CFG) is True


def test_turn_out_of_a_dip_arms():
    t = 1000.0
    assert aew._any_turn_allows_buy(_rec("X", -85, rising=False, slow_rising=False), CFG, now=t) == (False, "wait_turn")
    assert aew._any_turn_allows_buy(_rec("X", -60), CFG, now=t + 60) == (True, "any_turn")


def test_no_dip_no_arm_and_late_no_arm():
    assert aew._any_turn_allows_buy(_rec("Y", -55), CFG, now=1.0)[0] is False
    aew._any_turn_allows_buy(_rec("Z", -90, rising=False), CFG, now=1.0)
    assert aew._any_turn_allows_buy(_rec("Z", -30), CFG, now=30.0) == (False, "wait_turn")


def test_one_line_rising_is_not_a_turn():
    aew._any_turn_allows_buy(_rec("W", -90, rising=False), CFG, now=1.0)
    assert aew._any_turn_allows_buy(_rec("W", -60, slow_rising=False), CFG, now=30.0)[0] is False


def test_stale_after_max_age_and_falling_cancels():
    aew._any_turn_allows_buy(_rec("S", -90, rising=False), CFG, now=1.0)
    assert aew._any_turn_allows_buy(_rec("S", -60), CFG, now=10.0)[0] is True
    assert aew._any_turn_allows_buy(_rec("S", -55, falling=True), CFG, now=20.0) == (False, "exh_falling")
    assert aew._any_turn_allows_buy(_rec("S", -50), CFG, now=75.0) == (False, "turn_stale")


def test_the_dip_must_be_inside_the_look_window():
    aew._any_turn_allows_buy(_rec("D", -90, rising=False), CFG, now=1.0)
    assert aew._any_turn_allows_buy(_rec("D", -60), CFG, now=1.0 + 700)[0] is False


def test_paint_peek_does_not_advance_the_latch():
    aew._any_turn_allows_buy(_rec("P", -90, rising=False), CFG, now=1.0)
    aew._MID_RISE_PEEK.on = True
    try:
        assert aew._any_turn_allows_buy(_rec("P", -60), CFG, now=5.0)[0] is True
    finally:
        aew._MID_RISE_PEEK.on = False
    assert aew._ANY_TURN_STATE["P"][1] is None


def test_exhaustion_dispatches_to_the_lane_only_when_on():
    aew._any_turn_allows_buy(_rec("Q", -90, rising=False), CFG, now=1.0)
    ok, why = aew.exhaustion_allows_buy(_rec("Q", -60), {**CFG, "ai_watch_require_live_pctr": True}, now=5.0)
    assert ok and why == "any_turn"
