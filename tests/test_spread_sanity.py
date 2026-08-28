"""A logged spread_r wide enough to be impossible must not size the trail.

Measured 2026-08-28 against SIP — the NBBO orders actually route to — within
20 seconds of each of that day's fills:

    PURR  5.547R logged  ->  $0.02 =  0.032R actual   172x
    ASST  1.368R         ->  $0.04 =  0.035R           39x
    ASST  1.315R         ->  $0.03 =  0.026R           50x
    ASST  1.079R         ->  $0.04 =  0.034R           32x
    ASST  1.071R         ->  $0.03 =  0.026R           42x
    CRSR  0.262R         ->  $0.07 =  0.115R          2.3x
    ...every other reading that day                 1.3-6.0x

The error is 30-170x on exactly the names that trip the k floor and 1.3-6x on
everything else, so it is not a scale factor and cannot be calibrated away —
which is the same conclusion the premarket spread study reached from the
other direction.

The subtlety worth pinning: capping the VALUE at the bound would be a no-op,
because ai_local_trail_give_spread_max_r already caps the resulting give at
0.5R — 1.07 and 5.55 produce an identical shelf today. Only treating the
reading as ABSENT changes anything, and the tests below are written to fail
if someone "simplifies" absence back into a clamp.
"""
import ai_positions as cp


def _cfg(**kw):
    base = {
        "ai_local_trail_give_r": 0.05,
        "ai_local_trail_give_open_r": 0.05,
        "ai_local_trail_give_spread_k": 1.0,
        "ai_local_trail_give_spread_max_r": 0.50,
        "ai_local_trail_min_give_px": 0.01,
        "ai_local_trail_give_max_pct": 0.0,
    }
    base.update(kw)
    return base


# ── the helper ───────────────────────────────────────────────────────────

def test_a_real_reading_passes_through_unchanged():
    for v in (0.011, 0.032, 0.115, 0.262, 0.499):
        assert cp._sane_spread_r(v, _cfg()) == v


def test_an_impossible_reading_is_absent_not_clamped():
    """PURR's 5.547R was a 2-cent book. None, not 0.5."""
    for v in (0.501, 1.071, 1.368, 5.547):
        assert cp._sane_spread_r(v, _cfg()) is None


def test_junk_and_non_positive_readings_are_absent():
    assert cp._sane_spread_r(None, _cfg()) is None
    assert cp._sane_spread_r(0, _cfg()) is None
    assert cp._sane_spread_r(-1.0, _cfg()) is None
    assert cp._sane_spread_r("wide", _cfg()) is None


def test_the_bound_is_configurable_and_zero_disables_it():
    assert cp._sane_spread_r(1.071, _cfg(ai_spread_r_sane_max=2.0)) == 1.071
    assert cp._sane_spread_r(5.547, _cfg(ai_spread_r_sane_max=0.0)) == 5.547


def test_a_missing_config_still_defends():
    """The trail is reached from paths that pass no cfg at all."""
    assert cp._sane_spread_r(5.547, None) is None
    assert cp._sane_spread_r(0.032, None) == 0.032


# ── what it changes about the shelf ──────────────────────────────────────

def test_the_artifact_no_longer_widens_the_give():
    """PURR: entry 12.96, 5% synth stop, spread_r 5.547.

    Before, the k floor asked for 5.547R and the 0.5R cap handed back a
    $0.32 cushion on a stock whose book was two cents wide.
    """
    risk = 12.96 * 0.05
    junk = cp.local_trail_give(12.96, risk, _cfg(), mfe_r=0.3, spread_r=5.547)
    none = cp.local_trail_give(12.96, risk, _cfg(), mfe_r=0.3, spread_r=None)
    assert junk == none, "an impossible spread must size the shelf like no spread"
    assert junk < 0.5 * risk


def test_a_real_wide_book_still_widens_the_give():
    """The floor exists for a reason and must survive this change."""
    risk = 12.19 * 0.05
    wide = cp.local_trail_give(12.19, risk, _cfg(), mfe_r=0.3, spread_r=0.262)
    bare = cp.local_trail_give(12.19, risk, _cfg(), mfe_r=0.3, spread_r=None)
    assert wide > bare


def test_the_cap_would_have_hidden_this_so_the_test_uses_absence():
    """Guards the reasoning, not just the behaviour.

    1.071 and 5.547 both exceed the 0.5R cap, so a clamp-to-bound leaves the
    give identical and the bug alive. If someone rewrites _sane_spread_r to
    return the bound, this fails.
    """
    risk = 23.53 * 0.05
    a = cp.local_trail_give(23.53, risk, _cfg(), mfe_r=0.3, spread_r=1.071)
    b = cp.local_trail_give(23.53, risk, _cfg(), mfe_r=0.3, spread_r=5.547)
    capped = cp.local_trail_give(23.53, risk, _cfg(), mfe_r=0.3, spread_r=0.50)
    assert a == b, "both are artifacts; they must land in the same place"
    assert a < capped, "...and that place is not the cap"


def test_the_seeded_shelf_at_fill_gets_the_same_defence():
    """sim_fill_replay passes a logged spread straight in here, never through
    _pos_spread_r — which is why the bound lives at consumption."""
    risk = 12.96 * 0.05
    junk = cp.initial_local_stop(12.96, risk, _cfg(), spread_r=5.547)
    none = cp.initial_local_stop(12.96, risk, _cfg(), spread_r=None)
    assert junk == none


# ── the record must keep the evidence ────────────────────────────────────

def test_the_raw_reading_is_still_readable_for_diagnosis():
    """_pos_spread_r stays raw: the outcome record is how these artifacts
    were found, and a filtered record cannot count them."""
    assert cp._pos_spread_r({"features": {"spread_r": 5.547}}) == 5.547
