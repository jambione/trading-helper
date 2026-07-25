"""Trending panel: which day each snapshot figure describes, and RVOL feed match.

The defect family these cover: the panel took `daily_bar` to mean "today" and
`previous_daily_bar` to mean "the session before today". Both are true during
regular hours and false every morning before the day's first print — when
`daily_bar` IS the last completed session. Then yesterday's total was shown as
today's volume, and %chg was measured against the close *before* the last
session, reporting that session's whole move as if it were today's.

That last one is not cosmetic: `stocktwits_look_min_abs_chg` gates the LOOK
badge on |%chg|, so an inherited move manufactures LOOK badges that ring the
speaker and land in the journal as evidence — during exactly the pre-market
hours two of the three daily tranches sit in.
"""
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from stocktwits_trending import (  # noqa: E402
    MIN_AVG_SESSIONS,
    average_volume,
    apply_look_highlights,
    range_bar,
    row_rvol,
    snapshot_fields,
    _bar_et_date,
)

TODAY = date(2026, 7, 24)          # a Friday
YESTERDAY = date(2026, 7, 23)
DAY_BEFORE = date(2026, 7, 22)

# 2026-07-24 10:15 ET, mid-session
NOW = datetime(2026, 7, 24, 14, 15, tzinfo=timezone.utc).timestamp()


class Bar:
    def __init__(self, d: date, close=None, volume=None, hour=9):
        self.timestamp = datetime(d.year, d.month, d.day, hour,
                                  tzinfo=timezone.utc)
        self.close = close
        self.volume = volume


class Trade:
    def __init__(self, price, ts=None):
        self.price = price
        self.timestamp = ts


class Snap:
    def __init__(self, daily=None, prev=None, latest=None):
        self.daily_bar = daily
        self.previous_daily_bar = prev
        self.latest_trade = latest


# ── %chg baseline: the last close strictly before today ─────────────────────

def test_intraday_measures_against_the_previous_session():
    """daily_bar IS today, so the baseline is previous_daily_bar."""
    snap = Snap(daily=Bar(TODAY, close=11.0, volume=500_000),
                prev=Bar(YESTERDAY, close=10.0),
                latest=Trade(11.0))
    f = snapshot_fields(snap, NOW, TODAY)
    assert f["prev_close"] == 10.0
    assert round(f["pct_change"], 4) == 10.0
    assert f["pct_basis_date"] == YESTERDAY.isoformat()


def test_premarket_measures_against_the_last_completed_session():
    """The bug. Before today's first print daily_bar is YESTERDAY, so the
    baseline is that bar's own close — not previous_daily_bar, which is a
    session further back and would report yesterday's move as today's."""
    snap = Snap(daily=Bar(YESTERDAY, close=10.0, volume=9_000_000),
                prev=Bar(DAY_BEFORE, close=8.0),
                latest=Trade(10.10))
    f = snapshot_fields(snap, NOW, TODAY)
    assert f["prev_close"] == 10.0
    assert round(f["pct_change"], 2) == 1.0
    assert f["pct_basis_date"] == YESTERDAY.isoformat()


def test_the_old_baseline_would_have_inflated_the_move_past_the_look_gate():
    """Concretely why it matters: 1.0% is under the 3% LOOK threshold, the
    figure the old code produced is well over it."""
    snap = Snap(daily=Bar(YESTERDAY, close=10.0),
                prev=Bar(DAY_BEFORE, close=8.0),
                latest=Trade(10.10))
    fixed = snapshot_fields(snap, NOW, TODAY)["pct_change"]
    old = (10.10 - 8.0) / 8.0 * 100.0
    assert abs(fixed) < 3.0 <= abs(old)


def test_no_baseline_is_published_when_the_bar_date_is_unknown():
    snap = Snap(daily=Bar(TODAY, close=11.0), prev=Bar(YESTERDAY, close=10.0),
                latest=Trade(11.0))
    snap.daily_bar.timestamp = None
    f = snapshot_fields(snap, NOW, TODAY)
    assert "pct_change" not in f
    assert "vol_session" not in f
    assert f["price"] == 11.0          # the price itself is still usable


# ── session volume: only from a bar that provably covers today ──────────────

def test_todays_bar_supplies_session_volume():
    snap = Snap(daily=Bar(TODAY, close=11.0, volume=500_000))
    f = snapshot_fields(snap, NOW, TODAY)
    assert f["vol_session"] == 500_000
    assert f["vol_bar_date"] == TODAY.isoformat()


def test_a_stale_bar_supplies_no_session_volume():
    """Yesterday's completed total must not appear as today's volume — that is
    the same wrong-day defect T2.1 fixed on the momentum side."""
    snap = Snap(daily=Bar(YESTERDAY, close=10.0, volume=9_000_000))
    f = snapshot_fields(snap, NOW, TODAY)
    assert "vol_session" not in f
    assert f["vol_bar_date"] == YESTERDAY.isoformat()


def test_zero_volume_is_not_published_as_a_number():
    snap = Snap(daily=Bar(TODAY, close=11.0, volume=0))
    assert "vol_session" not in snapshot_fields(snap, NOW, TODAY)


# ── price age ───────────────────────────────────────────────────────────────

def test_price_carries_the_age_of_its_own_print():
    ts = datetime(2026, 7, 24, 14, 14, tzinfo=timezone.utc)     # 60s before NOW
    snap = Snap(daily=Bar(TODAY, close=10.0), latest=Trade(11.0, ts))
    f = snapshot_fields(snap, NOW, TODAY)
    assert f["price"] == 11.0
    assert round(f["price_age_sec"]) == 60


def test_a_bar_close_standing_in_for_a_trade_claims_no_print_age():
    """With no latest_trade the bar close is the best price on offer, but it is
    not a print and must not be dated as one."""
    snap = Snap(daily=Bar(TODAY, close=10.0), latest=None)
    f = snapshot_fields(snap, NOW, TODAY)
    assert f["price"] == 10.0
    assert "price_age_sec" not in f


def test_bar_dates_are_read_in_et_not_the_host_zone():
    """22:00 UTC on the 24th is 18:00 ET the same day; 02:00 UTC on the 25th is
    22:00 ET on the 24th. A host in UTC must not disagree about the session."""
    assert _bar_et_date(Bar(TODAY, hour=22)) == TODAY
    late = Bar(date(2026, 7, 25), hour=2)
    assert _bar_et_date(late) == TODAY


# ── average volume for the RVOL denominator ─────────────────────────────────

def _seq(n, vol=1_000_000, upto=TODAY):
    return [(date(2026, 7, 23 - i), vol) for i in range(n)]


def test_average_needs_enough_completed_sessions():
    assert average_volume(_seq(MIN_AVG_SESSIONS - 1), TODAY) is None
    assert average_volume(_seq(MIN_AVG_SESSIONS), TODAY) == 1_000_000


def test_todays_partial_bar_is_excluded_from_its_own_denominator():
    """Including it drags the average down all morning and inflates every RVOL
    taken from it."""
    seq = _seq(MIN_AVG_SESSIONS) + [(TODAY, 1_000)]
    assert average_volume(seq, TODAY) == 1_000_000


def test_average_uses_only_the_last_avg_days_sessions():
    seq = [(date(2026, 6, 1), 50_000_000)] + _seq(MIN_AVG_SESSIONS)
    assert average_volume(seq, TODAY, avg_days=MIN_AVG_SESSIONS) == 1_000_000


def test_halted_sessions_do_not_count_as_history():
    assert average_volume([(date(2026, 7, 23 - i), 0)
                           for i in range(10)], TODAY) is None


# ── RVOL only when both sides come from the same feed ───────────────────────

def test_no_rvol_without_an_iex_average():
    """Stocktwits' consolidated avg_vol is the only other average on hand and
    dividing an IEX numerator by it understates RVOL by an order of magnitude,
    so the answer is no answer."""
    row = {"vol_session": 500_000, "avg_vol_consolidated": 40_000_000}
    assert row_rvol(row, None, NOW) == (None, None)


def test_no_rvol_without_session_volume():
    assert row_rvol({}, 1_000_000, NOW) == (None, None)


def test_rvol_is_computed_from_matched_feeds():
    rvol, raw = row_rvol({"vol_session": 2_000_000}, 1_000_000, NOW,
                         time_adjusted=False)
    assert raw == 2.0
    assert rvol == 2.0


# ── LOOK gate no longer mixes units ─────────────────────────────────────────

def test_a_row_with_no_session_volume_has_no_volume_evidence():
    """It used to substitute the consolidated 30-day average into the same
    median as IEX session totals, then compare each row against that mixture."""
    rows = [{"symbol": "AAA", "trending_score": 20.0, "pct_change": 8.0,
             "avg_vol_consolidated": 40_000_000, "price": 9.0,
             "high_52w": 10.0, "low_52w": 1.0}]
    apply_look_highlights(rows, min_abs_chg=3.0, max_looks=2)
    assert rows[0]["look"] is False


def test_min_rvol_blocks_a_row_that_only_beat_a_quiet_panel_median():
    rows = [{"symbol": "AAA", "trending_score": 20.0, "pct_change": 8.0,
             "vol_session": 2_000, "rvol": 0.4, "price": 9.0,
             "high_52w": 10.0, "low_52w": 1.0}]
    apply_look_highlights(rows, min_abs_chg=3.0, max_looks=2, min_rvol=1.5)
    assert rows[0]["look"] is False


def test_min_rvol_passes_a_genuinely_heavy_row():
    rows = [{"symbol": "AAA", "trending_score": 20.0, "pct_change": 8.0,
             "vol_session": 2_000_000, "rvol": 6.0, "price": 9.0,
             "high_52w": 10.0, "low_52w": 1.0}]
    apply_look_highlights(rows, min_abs_chg=3.0, max_looks=2, min_rvol=1.5)
    assert rows[0]["look"] is True


def test_unknown_rvol_neither_passes_nor_blocks():
    """The median test stands alone rather than a missing rvol being read as 0."""
    rows = [{"symbol": "AAA", "trending_score": 20.0, "pct_change": 8.0,
             "vol_session": 2_000_000, "price": 9.0,
             "high_52w": 10.0, "low_52w": 1.0}]
    apply_look_highlights(rows, min_abs_chg=3.0, max_looks=2, min_rvol=1.5)
    assert rows[0]["look"] is True


# ── 52w range track ─────────────────────────────────────────────────────────

def _marker(bar: str) -> str:
    return next((c for c in bar if c in "●▶◀"), "")


def test_the_marker_tracks_position_in_the_range():
    low = range_bar(1.0, 1.0, 11.0, width=11)
    mid = range_bar(6.0, 1.0, 11.0, width=11)
    high = range_bar(11.0, 1.0, 11.0, width=11)
    assert low.index("●") < mid.index("●") < high.index("●")


def test_a_new_52w_high_is_not_clamped_into_looking_like_a_near_high():
    """Momentum names print new highs constantly. `●` parked at the right end
    would read identically to merely being near the old high."""
    assert _marker(range_bar(12.0, 1.0, 11.0)) == "▶"
    assert _marker(range_bar(10.9, 1.0, 11.0)) == "●"


def test_a_new_52w_low_gets_its_own_glyph():
    assert _marker(range_bar(0.5, 1.0, 11.0)) == "◀"


def test_no_track_is_drawn_without_the_data_to_place_it():
    assert range_bar(None, 1.0, 11.0) == ""
    assert range_bar(5.0, None, 11.0) == ""
    assert range_bar(5.0, 1.0, None) == ""


def test_a_degenerate_range_draws_nothing():
    """hi == lo would put the marker somewhere plausible-looking with no
    information behind it."""
    assert range_bar(5.0, 5.0, 5.0) == ""
    assert range_bar(5.0, 11.0, 1.0) == ""


def test_track_width_is_honoured():
    for w in (3, 11, 20):
        bar = range_bar(6.0, 1.0, 11.0, width=w)
        assert sum(1 for c in bar if c in "─●▶◀") == w
