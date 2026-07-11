"""Grid split, bullish scoring, and leader election for the multi-chart
TV monitor — the screen-free half of tv-monitor/tv_core.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tv-monitor"))

from tv_core import LeaderTracker, bullish_score, grid_cells  # noqa: E402


# ------------------------------------------------------------ grid_cells ----

def test_grid_cells_1x1_is_whole_window():
    assert grid_cells(1000, 800, 1, 1) == [
        {"left": 0, "top": 0, "width": 1000, "height": 800}]


def test_grid_cells_2x2_tiles_and_reading_order():
    cells = grid_cells(1000, 800, 2, 2)
    assert len(cells) == 4
    # reading order: TL, TR, BL, BR
    assert (cells[0]["left"], cells[0]["top"]) == (0, 0)
    assert (cells[1]["left"], cells[1]["top"]) == (500, 0)
    assert (cells[2]["left"], cells[2]["top"]) == (0, 400)
    assert (cells[3]["left"], cells[3]["top"]) == (500, 400)
    # exact tiling — no gaps, no overlap, odd sizes included
    odd = grid_cells(1001, 799, 2, 2)
    assert odd[0]["width"] + odd[1]["width"] == 1001
    assert odd[0]["height"] + odd[2]["height"] == 799


def test_grid_cells_1x2_side_by_side():
    cells = grid_cells(1000, 800, 1, 2)
    assert len(cells) == 2
    assert all(c["height"] == 800 for c in cells)


# --------------------------------------------------------- bullish_score ----

def test_score_verdict_ladder():
    assert bullish_score("STRONG BUY") == 5.0
    assert bullish_score("BUY") == 4.0
    assert bullish_score("LEAN BUY (no regime backing)") == 3.0
    assert bullish_score("WATCH — exhaustion low, wait for the turn") == 2.0
    assert bullish_score("WAIT") == 0.0
    assert bullish_score("LEAN SELL (no regime backing)") == -3.0
    assert bullish_score("SELL") == -4.0
    assert bullish_score("STRONG SELL") == -5.0


def test_score_trend_tilts():
    assert bullish_score("BUY", trend={"score": 3}) == 7.0
    assert bullish_score("BUY", trend={"score": -2}) == 2.0
    assert bullish_score("BUY", trend={"score": None}) == 4.0


def test_score_squeeze_fire_and_building():
    fresh = {"fired": "LONG", "fired_ago": 30, "building": False, "mom": 2.0}
    assert bullish_score("BUY", sq=fresh) == 6.0
    stale = dict(fresh, fired_ago=300)
    assert bullish_score("BUY", sq=stale) == 4.0
    short = {"fired": "SHORT", "fired_ago": 10, "building": True, "mom": -3.0}
    assert bullish_score("WAIT", sq=short) == -3.0     # -2 fire, -1 building
    building_up = {"fired": None, "fired_ago": None, "building": True,
                   "mom": 1.5}
    assert bullish_score("BUY", sq=building_up) == 5.0


# --------------------------------------------------------- LeaderTracker ----

def test_vacant_seat_elected_immediately():
    lt = LeaderTracker(margin=1.5, confirm=5)
    assert lt.update({"AAPL": 4.0, "NVDA": 6.0}) == "NVDA"


def test_all_bearish_means_no_leader():
    lt = LeaderTracker()
    assert lt.update({"AAPL": -2.0, "NVDA": 0.0}) is None


def test_challenger_needs_margin():
    lt = LeaderTracker(margin=1.5, confirm=1)
    assert lt.update({"A": 5.0, "B": 1.0}) == "A"
    # beats A but not by the margin -> A keeps the seat
    assert lt.update({"A": 5.0, "B": 6.0}) == "A"
    assert lt.update({"A": 5.0, "B": 6.5}) == "B"


def test_challenger_needs_consecutive_reads():
    lt = LeaderTracker(margin=1.0, confirm=3)
    assert lt.update({"A": 5.0, "B": 1.0}) == "A"
    assert lt.update({"A": 5.0, "B": 8.0}) == "A"   # streak 1
    assert lt.update({"A": 5.0, "B": 8.0}) == "A"   # streak 2
    assert lt.update({"A": 5.0, "B": 8.0}) == "B"   # streak 3 -> takeover


def test_interrupted_streak_resets():
    lt = LeaderTracker(margin=1.0, confirm=2)
    assert lt.update({"A": 5.0, "B": 1.0}) == "A"
    assert lt.update({"A": 5.0, "B": 8.0}) == "A"   # streak 1
    assert lt.update({"A": 5.0, "B": 5.5}) == "A"   # dip -> reset
    assert lt.update({"A": 5.0, "B": 8.0}) == "A"   # streak 1 again
    assert lt.update({"A": 5.0, "B": 8.0}) == "B"


def test_leader_leaving_grid_reelects_now():
    lt = LeaderTracker(margin=1.5, confirm=5)
    assert lt.update({"A": 5.0, "B": 4.0}) == "A"
    assert lt.update({"B": 4.0, "C": 2.0}) == "B"   # A gone -> instant


def test_leader_turning_bearish_reelects_now():
    lt = LeaderTracker(margin=1.5, confirm=5)
    assert lt.update({"A": 5.0, "B": 4.0}) == "A"
    assert lt.update({"A": -1.0, "B": 4.0}) == "B"


def test_single_symbol_grid():
    lt = LeaderTracker()
    assert lt.update({"TSLA": 3.0}) == "TSLA"
    assert lt.update({"TSLA": -1.0}) is None
