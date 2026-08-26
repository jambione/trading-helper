"""Who owns a watchlist row when two sources seed the same symbol?

The rule is ownership by deliberateness: research > bro call > desk heat.
A thesis and a named call-out are somebody deciding on a symbol; momentum
and trending are automatic heat that sweeps up whatever is moving.

The bb_live half was documented above _BB_LIVE_SOURCES and never
implemented — _merge_source did not reference the set at all, so it fell
through to "newest wins" in both directions. A bro call took names away
from research theses (which the comment explicitly forbade), and the next
momentum sweep erased the bro attribution. Across 2026-08-20..26 exactly
one bb_live name survived to be counted, so the source read as dead when it
was actually being relabelled: DAIC was admitted on a Trader Bro call on
8/26 and immediately showed up as `momentum`.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402

RESEARCH = "anthropic"
BRO = "bb_live"
DESK = "momentum"


# ── research outranks everything ─────────────────────────────────────────

def test_research_keeps_a_name_against_desk_heat():
    assert ew._merge_source(RESEARCH, DESK) == RESEARCH
    assert ew._merge_source("xai", "trending") == "xai"


def test_research_keeps_a_name_against_a_bro_call():
    """The line the comment promised and the code did not deliver."""
    assert ew._merge_source(RESEARCH, BRO) == RESEARCH
    assert ew._merge_source("grok", "bro") == "grok"


def test_research_takes_a_name_from_anyone_else():
    assert ew._merge_source(DESK, RESEARCH) == RESEARCH
    assert ew._merge_source(BRO, "xai") == "xai"


# ── a call-out outranks automatic heat ───────────────────────────────────

def test_a_bro_call_is_not_erased_by_the_next_momentum_sweep():
    """The bug the operator actually saw: DAIC admitted on a Trader Bro
    call and relabelled `momentum` before it could be read as a bro name."""
    assert ew._merge_source(BRO, DESK) == BRO
    assert ew._merge_source("bro", "trending") == "bro"
    assert ew._merge_source("bb", "stocktwits") == "bb"


def test_a_bro_call_takes_a_name_from_desk_heat():
    assert ew._merge_source(DESK, BRO) == BRO
    assert ew._merge_source("trending", "bro") == "bro"


# ── unchanged behaviour ──────────────────────────────────────────────────

def test_desk_sources_still_let_the_newest_win():
    assert ew._merge_source(DESK, "trending") == "trending"
    assert ew._merge_source("trending", DESK) == DESK


def test_an_empty_source_never_takes_ownership():
    assert ew._merge_source(RESEARCH, "") == RESEARCH
    assert ew._merge_source("", DESK) == DESK
    assert ew._merge_source(BRO, "") == BRO


def test_same_source_twice_is_stable():
    for s in (RESEARCH, BRO, DESK, "trending", "xai"):
        assert ew._merge_source(s, s) == s


def test_bb_live_is_its_own_bucket_not_a_desk_source():
    """If bb_live were ever folded into _DESK_SOURCES the ownership rule
    above would silently invert, so pin the membership itself."""
    assert "bb_live" in ew._BB_LIVE_SOURCES
    assert not (ew._BB_LIVE_SOURCES & ew._DESK_SOURCES)
    assert not (ew._BB_LIVE_SOURCES & ew._RESEARCH_SOURCES)
    # And it must still be a panel source, or _sync_watch_locked drops it.
    assert ew._BB_LIVE_SOURCES <= ew._PANEL_SOURCES


def test_ownership_is_transitive_across_a_reseed_sequence():
    """A name seeded bro -> momentum -> bro -> momentum must stay bro, not
    flap with whichever panel refreshed last."""
    src = BRO
    for nxt in (DESK, "trending", DESK, BRO, DESK):
        src = ew._merge_source(src, nxt)
    assert src == BRO
