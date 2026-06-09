"""
test_discord_ingest.py — offline functional test of the Discord OCR ingest path
in the dashboard (the seam that replaced audio transcription).

Drives the REAL dashboard functions the /api/discord/ingest and /api/state
handlers call — ingest_discord_alerts → mention tracking + feed, discord_status,
_build_mention_rank, _snapshot — with add_ticker_to_log stubbed so nothing is
written to the live watchlist and no network/Alpaca/Finnhub is touched.

Run:
    venv/bin/python -m pytest tests/test_discord_ingest.py -q
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard as d   # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    # Never touch the real watchlist file from a test.
    monkeypatch.setattr(d, "add_ticker_to_log", lambda t: (True, True))
    with d.STATE.lock:
        d.STATE.mention_ts.clear()
        d.STATE.mention_daily.clear()
        d.STATE.discord_alerts.clear()
        d.STATE.discord_last_ts = 0.0
    yield


def test_ingest_records_feed_and_mentions():
    n = d.ingest_discord_alerts([
        {"ticker": "NVDA", "line": "NVDA Price Volatility Spike! >>>>> High = 41.83"},
        {"ticker": "spy",  "line": "SPY NEW WEEKLY LOW >>>>> Price: $739.20"},
    ])
    assert n == 2

    st = d.discord_status()
    assert st["running"] is True                 # fresh heartbeat → alive
    assert [a["ticker"] for a in st["alerts"]] == ["NVDA", "SPY"]   # upper-cased, in order

    with d.STATE.lock:
        assert "NVDA" in d.STATE.mention_ts and "SPY" in d.STATE.mention_ts
        assert d.STATE.mention_daily["NVDA"] == 1


def test_invalid_tickers_skipped_but_heartbeat_still_sent():
    n = d.ingest_discord_alerts([
        {"ticker": "TOOLONG", "line": "x"},   # > 5 chars
        {"ticker": "",        "line": "y"},   # empty
        {"ticker": "123",     "line": "z"},   # non-alpha
    ])
    assert n == 0
    # Even with nothing accepted, the call is a heartbeat → source counts as alive.
    assert d.discord_status()["running"] is True


def test_empty_list_is_a_valid_heartbeat():
    assert d.ingest_discord_alerts([]) == 0
    assert d.discord_status()["running"] is True


def test_squeeze_burst_alert_trips_the_threshold_immediately():
    # A burst alert injects mention_alert_threshold mentions at once, so the
    # ticker should immediately be in burst (>= threshold within the window).
    threshold = int(d.STATE.cfg.get("mention_alert_threshold", 5))
    d.ingest_discord_alerts([
        {"ticker": "ATHE", "line": "ATHE ww close over 6.78/7/7.50", "burst": True},
    ])
    with d.STATE.lock:
        assert len(d.STATE.mention_ts["ATHE"]) >= threshold
    # A plain alert injects exactly one mention (no burst).
    d.ingest_discord_alerts([{"ticker": "NVDA", "line": "NVDA >>>>> x"}])
    with d.STATE.lock:
        assert len(d.STATE.mention_ts["NVDA"]) == 1


def test_status_goes_offline_when_stale():
    d.ingest_discord_alerts([{"ticker": "AAPL", "line": "AAPL >>>>> x"}])
    # Simulate no heartbeat for longer than the stale window.
    with d.STATE.lock:
        d.STATE.discord_last_ts = time.time() - (d._DISCORD_STALE_SEC + 5)
    assert d.discord_status()["running"] is False


def test_build_mention_rank_floats_recent_alert():
    d.ingest_discord_alerts([{"ticker": "TSLA", "line": "TSLA >>>>> x"}])
    rank = d._build_mention_rank({"TSLA", "AAPL"})
    assert rank.get("TSLA") == 0          # most-recent mention ranks first
    assert "AAPL" not in rank             # never mentioned → not ranked


def test_snapshot_exposes_discord_block_not_transcriber(monkeypatch):
    # _snapshot reads the watchlist + signal state from disk; stub those to keep
    # it hermetic and focused on the discord block shape.
    monkeypatch.setattr(d, "load_tickers", lambda: [])
    monkeypatch.setattr(d, "load_news", lambda: [])
    monkeypatch.setattr(d, "_load_signal_state", lambda: {})
    d.ingest_discord_alerts([{"ticker": "AMD", "line": "AMD >>>>> x"}])

    snap = d._snapshot()
    assert "discord" in snap
    assert "transcriber" not in snap
    assert snap["discord"]["running"] is True
    assert any(a["ticker"] == "AMD" for a in snap["discord"]["alerts"])
