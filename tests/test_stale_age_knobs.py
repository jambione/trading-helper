"""Arm-only tape-age knobs (docs/studies/stale_age_replay_prereg.json): off = unchanged; on = arm-only."""
import ai_entry_watch as ew

T = 1_793_120_000.0
OFF = {}
AGE30 = {"ai_watch_arm_max_age_sec": 30.0}


def rec(age=20.0, src="stale_tape", sym="AAA"):
    return {"symbol": sym, "status": "watching", "last_ask": 50.0, "last_ask_src": src,
            "last_ask_age_sec": age, "last_ask_ts": T - age, "price_age_sec": age}


def test_knobs_off_return_the_source_verdict():
    for src_stale in (True, False):
        assert ew.arm_tape_stale(rec(), "AAA", T, OFF, src_stale) is src_stale
        assert ew.arm_tape_stale(rec(), "AAA", T, {"ai_watch_arm_age_mode": "src"}, src_stale) is src_stale


def test_arm_ceiling_releases_a_20s_print_but_not_a_40s_one():
    assert ew.arm_tape_stale(rec(20.0), "AAA", T, AGE30, True) is False
    assert ew.arm_tape_stale(rec(40.0), "AAA", T, AGE30, True) is True
    assert ew.arm_tape_stale(rec(40.0), "AAA", T, AGE30, False) is False   # src mode never adds a refusal
    r = rec()
    r.pop("last_ask_ts"); r.pop("last_ask_age_sec"); r.pop("price_age_sec")
    assert ew.arm_tape_stale(r, "AAA", T, AGE30, True) is True              # no age: stale, fail closed


def test_gap_ceiling_still_applies():
    ew._PRINT_GAPS["GAP"] = [T - 1.0, [40.0] * 10]
    cfg = {**AGE30, "ai_watch_tape_gap_mult": 1.5, "ai_watch_tape_gap_max_sec": 60.0}
    assert ew.arm_tape_stale(rec(50.0, sym="GAP"), "GAP", T, cfg, True) is False   # 1.5 x 40 = 60 >= 50
    assert ew.arm_tape_stale(rec(70.0, sym="GAP"), "GAP", T, cfg, True) is True
    ew._PRINT_GAPS.pop("GAP", None)


def test_age_mode_judges_by_print_age_through_the_hook(monkeypatch):
    cfg = {"ai_watch_arm_age_mode": "age"}
    monkeypatch.setattr(ew, "_arm_tape_age", lambda r, s, t: 25.0)
    assert ew.arm_tape_stale(rec(src="stream"), "AAA", T, cfg, False) is True       # 25 > 15
    assert ew.arm_tape_stale(rec(src="stream"), "AAA", T, {**cfg, **AGE30}, False) is False
    monkeypatch.setattr(ew, "_arm_tape_age", lambda r, s, t: None)
    assert ew.arm_tape_stale(rec(src="stream"), "AAA", T, cfg, False) is True       # no print: stale


def test_random_release_is_deterministic_near_the_share_and_only_above_15s():
    cfg = {"ai_watch_stale_rand_release_share": 0.5, "ai_watch_stale_rand_release_salt": "SA-1"}
    got = [ew.arm_tape_stale(rec(40.0, sym=f"S{i}"), f"S{i}", T, cfg, True) for i in range(400)]
    again = [ew.arm_tape_stale(rec(40.0, sym=f"S{i}"), f"S{i}", T, cfg, True) for i in range(400)]
    assert got == again
    assert 150 < sum(1 for g in got if g is False) < 250
    assert ew.arm_tape_stale(rec(5.0), "AAA", T, {**cfg, "ai_watch_stale_rand_release_share": 1.0,
                                                   "ai_watch_arm_age_mode": "age"}, False) is False   # fresh anyway


def test_should_arm_buy_stale_branch_follows_the_arm_verdict(monkeypatch):
    calls = []
    real = ew.arm_tape_stale

    def spy(r, s, now, cfg, src_stale):
        calls.append(src_stale)
        return real(r, s, now, cfg, src_stale)
    monkeypatch.setattr(ew, "arm_tape_stale", spy)
    ok, why = ew.should_arm_buy(rec(age=40.0), ask=50.0, bid=None, cfg=AGE30, now=T)
    assert (ok, why) == (False, "stale_quote") and calls == [True]


def test_admission_and_eviction_do_not_read_the_arm_knobs():
    for age in (5.0, 20.0, 40.0):
        for src in ("stream", "stale_tape"):
            r = rec(age, src)
            assert ew._arm_ready_young_tape(dict(r), {}, now=T) == ew._arm_ready_young_tape(dict(r), {**AGE30}, now=T)
            assert ew._is_unarmable_stale_watching(dict(r), {}, now=T) == \
                ew._is_unarmable_stale_watching(dict(r), {**AGE30, "ai_watch_arm_age_mode": "age"}, now=T)
