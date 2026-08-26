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


# ── a call-out does NOT outrank desk heat ────────────────────────────────

def test_a_bro_call_does_not_take_ownership_from_desk_heat():
    """Deliberately weaker. The seed loop calls a bro call "the weakest
    evidence on this list" and only lets it contribute symbols nothing else
    named, so letting it win a merge would contradict the seeder. Ownership
    and visibility are separate: the seeder tags `bro_call` onto a row it
    does not own — see test_bro_call_tags_a_row_it_does_not_own.
    """
    assert ew._merge_source(DESK, BRO) == BRO      # newest wins, unchanged
    assert ew._merge_source(BRO, DESK) == DESK     # and desk can take it back


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


def test_research_ownership_survives_a_whole_reseed_sequence():
    """The case that actually mattered: once a thesis owns a row, no
    sequence of desk sweeps or bro calls may quietly relabel it."""
    src = RESEARCH
    for nxt in (DESK, BRO, "trending", BRO, DESK):
        src = ew._merge_source(src, nxt)
    assert src == RESEARCH


# ── visibility without ownership ─────────────────────────────────────────

def test_bro_call_tags_a_row_it_does_not_own(monkeypatch):
    """The operator's actual complaint: Trader Bro never appears.

    DAIC was called by Bro on 8/26, momentum named it in the same pass, and
    the row reached the book as `momentum` with criteria
    ['big_move', 'uptrend'] — nothing on it recorded that a human had
    called it out. The call must not seize the row (it is the weakest
    evidence here), but it must leave a mark.
    """
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "_momentum_flagged_from_dashboard",
                        lambda max_price: [(9.0, {
                            "symbol": "DAIC", "source": "momentum",
                            "price": 3.88, "criteria": ["big_move"],
                        })])
    monkeypatch.setattr(ew, "_trending_rows_from_dashboard",
                        lambda *a, **k: [], raising=False)
    monkeypatch.setattr(ew, "_research_rows", lambda *a, **k: [],
                        raising=False)
    monkeypatch.setattr(ew, "_bb_live_from_dashboard",
                        lambda max_price, fresh, now=None: [
                            (800.0, {"symbol": "DAIC", "source": "bb_live",
                                     "price": 3.88,
                                     "criteria": ["bro_call"]}),
                            (700.0, {"symbol": "SOLO", "source": "bb_live",
                                     "price": 4.10,
                                     "criteria": ["bro_call"]}),
                        ])

    rows = ew.desk_candidate_rows({
        "ai_watch_seed_momentum": True,
        "ai_watch_seed_trending": False,
        "ai_watch_seed_research": False,
        "ai_watch_seed_bb_live": True,
        "ai_watch_seed_bb_live_n": 6,
        "ai_watch_bb_live_fresh_sec": 900.0,
        "ai_max_price": 100.0,
    })
    by = {str(r.get("symbol")): r for r in rows}

    assert "DAIC" in by, "momentum's row must survive"
    assert by["DAIC"]["source"] == "momentum", "the call must not seize it"
    assert "bro_call" in (by["DAIC"].get("criteria") or []), (
        "but the call has to be visible on the row")
    assert "big_move" in by["DAIC"]["criteria"], "and not clobber what was there"

    # A name nothing else claimed still comes in owned by the call.
    assert "SOLO" in by
    assert by["SOLO"]["source"] == "bb_live"


def test_tagging_is_idempotent_across_repeated_seeds(monkeypatch):
    """The seeder runs every poll; criteria must not grow without bound."""
    import ai_entry_watch as ew

    row = {"symbol": "DAIC", "source": "momentum", "price": 3.88,
           "criteria": ["big_move"]}
    monkeypatch.setattr(ew, "_momentum_flagged_from_dashboard",
                        lambda max_price: [(9.0, dict(row))])
    monkeypatch.setattr(ew, "_trending_rows_from_dashboard",
                        lambda *a, **k: [], raising=False)
    monkeypatch.setattr(ew, "_research_rows", lambda *a, **k: [],
                        raising=False)
    monkeypatch.setattr(ew, "_bb_live_from_dashboard",
                        lambda max_price, fresh, now=None: [
                            (800.0, {"symbol": "DAIC", "source": "bb_live",
                                     "price": 3.88,
                                     "criteria": ["bro_call"]}),
                        ])
    cfg = {
        "ai_watch_seed_momentum": True, "ai_watch_seed_trending": False,
        "ai_watch_seed_research": False, "ai_watch_seed_bb_live": True,
        "ai_watch_seed_bb_live_n": 6, "ai_watch_bb_live_fresh_sec": 900.0,
        "ai_max_price": 100.0,
    }
    for _ in range(3):
        rows = ew.desk_candidate_rows(cfg)
        got = [r for r in rows if r.get("symbol") == "DAIC"][0]
        assert got["criteria"].count("bro_call") == 1


# ── a call is history, not live state ────────────────────────────────────

def test_bro_call_survives_a_reseed_after_the_call_ages_out():
    """A Trader Bro call is live for 900s; the fact of it is permanent.

    DAIC on 8/26: tagged bro_call at 07:04, then momentum re-seeded it with
    ['mom_open', 'uptrend'] and the record stopped saying a human had ever
    named it. `admit_criteria` records what was true AT ADMISSION, so
    letting a later sweep erase it is the same disappearing act the source
    relabel was doing.
    """
    import ai_entry_watch as ew

    prev = {"admit_criteria": ["mom_open", "bro_call", "uptrend"]}
    row = {"criteria": ["mom_open", "uptrend"]}          # call has expired
    got = ew._merge_admit_criteria(row, prev)
    assert "bro_call" in got
    assert "mom_open" in got and "uptrend" in got


def test_non_sticky_criteria_still_reflect_the_current_pass():
    """Only history is sticky. 'big_move' means it is moving NOW, so a name
    that stops moving must stop claiming it."""
    import ai_entry_watch as ew

    prev = {"admit_criteria": ["big_move", "uptrend"]}
    got = ew._merge_admit_criteria({"criteria": ["uptrend"]}, prev)
    assert got == ["uptrend"]
    assert "big_move" not in got


def test_merge_admit_criteria_does_not_duplicate():
    import ai_entry_watch as ew

    prev = {"admit_criteria": ["bro_call"]}
    got = ew._merge_admit_criteria({"criteria": ["bro_call", "uptrend"]}, prev)
    assert got.count("bro_call") == 1


def test_empty_fresh_criteria_falls_back_to_the_previous_list():
    """Unchanged behaviour: a pass that computed nothing must not wipe the
    record it already had."""
    import ai_entry_watch as ew

    prev = {"admit_criteria": ["mom_open", "uptrend"]}
    assert ew._merge_admit_criteria({}, prev) == ["mom_open", "uptrend"]
    assert ew._merge_admit_criteria({"criteria": []}, prev) == [
        "mom_open", "uptrend"]


def test_merge_admit_criteria_handles_missing_inputs():
    import ai_entry_watch as ew

    assert ew._merge_admit_criteria({}, {}) == []
    assert ew._merge_admit_criteria(None, None) == []
