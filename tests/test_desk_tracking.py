"""
Desk / Find It First tracking + on_desk expiry hold.

Run:
    python3 -m pytest tests/test_desk_tracking.py -q
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

se = pytest.importorskip("signal_engine", reason="signal_engine requires pandas")
row_triggers_tracking = se.row_triggers_tracking
TickerState = se.TickerState


# ── row_triggers_tracking ─────────────────────────────────────────────────────

def test_mentioned_triggers():
    ok, reason = row_triggers_tracking({"ticker": "AAPL", "mentioned": True})
    assert ok and reason == "mentioned"


def test_burst_triggers():
    ok, reason = row_triggers_tracking(
        {"ticker": "AAPL", "mentioned": False, "mention_burst": True})
    assert ok and reason == "burst"


def test_find_it_first_triggers_without_desk_flag():
    ok, reason = row_triggers_tracking(
        {"ticker": "EZRA", "find_it_first": True}, track_desk=False)
    assert ok and reason == "find_it_first"


def test_desk_tracks_any_ticker_when_enabled():
    ok, reason = row_triggers_tracking(
        {"ticker": "ZNB", "mentioned": False, "mention_burst": False},
        track_desk=True)
    assert ok and reason == "desk"


def test_desk_off_requires_signal():
    ok, reason = row_triggers_tracking(
        {"ticker": "ZNB", "mentioned": False, "mention_burst": False},
        track_desk=False)
    assert not ok and reason == ""


def test_empty_row_never_triggers():
    assert row_triggers_tracking({}) == (False, "")
    assert row_triggers_tracking({"ticker": ""}) == (False, "")


def test_mentioned_wins_over_find_it_first():
    ok, reason = row_triggers_tracking(
        {"ticker": "X", "mentioned": True, "find_it_first": True},
        track_desk=True)
    assert reason == "mentioned"


# ── on_desk holds expiry ──────────────────────────────────────────────────────

def test_on_desk_prevents_cold_expiry():
    ts = TickerState("EZRA")
    ts.ever_positive_hist = False
    ts.added_ts = time.time() - (se.EXPIRY_COLD + 60)
    ts.on_desk = True
    assert not ts.is_expired()
    assert ts.time_left_s() == float("inf")


def test_leaves_desk_then_cold_expires():
    ts = TickerState("EZRA")
    ts.ever_positive_hist = False
    ts.added_ts = time.time() - (se.EXPIRY_COLD + 60)
    ts.on_desk = False
    assert ts.is_expired()


def test_on_desk_does_not_override_in_position():
    # Sanity: in_position already never expires; on_desk is orthogonal
    ts = TickerState("EZRA")
    ts.in_position = True
    ts.on_desk = False
    ts.added_ts = time.time() - 99999
    assert not ts.is_expired()
