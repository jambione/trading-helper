"""The any-%R-turn arm lane (ai_watch_exh_any_turn_arm). Off by default."""
import ai_entry_watch as aew


def _rec(sym, fast, rising=True, slow_rising=True, falling=False):
    return {"symbol": sym, "indicator": {"pctr": fast, "pctr_rising": rising, "pctr_slow_rising": slow_rising,
                                         "pctr_falling": falling, "pctr_src": "live"}}


CFG = {"ai_watch_exh_any_turn_arm": True}


def setup_function(_f):
    aew._ANY_TURN_STATE.clear()


def _arm(sym, t0=1000.0):
    aew._any_turn_allows_buy(_rec(sym, -85, rising=False, slow_rising=False), CFG, now=t0)
    aew._any_turn_allows_buy(_rec(sym, -75), CFG, now=t0 + 20)      # both rising, first reading
    return aew._any_turn_allows_buy(_rec(sym, -60), CFG, now=t0 + 40)  # second reading: onset


def test_off_by_default():
    assert aew.exh_any_turn_arm_enabled({}) is False
    assert aew.exh_any_turn_arm_enabled(CFG) is True


def test_turn_needs_two_rising_readings_out_of_a_dip():
    t = 1000.0
    aew._any_turn_allows_buy(_rec("X", -85, rising=False, slow_rising=False), CFG, now=t)
    assert aew._any_turn_allows_buy(_rec("X", -75), CFG, now=t + 20) == (False, "wait_turn")
    assert aew._any_turn_allows_buy(_rec("X", -60), CFG, now=t + 40) == (True, "any_turn")


def test_no_dip_no_arm_and_late_no_arm():
    for k in range(3):
        assert aew._any_turn_allows_buy(_rec("Y", -55), CFG, now=1.0 + k)[0] is False
    aew._any_turn_allows_buy(_rec("Z", -90, rising=False), CFG, now=1.0)
    aew._any_turn_allows_buy(_rec("Z", -35), CFG, now=20.0)
    assert aew._any_turn_allows_buy(_rec("Z", -30), CFG, now=40.0) == (False, "wait_turn")


def test_one_line_rising_is_not_a_turn():
    aew._any_turn_allows_buy(_rec("W", -90, rising=False), CFG, now=1.0)
    aew._any_turn_allows_buy(_rec("W", -70, slow_rising=False), CFG, now=20.0)
    assert aew._any_turn_allows_buy(_rec("W", -60, slow_rising=False), CFG, now=40.0)[0] is False


def test_falling_ends_the_arm_and_a_new_dip_is_needed():
    assert _arm("S")[0] is True
    assert aew._any_turn_allows_buy(_rec("S", -55, falling=True, rising=False), CFG, now=1050.0) == (False, "exh_falling")
    aew._any_turn_allows_buy(_rec("S", -50), CFG, now=1060.0)
    assert aew._any_turn_allows_buy(_rec("S", -45), CFG, now=1070.0) == (False, "wait_turn")
    aew._any_turn_allows_buy(_rec("S", -80, rising=False), CFG, now=1080.0)
    aew._any_turn_allows_buy(_rec("S", -70), CFG, now=1090.0)
    assert aew._any_turn_allows_buy(_rec("S", -60), CFG, now=1100.0) == (True, "any_turn")


def test_stale_after_max_age_does_not_rearm_without_a_new_dip():
    assert _arm("T")[0] is True
    assert aew._any_turn_allows_buy(_rec("T", -50), CFG, now=1101.0) == (False, "turn_stale")
    assert aew._any_turn_allows_buy(_rec("T", -50), CFG, now=1110.0) == (False, "wait_turn")


def test_the_dip_must_be_inside_the_look_window():
    aew._any_turn_allows_buy(_rec("D", -90, rising=False), CFG, now=1.0)
    aew._any_turn_allows_buy(_rec("D", -60), CFG, now=1.0 + 690)
    assert aew._any_turn_allows_buy(_rec("D", -60), CFG, now=1.0 + 700)[0] is False


def test_paint_peek_does_not_advance_the_latch():
    aew._any_turn_allows_buy(_rec("P", -90, rising=False), CFG, now=1.0)
    aew._any_turn_allows_buy(_rec("P", -75), CFG, now=20.0)
    aew._MID_RISE_PEEK.on = True
    try:
        assert aew._any_turn_allows_buy(_rec("P", -60), CFG, now=40.0)[0] is True
    finally:
        aew._MID_RISE_PEEK.on = False
    assert aew._ANY_TURN_STATE["P"]["since"] is None


def test_exhaustion_dispatches_to_the_lane_only_when_on():
    aew._any_turn_allows_buy(_rec("Q", -90, rising=False), CFG, now=1.0)
    aew._any_turn_allows_buy(_rec("Q", -75), CFG, now=20.0)
    ok, why = aew.exhaustion_allows_buy(_rec("Q", -60), {**CFG, "ai_watch_require_live_pctr": True}, now=40.0)
    assert ok and why == "any_turn"
