"""
test_discord_sentiment.py — offline tests for the Discord OCR sentiment pipeline.

Section A/B cover the pure parsers in discord_source.py (parse_market_signal,
parse_chat_sentiment / _score_text). Section C/D drive the dashboard ingest +
snapshot path with add_ticker_to_log stubbed and STATE cleared between tests —
no network, no screen capture, no real file I/O.

Run:
    venv/bin/python -m pytest tests/test_discord_sentiment.py -q
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord_source as ds   # noqa: E402
import dashboard as d         # noqa: E402


# ── Section A — parse_market_signal (pure) ────────────────────────────────────

def test_market_bearish():
    tkr, score, _ = ds.parse_market_signal("🔴 HIGH VOLUME BEARISH — SPY")
    assert (tkr, score) == ("SPY", -1.0)


def test_market_bullish():
    tkr, score, _ = ds.parse_market_signal("🟢 HIGH VOLUME BULLISH — SPY")
    assert (tkr, score) == ("SPY", 1.0)


def test_market_bullish_words_only_no_emoji():
    tkr, score, _ = ds.parse_market_signal("HIGH VOLUME BULLISH - SPY")
    assert (tkr, score) == ("SPY", 1.0)


def test_market_followup_line_ignored():
    _, score, _ = ds.parse_market_signal("Closed DOWN on high RVOL @ 739.96")
    assert score == 0.0


def test_market_regular_alert_ignored():
    _, score, _ = ds.parse_market_signal("COSM New Daily High >>>>> Current Price = 1.23")
    assert score == 0.0


# ── Section B — parse_chat_sentiment (pure) ───────────────────────────────────

def test_chat_bullish_ticker():
    tkr, score, _ = ds.parse_chat_sentiment("[9:41 AM]Mackenzie: EHGO bouncing")
    assert tkr == "EHGO"
    assert score > 0


def test_chat_bearish_no_ticker_is_market():
    tkr, score, _ = ds.parse_chat_sentiment("[9:40 AM]Mackenzie: choppy day")
    assert tkr is None
    assert score < 0


def test_chat_bearish_with_ticker():
    tkr, score, _ = ds.parse_chat_sentiment("[9:40 AM]Bullish Bob: OBAI all out lotto - loss")
    assert tkr == "OBAI"
    assert score < 0


def test_chat_hod_not_parsed_as_ticker():
    tkr, score, _ = ds.parse_chat_sentiment("[9:32 AM]Bullish Bob: EHGO hod = 8.07")
    assert tkr == "EHGO"   # HOD is skipped, EHGO is the ticker
    assert score > 0       # "hod" scores bullish


def test_chat_neutral_filtered():
    _, score, _ = ds.parse_chat_sentiment(
        "[9:32 AM]Joosshhhh [BULL]: good ol China stocks post PDT")
    assert score == 0.0


def test_chat_emoji_only_bullish():
    tkr, score, _ = ds.parse_chat_sentiment("[9:15 AM]Trader: SPY 🚀")
    assert tkr == "SPY"
    assert score > 0


def test_chat_transitioning_bullish():
    _, score, _ = ds.parse_chat_sentiment("[9:21 AM]NattyTrader05: OBAI - red to flat 💃")
    assert score > 0


def test_chat_red_to_green_nets_positive():
    _, score, _ = ds.parse_chat_sentiment("[9:00 AM]X: SPY red to green")
    assert score > 0   # phrase +0.9 dominates the consumed "red"/"green" sub-words


def test_chat_score_clamped():
    bull = "[9:00 AM]X: breakout ripping mooning bullish calls green running"
    bear = "[9:00 AM]X: breakdown dumping tanking bearish puts short dump"
    _, sb, _ = ds.parse_chat_sentiment(bull)
    _, sr, _ = ds.parse_chat_sentiment(bear)
    assert sb <= 1.0
    assert sr >= -1.0


def test_chat_non_chat_line_ignored():
    tkr, score, _ = ds.parse_chat_sentiment("EHGO bouncing")
    assert (tkr, score) == (None, 0.0)


def test_chat_bot_alert_body_ignored():
    tkr, score, _ = ds.parse_chat_sentiment(
        "[9:00 AM]Bot: INHD Spike >>>>> High = 41.83")
    assert (tkr, score) == (None, 0.0)


# ── Section C — Dashboard ingest ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(d, "add_ticker_to_log", lambda t: (True, True))
    with d.STATE.lock:
        d.STATE.mention_ts.clear()
        d.STATE.mention_daily.clear()
        d.STATE.discord_alerts.clear()
        d.STATE.discord_last_ts = 0.0
        d.STATE.sentiment_events.clear()
        d.STATE.sentiment_feed.clear()
    yield


def _ev(ticker, score, source="chat", ts=None):
    return {"ticker": ticker, "score": score, "source": source,
            "raw": "x", "ts": ts if ts is not None else time.time()}


def test_sentiment_event_stored_under_ticker():
    d.ingest_discord_alerts([], [_ev("SPY", -1.0, "hv_alert")])
    with d.STATE.lock:
        assert len(d.STATE.sentiment_events["SPY"]) == 1


def test_sentiment_market_event_stored_under_none():
    d.ingest_discord_alerts([], [_ev(None, -0.5, "chat")])
    with d.STATE.lock:
        assert len(d.STATE.sentiment_events[None]) == 1


def test_old_event_pruned():
    old_ts = time.time() - (d._SENTIMENT_WINDOW_SEC + 60)
    d.ingest_discord_alerts([], [_ev("EHGO", 0.8, "chat", ts=old_ts)])
    with d.STATE.lock:
        s = d._ticker_sentiment("EHGO")
    assert s["count"] == 0


def test_fresh_event_included_with_sign():
    d.ingest_discord_alerts([], [_ev("EHGO", 0.8, "chat")])
    with d.STATE.lock:
        s = d._ticker_sentiment("EHGO")
    assert s["count"] == 1
    assert s["score"] > 0


def test_recency_weighting_favors_newer():
    now    = time.time()
    old_ts = now - 29 * 60        # ~29 min old, bearish
    d.ingest_discord_alerts([], [
        _ev("TSLA", -1.0, "chat", ts=old_ts),
        _ev("TSLA",  1.0, "chat", ts=now),
    ])
    with d.STATE.lock:
        s = d._ticker_sentiment("TSLA")
    assert s["count"] == 2
    assert s["score"] > 0          # newer (bullish) event wins the weighted mean


def test_near_zero_filtered_before_storage():
    d.ingest_discord_alerts([], [_ev("AAPL", 0.02, "chat")])
    with d.STATE.lock:
        assert "AAPL" not in d.STATE.sentiment_events
        s = d._ticker_sentiment("AAPL")
    assert s["count"] == 0


def test_feed_grows_per_event():
    d.ingest_discord_alerts([], [
        _ev("SPY", -1.0, "hv_alert"),
        _ev("EHGO", 0.8, "chat"),
        _ev("AAPL", 0.01, "chat"),     # filtered, should not appear
    ])
    with d.STATE.lock:
        assert len(d.STATE.sentiment_feed) == 2


# ── Section D — Snapshot shape ────────────────────────────────────────────────

def test_snapshot_market_sentiment_present(monkeypatch):
    monkeypatch.setattr(d, "load_tickers", lambda: [])
    monkeypatch.setattr(d, "load_news", lambda: [])
    monkeypatch.setattr(d, "_load_signal_state", lambda: {})
    d.ingest_discord_alerts([], [_ev(None, -1.0, "hv_alert")])

    snap = d._snapshot()
    assert "market_sentiment" in snap
    assert "score" in snap["market_sentiment"]
    assert "count" in snap["market_sentiment"]
    assert snap["market_sentiment"]["count"] == 1


def test_snapshot_ticker_sentiment_only_when_present(monkeypatch):
    monkeypatch.setattr(d, "load_tickers", lambda: ["EHGO", "AAPL"])
    monkeypatch.setattr(d, "load_news", lambda: [])
    monkeypatch.setattr(d, "_load_signal_state", lambda: {})
    monkeypatch.setattr(d, "refresh_ticker_timestamps", lambda t: None)
    d.ingest_discord_alerts([], [_ev("EHGO", 0.8, "chat")])

    snap = d._snapshot()
    rows = {r["ticker"]: r for r in snap["tickers"]}
    assert "sentiment" in rows["EHGO"]
    assert rows["EHGO"]["sentiment"]["count"] == 1
    assert "sentiment" not in rows["AAPL"]


# ── Section D2 — Ranked sentiment ────────────────────────────────────────────

def test_ranked_sentiment_sorted_desc():
    d.ingest_discord_alerts([], [
        _ev("AAA", 0.3, "chat"),
        _ev("BBB", 0.9, "chat"),
        _ev("CCC", -0.4, "scanner"),
    ])
    with d.STATE.lock:
        ranked = d._ranked_sentiment()
    assert [r["ticker"] for r in ranked] == ["BBB", "AAA", "CCC"]
    assert ranked[0]["score"] > ranked[1]["score"] > ranked[2]["score"]


def test_ranked_sentiment_excludes_market_none_bucket():
    d.ingest_discord_alerts([], [_ev(None, -0.8, "chat"), _ev("EHGO", 0.5, "chat")])
    with d.STATE.lock:
        ranked = d._ranked_sentiment()
    assert [r["ticker"] for r in ranked] == ["EHGO"]


def test_ranked_sentiment_in_snapshot(monkeypatch):
    monkeypatch.setattr(d, "load_tickers", lambda: [])
    monkeypatch.setattr(d, "load_news", lambda: [])
    monkeypatch.setattr(d, "_load_signal_state", lambda: {})
    d.ingest_discord_alerts([], [_ev("EHGO", 0.8, "chat")])

    snap = d._snapshot()
    assert "sentiment_ranked" in snap["discord"]
    assert snap["discord"]["sentiment_ranked"][0]["ticker"] == "EHGO"


# ── Section E — High sentiment boosts mentions ────────────────────────────────

def test_high_chat_sentiment_adds_mention():
    d.ingest_discord_alerts([], [_ev("EHGO", 0.9, "chat")])
    with d.STATE.lock:
        count = d.STATE.mention_daily.get("EHGO", 0)
    assert count >= 1


def test_low_chat_sentiment_does_not_add_mention():
    d.ingest_discord_alerts([], [_ev("EHGO", 0.4, "chat")])
    with d.STATE.lock:
        count = d.STATE.mention_daily.get("EHGO", 0)
    assert count == 0


def test_hv_alert_does_not_add_mention():
    # SPY direction alerts fire constantly — they must not flood SPY's mention count.
    d.ingest_discord_alerts([], [_ev("SPY", 1.0, "hv_alert")])
    with d.STATE.lock:
        count = d.STATE.mention_daily.get("SPY", 0)
    assert count == 0


def test_high_tv_chart_sentiment_adds_mention():
    d.ingest_discord_alerts([], [_ev("ICCM", 1.0, "tv_chart")])
    with d.STATE.lock:
        count = d.STATE.mention_daily.get("ICCM", 0)
    assert count >= 1


def test_market_sentiment_no_ticker_no_mention():
    # None-ticker (market) events should never affect per-ticker mention counts.
    d.ingest_discord_alerts([], [_ev(None, 1.0, "chat")])
    with d.STATE.lock:
        total = sum(d.STATE.mention_daily.values())
    assert total == 0


# ── Section F — Scanner table detection (strict) ─────────────────────────────
# Strict contract: scanner only emits a ticker when the table is active AND the
# line is exactly "TICKER $price ±pct%". Lines with words, alert arrows, or chat
# timestamps are rejected (and arrows/chat close the table) — this is what kills
# the BB/HIGH/KEY/FOR misread noise.

_ROW = "EHGO $4.72 +18.2%"   # a canonical strict scanner row


def _run_scanner(lines: list[str]) -> list[ds.SentimentEvent]:
    """Drive _update_scanner_state across a list of OCR lines, return events."""
    scanner_state = ds._ScannerState()
    events: list[ds.SentimentEvent] = []
    seen: dict = {}
    t0 = time.time()
    for line in lines:
        ds._update_scanner_state(line, scanner_state, events, seen, t0)
    return events


def test_scanner_inactive_by_default():
    # Even a perfectly-shaped row emits nothing until Market Update activates it.
    events = _run_scanner([_ROW])
    assert events == []


def test_market_update_activates_scanner():
    scanner_state = ds._ScannerState()
    assert not scanner_state.active
    ds._update_scanner_state("Market Update 12:00 PM", scanner_state, [], {}, time.time())
    assert scanner_state.active


def test_scanner_emits_strict_row_after_sentinel():
    events = _run_scanner(["Market Update", _ROW])
    assert len(events) == 1
    assert events[0].ticker == "EHGO"
    assert events[0].source == "scanner"


def test_scanner_rejects_chat_alert_noise():
    # The exact lines from the live noise screenshot must produce NO events.
    events = _run_scanner([
        "Market Update",
        "BB LIVE Alerts",
        ">>>>> 1 Minute High Price = 41.83",
        "Set alerts below key levels to",
        "Trendy Tickers: CRVO - CQ",
    ])
    assert events == []


def test_scanner_boundary_closes_table():
    # An alert arrow line ends the table; a strict row AFTER it is not captured.
    events = _run_scanner(["Market Update", ">>>>> done", _ROW])
    assert events == []


def test_scanner_momentum_col_score():
    events = _run_scanner(["Market Update", "Momentum+HOD", _ROW])
    assert len(events) == 1
    assert events[0].score == 0.80


def test_scanner_gap_up_col_score():
    events = _run_scanner(["Market Update", "Gap Up", _ROW])
    assert len(events) == 1
    assert events[0].score == 0.75


def test_scanner_volume_breakout_col_score():
    events = _run_scanner(["Market Update", "Volume Breakout", _ROW])
    assert len(events) == 1
    assert events[0].score == 0.60


def test_scanner_price_spike_col_score():
    events = _run_scanner(["Market Update", "Price Spike", _ROW])
    assert len(events) == 1
    assert events[0].score == 0.55


def test_scanner_default_score_without_header():
    events = _run_scanner(["Market Update", _ROW])
    assert events[0].score == ds._SCANNER_DEFAULT


def test_scanner_dedup_same_line():
    scanner_state = ds._ScannerState()
    scanner_state.active = True
    events: list = []
    seen: dict = {}
    t0 = time.time()
    ds._update_scanner_state(_ROW, scanner_state, events, seen, t0)
    ds._update_scanner_state(_ROW, scanner_state, events, seen, t0)
    assert len(events) == 1


def test_scanner_momentum_ticker_adds_mention():
    d.ingest_discord_alerts([], [_ev("EHGO", 0.80, "scanner")])
    with d.STATE.lock:
        count = d.STATE.mention_daily.get("EHGO", 0)
    assert count >= 1


def test_scanner_price_spike_does_not_add_mention():
    d.ingest_discord_alerts([], [_ev("EHGO", 0.55, "scanner")])
    with d.STATE.lock:
        count = d.STATE.mention_daily.get("EHGO", 0)
    assert count == 0
