"""square_relax arm-only knobs: unset = live; each knob loosens only its own pre-square condition."""
import ai_entry_watch as ew

BASE = {"rte_threshold": 20, "ai_watch_exh_pre_thr": 35.0, "rte_confluence_max": 15.0, "ai_watch_square_min_count": 1}
T = 1_793_200_000.0


def rec(fast, slow, sym="AAA"):
    ew._PRESQUARE_STREAK.pop(sym, None)
    return {"symbol": sym, "indicator": {"pctr": fast, "pctr_slow": slow, "pctr_rising": True, "pctr_falling": False}}


def verdict(fast, slow, cfg, sym="AAA"):
    return ew._presquare_exh_allows_buy(rec(fast, slow, sym), cfg, now=T)


def test_unset_knobs_match_live_thresholds():
    assert verdict(-30.0, -25.0, BASE)[0] is True            # tight, in band
    assert verdict(-30.0, -12.0, BASE)[1] == "wait_exh"      # gap 18 > 15
    assert verdict(-40.0, -30.0, BASE)[1] == "wait_exh"      # lower line below -35
    assert verdict(-15.0, -10.0, BASE)[1] == "wait_exh"      # both overbought and tight: a full square, not pre-square


def test_tight20_loosens_only_the_gap():
    cfg = {**BASE, "ai_watch_presquare_confluence_max": 20.0}
    assert verdict(-30.0, -12.0, cfg)[0] is True             # gap 18 now allowed
    assert verdict(-40.0, -30.0, cfg)[1] == "wait_exh"       # pre-band unchanged
    assert verdict(-19.0, -5.0, cfg)[1] == "wait_exh"        # both >= -20 and tight under 20: still a full square


def test_pre45_loosens_only_the_band():
    cfg = {**BASE, "ai_watch_presquare_pre_thr": 45.0}
    assert verdict(-40.0, -30.0, cfg)[0] is True
    assert verdict(-30.0, -12.0, cfg)[1] == "wait_exh"       # gap unchanged


def test_random_release_only_from_the_single_condition_envelope():
    cfg = {**BASE, "ai_watch_presquare_rand_release_share": 1.0, "ai_watch_presquare_rand_release_salt": "SQ-x"}
    assert verdict(-30.0, -12.0, cfg)[0] is True             # gap 18: wide-only, in envelope
    assert verdict(-40.0, -30.0, cfg)[0] is True             # lower -40: low-only, in envelope
    assert verdict(-30.0, -2.0, cfg)[1] == "wait_exh"        # gap 28: outside the envelope
    assert verdict(-55.0, -45.0, cfg)[1] == "wait_exh"       # lower -55: outside
    assert verdict(-45.0, -20.0, cfg)[1] == "wait_exh"       # fails two conditions


def test_falling_still_refuses_a_loosened_check():
    cfg = {**BASE, "ai_watch_presquare_confluence_max": 20.0}
    r = rec(-30.0, -12.0)
    r["indicator"]["pctr_falling"] = True
    assert ew._presquare_exh_allows_buy(r, cfg, now=T)[1] == "exh_falling"


def test_counters_record_variant_and_random_releases():
    ew._SQ_COUNTS.clear()
    verdict(-30.0, -12.0, {**BASE, "ai_watch_presquare_confluence_max": 20.0})
    verdict(-30.0, -25.0, {**BASE, "ai_watch_presquare_confluence_max": 20.0})
    assert ew._SQ_COUNTS.get("released_variant") == 1 and ew._SQ_COUNTS.get("checks_passing") == 2
