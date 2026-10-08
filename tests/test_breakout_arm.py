"""Breakout entry trigger (operator 10/8): replaces only the %R square, never the hard gates."""
import ai_entry_watch as ew

BRK = {"ob_brk_dist_pct": 0.15, "ob_resist_0.3": False}


def _fake_should_arm(result_with_rules_off):
    calls = []

    def f(rec, ask=None, bid=None, cfg=None, now=None):
        calls.append(cfg)
        assert cfg["ai_watch_exhaustion_rules"] is False and cfg["ai_watch_arm_require_indicators"] is False
        return result_with_rules_off
    return f, calls


def test_off_by_default_changes_nothing(monkeypatch):
    assert ew.breakout_arm_mode({}) == "off"
    assert ew.apply_breakout_arm({}, 10, 9.99, {}, 0, BRK, False, "wait_exh") == (False, "wait_exh")
    assert ew.apply_breakout_arm({}, 10, 9.99, {}, 0, {}, True, "last_presquare") == (True, "last_presquare")


def test_breakout_arms_without_a_square_when_hard_gates_pass(monkeypatch):
    f, calls = _fake_should_arm((True, "exhaustion_off"))
    monkeypatch.setattr(ew, "should_arm_buy", f)
    for mode in ("replace", "either"):
        cfg = {"ai_watch_breakout_arm": mode}
        assert ew.apply_breakout_arm({}, 10, 9.99, cfg, 0, BRK, False, "exh_falling") == (True, "breakout_arm")


def test_a_breakout_never_passes_a_hard_gate(monkeypatch):
    f, _ = _fake_should_arm((False, "spread_wide"))
    monkeypatch.setattr(ew, "should_arm_buy", f)
    cfg = {"ai_watch_breakout_arm": "replace"}
    assert ew.apply_breakout_arm({}, 10, 9.99, cfg, 0, BRK, False, "exh_falling") == (False, "spread_wide")


def test_replace_refuses_a_square_without_a_breakout_and_either_keeps_it():
    assert ew.apply_breakout_arm({}, 10, 9.99, {"ai_watch_breakout_arm": "replace"}, 0, {"ob_brk_dist_pct": None},
                                 True, "last_presquare") == (False, "no_breakout")
    assert ew.apply_breakout_arm({}, 10, 9.99, {"ai_watch_breakout_arm": "either"}, 0, {"ob_brk_dist_pct": None},
                                 True, "last_presquare") == (True, "last_presquare")


def test_only_the_clean_band_counts():
    cfg = {"ai_watch_breakout_arm": "replace"}
    for brk in (0.05, 0.30, 0.8):          # poke / chase are not clean
        assert ew.apply_breakout_arm({}, 10, 9.99, cfg, 0, {"ob_brk_dist_pct": brk}, True, "x") == (False, "no_breakout")


def test_wired_at_the_arm_site_and_keeps_the_reading_on():
    src = open(ew.__file__).read()
    assert "ok_arm, why = apply_breakout_arm(rec, ask_f, bid_f, cfg, t0, _ob_arm, ok_arm, why)" in src
    assert ew._ob_observe_on({"ai_watch_breakout_arm": "either"})
