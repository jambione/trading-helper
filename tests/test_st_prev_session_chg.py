"""%Chg pre-market: show the last session's move, never as today's.

The complaint this answers: stocktwits.com shows IMMX −3.18% while the panel
showed +0.00%. Both were right. Verified against real bars for 2026-07-27
(a Monday, 05:31 ET, nothing traded yet):

    IMMX  Thu 8.79 → Fri 8.515   site −3.18%,  panel +0.00%
    CRML  Thu 6.12 → Fri 5.81    site −4.91%,  panel +0.00%

The site was showing FRIDAY'S session. The panel was showing "unmoved since
Friday's close" — true, because the price IS Friday's close, so pct_change
measured that close against itself.

Fixing it by pointing pct_change at the last session would re-introduce the
defect snapshot_fields' docstring already documents: apply_look_highlights
gates the LOOK badge on |pct_change|, so an inherited move manufactures LOOK
badges that ring the speaker and land in the journal as evidence — during
exactly these pre-market hours. So the session move gets its own field, and
the renderer dims and dates it. These tests pin both halves.
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "momentum-monitor"))

from momentum_signal import _session_tag, _st_chg_cell  # noqa: E402
from stocktwits_trending import apply_look_highlights, snapshot_fields  # noqa: E402

TODAY = date(2026, 7, 27)          # Monday
FRI = date(2026, 7, 24)
THU = date(2026, 7, 23)
NOW = datetime(2026, 7, 27, 5, 31, tzinfo=timezone.utc).timestamp()


class _Bar:
    def __init__(self, d, close):
        self.timestamp = datetime(d.year, d.month, d.day, 20, 0,
                                  tzinfo=timezone.utc)   # 16:00 ET
        self.close = close
        self.volume = 1_000_000


class _Trade:
    def __init__(self, d, price, hour=20):
        self.timestamp = datetime(d.year, d.month, d.day, hour, 0,
                                  tzinfo=timezone.utc)
        self.price = price


class _Snap:
    def __init__(self, daily=None, prev=None, latest=None):
        self.daily_bar, self.previous_daily_bar, self.latest_trade = (
            daily, prev, latest)


def _immx_premarket():
    """The real IMMX case: Friday's close is the newest of everything."""
    return _Snap(daily=_Bar(FRI, 8.515), prev=_Bar(THU, 8.79),
                 latest=_Trade(FRI, 8.515))


# ── the session move is computed and matches the website ────────────────────

def test_the_prior_session_move_matches_what_the_site_shows():
    out = snapshot_fields(_immx_premarket(), NOW, TODAY)
    assert round(out["pct_change_prev"], 2) == -3.13      # site: −3.18 (SIP)
    assert out["prev_session_date"] == FRI.isoformat()


def test_pct_change_is_still_the_honest_zero():
    """Unchanged on purpose: the price has not moved since that close."""
    out = snapshot_fields(_immx_premarket(), NOW, TODAY)
    assert round(out["pct_change"], 4) == 0.0
    assert out["pct_is_today"] is False


def test_a_printed_today_session_reports_todays_move_and_no_session_fallback():
    snap = _Snap(daily=_Bar(TODAY, 9.00), prev=_Bar(FRI, 8.515),
                 latest=_Trade(TODAY, 9.00))
    out = snapshot_fields(snap, NOW, TODAY)
    assert round(out["pct_change"], 2) == 5.70
    assert out["pct_is_today"] is True
    assert "pct_change_prev" not in out


def test_a_premarket_print_today_is_todays_move_not_a_fallback():
    """The case that would break a naive "is the daily bar today?" test: the
    daily bar is still Friday's at 05:31, but the trade is today's and the
    move against Friday's close is real."""
    snap = _Snap(daily=_Bar(FRI, 8.515), prev=_Bar(THU, 8.79),
                 latest=_Trade(TODAY, 9.20, hour=9))
    out = snapshot_fields(snap, NOW, TODAY)
    assert out["pct_is_today"] is True
    assert round(out["pct_change"], 2) == 8.04
    assert _st_chg_cell(out).startswith("[green]+8.04%")


def test_no_previous_bar_publishes_no_session_move():
    out = snapshot_fields(_Snap(daily=_Bar(FRI, 8.515),
                                latest=_Trade(FRI, 8.515)), NOW, TODAY)
    assert "pct_change_prev" not in out


def test_a_zero_previous_close_is_not_divided_by():
    out = snapshot_fields(_Snap(daily=_Bar(FRI, 8.5), prev=_Bar(THU, 0.0),
                                latest=_Trade(FRI, 8.5)), NOW, TODAY)
    assert "pct_change_prev" not in out


def test_no_trade_at_all_still_falls_back():
    """px comes off the bar close; there is no print date, so not today."""
    out = snapshot_fields(_Snap(daily=_Bar(FRI, 8.515), prev=_Bar(THU, 8.79)),
                          NOW, TODAY)
    assert out["pct_is_today"] is False
    assert round(out["pct_change_prev"], 2) == -3.13


# ── the LOOK gate must not see the inherited move ───────────────────────────

def _look_row(**over):
    # apply_look_highlights recomputes range_pct from price/52w, so the price
    # has to actually sit near the low for WASH to be reachable at all.
    row = {"symbol": "IMMX", "trending_score": 0.9, "price": 2.50,
           "vol_session": 2_000_000, "high_52w": 12.14, "low_52w": 1.94}
    row.update(over)
    return row


def test_an_inherited_move_cannot_manufacture_a_look_badge():
    """The whole reason the session move lives in its own field. −3.13% clears
    the 3.0 threshold and the row is near its lows — if the gate saw it, this
    would light WASH, beep, and be journalled as evidence."""
    rows = apply_look_highlights(
        [_look_row(pct_change=0.0, pct_change_prev=-3.13,
                   prev_session_date=FRI.isoformat())],
        min_abs_chg=3.0)
    assert not rows[0].get("look")


def test_a_real_today_move_still_lights_the_badge():
    """Guard against fixing the above by breaking LOOK entirely."""
    rows = apply_look_highlights(
        [_look_row(pct_change=-6.0, pct_is_today=True)], min_abs_chg=3.0)
    assert rows[0].get("look")
    assert rows[0].get("look_reason") == "WASH"


# ── the cell ────────────────────────────────────────────────────────────────

def test_the_session_move_renders_dimmed_and_dated():
    cell = _st_chg_cell(snapshot_fields(_immx_premarket(), NOW, TODAY))
    assert "dim" in cell
    assert "-3.1%" in cell
    assert "Fri" in cell
    assert "green" not in cell and "red" not in cell


def test_todays_move_renders_bright_and_undated():
    cell = _st_chg_cell({"pct_change": 5.7, "pct_is_today": True})
    assert cell == "[green]+5.70%[/green]"
    cell = _st_chg_cell({"pct_change": -5.7, "pct_is_today": True})
    assert cell == "[red]-5.70%[/red]"


def test_a_genuine_flat_today_is_not_dimmed_away():
    """0.00% with a today-dated print is a real reading and stays bright."""
    assert _st_chg_cell({"pct_change": 0.0, "pct_is_today": True}) == \
        "[green]+0.00%[/green]"


def test_nothing_to_show_is_a_dash_not_a_zero():
    assert _st_chg_cell({}) == "[dim]—[/dim]"
    assert _st_chg_cell({"pct_change": 0.0, "pct_is_today": False}) == \
        "[dim]—[/dim]"


def test_an_untagged_session_move_is_withheld():
    """The weekday marker is what stops it reading as today's, so a session
    move whose date will not parse is not shown at all."""
    assert _st_chg_cell({"pct_change_prev": -3.13}) == "[dim]—[/dim]"
    assert _st_chg_cell({"pct_change_prev": -3.13,
                         "prev_session_date": "junk"}) == "[dim]—[/dim]"


def test_session_tag_names_the_weekday():
    assert _session_tag("2026-07-24") == "Fri"
    assert _session_tag("2026-07-27") == "Mon"
    assert _session_tag(None) == ""
    assert _session_tag("nonsense") == ""


def test_the_two_numbers_are_never_the_same_style():
    """The safety property in one assertion: whatever the row, a fallback cell
    is dim and tagged and a today cell is coloured and bare."""
    today = _st_chg_cell({"pct_change": -3.13, "pct_is_today": True})
    fallback = _st_chg_cell({"pct_change": 0.0, "pct_is_today": False,
                             "pct_change_prev": -3.13,
                             "prev_session_date": FRI.isoformat()})
    assert today != fallback
    assert "dim" in fallback and "dim" not in today
    assert "Fri" in fallback and "Fri" not in today


# ── the panel wires it through ──────────────────────────────────────────────

def test_the_panel_renders_the_fallback_for_a_pre_market_row():
    from conftest import column_cells
    from momentum_signal import DEFAULTS, stocktwits_panel

    class _ST:
        error = None
        last_ok = NOW
        max_price = 30.0
        by_symbol: dict = {}

        def display_rows(self, *a, **k):
            row = {"symbol": "IMMX", "rank": 3, "price": 8.515}
            row.update(snapshot_fields(_immx_premarket(), NOW, TODAY))
            return [row]

        def quote_age(self, *a, **k):
            return None

    cell = column_cells(
        stocktwits_panel(_ST(), {}, cfg=DEFAULTS, now=NOW).renderable, "%Chg")[0]
    assert "-3.1%" in cell and "Fri" in cell and "dim" in cell


def test_the_yearless_tag_survives_a_year_boundary():
    """A Friday session read on the following Monday in January."""
    assert _session_tag("2025-12-31") == "Wed"
    assert (datetime(2026, 1, 2) - timedelta(days=2)).strftime("%a") == "Wed"
