"""Competing entries: RVOL first, then the best EXH / MACD gap that is trending.

ai_max_buys_per_poll is 1, so when several names qualify in the same poll
exactly one gets the trade. RVOL leads. Beneath it the seat goes to the name
whose own signal has turned, rather than to whichever the loop reached first.

Direction is the tier and size is the tiebreak, in that order — a small gap
that is opening beats a wide one that is closing, because the wide one is a
move already over.

RVOL is a float, so names essentially never tie on it exactly. These tests
band it (ai_watch_rank_rvol_band) to exercise the signal legs; at the shipped
default of 0 the volume ordering is exact and the signal rarely gets a say.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402


def _rec(sym, *, macd_up=False, exh_up=False, sep=None, pctr=None, rvol=None):
    ind = {}
    if macd_up:
        ind["macd_gap_rising"] = True
    if exh_up:
        ind["pctr_rising"] = True
    if sep is not None:
        ind["macd_sep_ratio"] = sep
    if pctr is not None:
        ind["pctr"] = pctr           # exhaustion_pct reads 100 + %R
    r = {"symbol": sym, "indicator": ind}
    if rvol is not None:
        r["rvol"] = rvol
    return r


def _order(*recs, band=1.0, monkeypatch=None):
    """Rank with RVOL banded so the signal legs are the deciding term.

    Every fixture below leaves rvol unset unless it is testing RVOL itself, so
    the names land in one bucket and the comparison under test is the signal.
    """
    ew._rank_rvol_band.__wrapped__ = None
    orig = ew._rank_rvol_band
    ew._rank_rvol_band = lambda: band
    try:
        return [s for s, _ in ew.rvol_ranked({r["symbol"]: r for r in recs})]
    finally:
        ew._rank_rvol_band = orig


# ── direction decides the tier ───────────────────────────────────────────

def test_both_trending_up_wins():
    """The confluence is the best evidence available, so it takes the seat."""
    both = _rec("BOTH", macd_up=True, exh_up=True, sep=1.0, pctr=-20)
    macd = _rec("MACD", macd_up=True, sep=3.5, pctr=-5)
    assert _order(macd, both)[0] == "BOTH"


def test_a_small_opening_gap_beats_a_wide_closing_one():
    """The rule in one assertion. A wide gap that is closing is a move already
    over; size must not outrank direction."""
    opening = _rec("OPEN", macd_up=True, sep=0.4, pctr=-60)
    closing = _rec("SHUT", sep=3.9, pctr=-2)
    assert _order(closing, opening)[0] == "OPEN"


def test_macd_rising_outranks_exh_rising():
    """MACD is the entry lever; %R is supporting."""
    m = _rec("MACD", macd_up=True, sep=0.5)
    e = _rec("EXH", exh_up=True, pctr=-1)
    assert _order(e, m)[0] == "MACD"


def test_anything_trending_beats_nothing_trending():
    flat = _rec("FLAT", sep=3.9, pctr=-1)
    up = _rec("UP", exh_up=True, sep=0.1, pctr=-90)
    assert _order(flat, up)[0] == "UP"


# ── size breaks ties inside a tier ───────────────────────────────────────

def test_wider_separation_wins_within_a_tier():
    a = _rec("WIDE", macd_up=True, sep=2.5)
    b = _rec("NARROW", macd_up=True, sep=0.6)
    assert _order(b, a)[0] == "WIDE"


def test_better_on_both_legs_outranks_better_on_one():
    both = _rec("BOTH", macd_up=True, sep=1.5, pctr=-10)   # 1.5 + 0.90
    one = _rec("ONE", macd_up=True, sep=2.0, pctr=-95)     # 2.0 + 0.05
    assert _order(one, both)[0] == "BOTH"


def test_a_huge_ratio_cannot_swamp_the_other_leg():
    """Separation is capped, so one enormous gap cannot outrank a name that is
    better on both legs."""
    huge = _rec("HUGE", macd_up=True, sep=400.0, pctr=-99)
    assert ew._signal_rank(huge)[1] <= ew._SEP_CAP + 1.0


# ── absence is not strength ──────────────────────────────────────────────

def test_an_unreadable_record_sorts_behind_a_measurable_one():
    known = _rec("KNOWN", macd_up=True, sep=0.2)
    blank = {"symbol": "BLANK"}
    assert _order(blank, known)[0] == "KNOWN"


def test_rvol_leads_and_signal_cannot_overturn_it():
    """The operator's ordering: volume first. A far busier name keeps the seat
    even against a perfect setup."""
    quiet = _rec("QUIET", macd_up=True, exh_up=True, sep=3.0, pctr=-5, rvol=1.1)
    busy = {"symbol": "BUSY", "rvol": 50.0}
    assert _order(busy, quiet)[0] == "BUSY"


def test_signal_decides_inside_an_rvol_band():
    """And this is why banding exists — 3.4x and 3.2x are the same tape, so
    the turning signal takes it."""
    a = _rec("TURNING", macd_up=True, exh_up=True, sep=1.2, pctr=-25, rvol=3.2)
    b = _rec("FLAT", sep=3.9, pctr=-1, rvol=3.4)
    assert _order(a, b, band=0.5)[0] == "TURNING"


def test_an_unknown_rvol_sorts_last_whatever_its_signal():
    known = {"symbol": "KNOWN", "rvol": 1.0}
    blank = _rec("BLANK", macd_up=True, exh_up=True, sep=3.0, pctr=-2)
    assert _order(known, blank)[-1] == "BLANK"


def test_every_record_survives_the_sort():
    """Ordering only — it decides who is offered the seat, never who is
    evaluated. A dropped record would silently stop being gated at all."""
    state = {f"S{i}": _rec(f"S{i}", sep=i * 0.1) for i in range(12)}
    assert len(ew.rvol_ranked(state)) == 12


# ── the move leads: spend the seat on a name that is travelling ────────────
#
# The purpose of ranking at all. The shelf trails 0.25% behind price, so a
# trade whose whole move is 0.2% cannot finish above its own fill however well
# it is managed. RVOL does not measure that — a heavily traded name that goes
# nowhere has high RVOL too. The one profitable session on record (08-24)
# differed from every other day in exactly one respect: median peak +0.95%
# against +0.12% to +0.31% everywhere else.

def _mv(sym, move, rvol, **ind):
    return {"symbol": sym, "pct_change": move, "rvol": rvol, "indicator": ind}


def _order_mv(*recs, move_band=2.0, rvol_band=0.5):
    om, orv = ew._rank_move_band, ew._rank_rvol_band
    odp, odr = ew._desk_pct_change, ew._desk_rvol
    ew._rank_move_band = lambda: move_band
    ew._rank_rvol_band = lambda: rvol_band
    ew._desk_pct_change = lambda s: None      # offline: use the stamp
    ew._desk_rvol = lambda s: None
    try:
        return [s for s, _ in ew.rvol_ranked({r["symbol"]: r for r in recs})]
    finally:
        ew._rank_move_band, ew._rank_rvol_band = om, orv
        ew._desk_pct_change, ew._desk_rvol = odp, odr


def test_the_bigger_mover_takes_the_seat_over_the_busier_name():
    runner = _mv("RUNNER", 12.4, 1.2, macd_gap_rising=True, macd_sep_ratio=1.4)
    busy = _mv("BUSY", 1.1, 8.9)
    assert _order_mv(busy, runner)[0] == "RUNNER"


def test_a_huge_rvol_on_a_name_going_nowhere_sorts_behind():
    """The failure mode this exists to stop."""
    stuck = _mv("STUCK", 0.9, 9.9, macd_gap_rising=True, pctr_rising=True,
                macd_sep_ratio=3.0)
    moving = _mv("MOVING", 8.0, 1.0)
    assert _order_mv(stuck, moving)[0] == "MOVING"


def test_a_decliner_sorts_behind_a_riser():
    """Signed since the day-change floors came off. abs() was defensible only
    while a floor kept decliners out of the pool; without one it would sort
    IREN at -12.2% to the front of a one-seat queue."""
    assert ew._rank_move({"symbol": "A", "pct_change": -9.0}) == -9.0
    up = _mv("UP", 4.0, 1.0)
    down = _mv("DOWN", -12.0, 1.0)
    assert _order_mv(down, up)[0] == "UP"


def test_live_pct_change_beats_the_admit_stamp():
    """Same live-before-stale rule as RVOL: a name can stop moving between
    admission and the poll that would buy it."""
    odp = ew._desk_pct_change
    ew._desk_pct_change = lambda s: 3.0
    try:
        assert ew._rank_move({"symbol": "A", "admit_pct_change": 30.0}) == 3.0
    finally:
        ew._desk_pct_change = odp


def test_rvol_still_breaks_ties_inside_a_move_bucket():
    a = _mv("A", 10.2, 1.0)
    b = _mv("B", 10.4, 7.0)
    assert _order_mv(a, b, move_band=5.0)[0] == "B"


def test_a_name_with_no_move_reading_sorts_last():
    known = _mv("KNOWN", 4.0, 1.0)
    blank = {"symbol": "BLANK", "rvol": 9.9,
             "indicator": {"macd_gap_rising": True, "pctr_rising": True}}
    assert _order_mv(known, blank)[-1] == "BLANK"


def test_zero_band_restores_rvol_first_ordering():
    """The knob is a switch, not a tuning: 0 gives back exactly the previous
    behaviour, so this can be turned off without a deploy."""
    runner = _mv("RUNNER", 12.4, 1.2)
    busy = _mv("BUSY", 1.1, 8.9)
    assert _order_mv(busy, runner, move_band=0.0)[0] == "BUSY"
