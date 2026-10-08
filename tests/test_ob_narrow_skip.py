"""Narrow order-block skip (operator 10/8, PEP/BSX): overbought AND resistance within 0.10% above."""
import ai_entry_watch as ew

ON = {"ai_watch_ob_resist_ob_skip": True, "ai_watch_ob_resist_ob_room_pct": 0.10}


def rec(ob):
    return {"indicator": {"pctr_ob": ob}}


def test_overbought_just_under_resistance_is_refused():
    assert ew._ob_resist_refusal({"ob_resist_0.3": True, "ob_room_pct": 0.04}, ON, rec(True))   # PEP 10/8
    assert ew._ob_resist_refusal({"ob_resist_0.3": True, "ob_room_pct": 0.07}, ON, rec(True))   # BSX 10/8
    assert ew._ob_resist_refusal({"ob_resist_0.3": True, "ob_room_pct": 0.0}, ON, rec(True))    # inside the zone


def test_not_refused_when_not_overbought_or_room_is_wider_or_no_resistance():
    assert not ew._ob_resist_refusal({"ob_resist_0.3": True, "ob_room_pct": 0.04}, ON, rec(False))
    assert not ew._ob_resist_refusal({"ob_resist_0.3": True, "ob_room_pct": 0.25}, ON, rec(True))
    assert not ew._ob_resist_refusal({"ob_resist_0.3": False, "ob_room_pct": None}, ON, rec(True))
    assert not ew._ob_resist_refusal({}, ON, rec(True))                       # no reading: fail open
    assert not ew._ob_resist_refusal(None, ON, rec(True))
    assert not ew._ob_resist_refusal({"ob_room_pct": 0.04}, ON, None)


def test_off_by_default_and_the_broad_skip_is_unchanged():
    f = {"ob_resist_0.3": True, "ob_room_pct": 0.04}
    assert not ew._ob_resist_refusal(f, {}, rec(True))
    assert ew._ob_resist_refusal(f, {"ai_watch_ob_resist_skip": True}, rec(False))
    assert ew._ob_resist_refusal(f, {"ai_watch_ob_resist_skip": True})          # old two-argument call still works


def test_the_reading_stays_on_for_the_narrow_skip():
    assert ew._ob_observe_on({"ai_watch_ob_resist_ob_skip": True})


def test_both_call_sites_pass_the_record():
    src = open(ew.__file__).read()
    assert "_ob_resist_refusal(_ob_arm, cfg, rec)" in src and "_ob_resist_refusal(_ob_fields, cfg, rec)" in src
    assert '"ob_resist", "ob_resist_ob"' in src
