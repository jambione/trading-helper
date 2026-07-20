"""The rebuilt vwap confidence pillar.

The old pillar was not an independent witness. Measured over the logged
sessions (n=21,281 rows where both were live) trend and vwap ran at
corr +0.43, agreeing 67.0% of the time against 47.1% expected if
independent -- so a "3/3" was nearer two witnesses than three, and the
gauge overstated itself exactly where it tells you to size up.

Two mechanical causes, both covered here:
  * the VWAP came from the local tape reader, which resets on every symbol
    switch -> median span 106s, never over 11.8 min. Not a session VWAP;
    a second, slower trend computed off the same price path.
  * vwap_min_age was 180s while trend_window is 300s, so a "mature" VWAP
    was YOUNGER than the trend it had to be independent of.
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flow_core.core import (VWAP_MIN_AGE_DEFAULT, SessionVWAP,  # noqa: E402
                     _session_anchor, confidence)

ET = ZoneInfo("America/New_York")


def ts(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=ET).timestamp()


# --------------------------------------------------------- the anchor ------

def test_regular_session_anchors_at_the_open():
    t = ts(2026, 7, 15, 11, 4)
    assert _session_anchor(t, ET) == ts(2026, 7, 15, 9, 30)


def test_premarket_anchors_at_four_am_not_the_open():
    """The logs contain 04:18 sessions - premarket is when these names
    move, so it gets its own VWAP rather than none at all."""
    t = ts(2026, 7, 15, 4, 18)
    assert _session_anchor(t, ET) == ts(2026, 7, 15, 4, 0)


def test_after_hours_anchors_at_the_close():
    t = ts(2026, 7, 15, 18, 30)
    assert _session_anchor(t, ET) == ts(2026, 7, 15, 16, 0)


def test_overnight_belongs_to_the_prior_after_hours_session():
    """02:00 is before every anchor of its own day; it must not roll
    forward into a session that has not started."""
    t = ts(2026, 7, 15, 2, 0)
    assert _session_anchor(t, ET) == ts(2026, 7, 14, 16, 0)


def test_crossing_the_open_resets_the_vwap():
    """Premarket prints must not pollute the regular-session VWAP - that
    is the whole convention the chart draws."""
    v = SessionVWAP(tz=ET)
    v.ingest("SPCX", 100.0, 10_000, ts(2026, 7, 15, 9, 0))    # premarket
    assert v.vwap("SPCX") == 100.0
    v.ingest("SPCX", 50.0, 10, ts(2026, 7, 15, 9, 31))        # RTH opens
    assert v.vwap("SPCX") == 50.0        # the 100.0 is gone, not averaged


# ------------------------------------------------- survives the hop --------

def test_vwap_survives_a_symbol_hop_and_back():
    """The bug in one test: the old VWAP reset every switch, so hopping
    away and back threw away the session. This one is keyed by symbol."""
    v = SessionVWAP(tz=ET)
    t = ts(2026, 7, 15, 10, 0)
    v.ingest("SPCX", 100.0, 1000, t)
    v.ingest("RUBI", 5.0, 1000, t + 1)       # hop away
    v.ingest("SPCX", 102.0, 1000, t + 2)     # hop back
    assert v.vwap("SPCX") == 101.0           # both SPCX prints, still there
    assert v.vwap("RUBI") == 5.0             # kept apart


def test_vwap_is_volume_weighted_not_a_price_mean():
    v = SessionVWAP(tz=ET)
    t = ts(2026, 7, 15, 10, 0)
    v.ingest("X", 10.0, 9000, t)
    v.ingest("X", 20.0, 1000, t + 1)
    assert v.vwap("X") == 11.0            # not 15.0


def test_age_reports_coverage_seen_not_time_since_the_anchor():
    """Start the monitor at 11:00 and the anchor is 09:30, but nothing
    from 09:30-11:00 was observed. Claiming 90 minutes of coverage would
    walk the pillar straight through its own maturity gate."""
    v = SessionVWAP(tz=ET)
    first = ts(2026, 7, 15, 11, 0)
    v.ingest("X", 10.0, 100, first)
    assert v.age("X", now=first + 60) == 60.0        # not 5400


def test_unseen_symbol_has_no_vwap_and_no_age():
    v = SessionVWAP(tz=ET)
    assert v.vwap("NOPE") is None and v.age("NOPE") is None


def test_junk_prints_are_ignored():
    v = SessionVWAP(tz=ET)
    t = ts(2026, 7, 15, 10, 0)
    for px, vol in ((0, 100), (-1, 100), (10.0, 0), (10.0, -5)):
        v.ingest("X", px, vol, t)
    assert v.vwap("X") is None


def test_forget_drops_a_rotated_off_symbol():
    v = SessionVWAP(tz=ET)
    v.ingest("X", 10.0, 100, ts(2026, 7, 15, 10, 0))
    v.forget(["x"])                       # case-insensitive, as subs are
    assert v.vwap("X") is None


# ------------------------------------------------- the maturity gate -------

def test_default_min_age_is_well_above_the_trend_window():
    """The old 180s default was BELOW trend_window=300: a 'mature' VWAP
    covered less ground than the trend it was meant to corroborate, so it
    could only ever echo it. Independence needs a much longer span."""
    assert VWAP_MIN_AGE_DEFAULT >= 3 * 300


def conf(vwap_age):
    """trend and vwap both point up; only the age varies."""
    tape = {"dom": 0.0, "sided_n": 9, "buy": 900, "sell": 900, "total": 1800}
    return confidence(1.0, tape, mid=101.0, vwap=100.0,
                      vwap_age=vwap_age, vwap_min_age=VWAP_MIN_AGE_DEFAULT)


def test_a_young_vwap_does_not_vote():
    """A tape VWAP that resets every hop can never reach the gate - which
    is the point: it abstains instead of echoing trend into a fake 3/3."""
    assert conf(300.0)["votes"]["vwap"] is None
    assert conf(300.0)["total"] == 2          # honestly counted as 2 pillars


def test_a_session_length_vwap_does_vote():
    c = conf(1800.0)
    assert c["votes"]["vwap"] == 1
    assert c["total"] == 3


def test_unknown_age_abstains_when_a_gate_is_set():
    """The closed trapdoor: vwap_age=None used to read as MATURE, so any
    caller supplying a vwap without an age bypassed the maturity gate
    entirely (0 violations in the logs, but a live hole). Unknown age now
    abstains -- a gate a caller can slip through by not reporting age is
    not a gate."""
    c = conf(None)
    assert c["votes"]["vwap"] is None
    assert c["total"] == 2                    # honestly counted as 2 pillars


def test_no_gate_keeps_the_old_always_mature_behavior():
    """vwap_min_age=0 (the confidence() default) must stay permissive so
    callers that never opted into the gate are unchanged."""
    tape = {"dom": 0.0, "sided_n": 9, "buy": 900, "sell": 900, "total": 1800}
    c = confidence(1.0, tape, mid=101.0, vwap=100.0,
                   vwap_age=None, vwap_min_age=0.0)
    assert c["votes"]["vwap"] == 1
