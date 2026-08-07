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
        d.STATE.price_spikes.clear()
        d.STATE.price_spike_base_ts.clear()
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


def test_alert_deque_drops_oldest_at_capacity():
    cap = d._MAX_DISCORD_ALERTS
    for i in range(cap + 5):
        d.ingest_discord_alerts([{"ticker": "TST", "line": f"TST >>>>> tick {i}"}])
    st = d.discord_status()
    assert len(st["alerts"]) == cap
    # The most recent entry should be tick (cap+4); the oldest were evicted.
    assert f"tick {cap + 4}" in st["alerts"][-1]["line"]


def test_price_spike_ingested_only_once(monkeypatch):
    monkeypatch.setattr(d, "_archive_price_spike", lambda rec: None)
    monkeypatch.setattr(d, "_send_price_spike_push", lambda *a, **k: None)
    alert = {
        "ticker": "LUCY",
        "line": "[ELITE] $LUCY Price Spike! | Float 5.14M",
        "alert_type": "Price Spike",
        "float_size": 5_140_000,
        "scanner_tier": "ELITE",
        "price_spike": True,
    }
    assert d.ingest_discord_alerts([alert]) == 1
    assert d.ingest_discord_alerts([alert]) == 0
    assert d.ingest_discord_alerts([dict(alert, line="different summary line")]) == 0
    with d.STATE.lock:
        assert d.STATE.mention_daily["LUCY"] == 1
        assert len(d.STATE.price_spikes) == 1


def test_price_spikes_expire_after_3_minutes(monkeypatch):
    monkeypatch.setattr(d, "_archive_price_spike", lambda rec: None)
    monkeypatch.setattr(d, "_send_price_spike_push", lambda *a, **k: None)
    monkeypatch.setattr(d, "load_tickers", lambda: [])
    monkeypatch.setattr(d, "load_news", lambda: [])
    monkeypatch.setattr(d, "load_swing", lambda: [])
    monkeypatch.setattr(d, "_load_signal_state", lambda: {})
    alert = {
        "ticker": "WOK",
        "line": "[ELITE] $WOK Price Spike! | Price $251",
        "alert_type": "Price Spike",
        "price": 251.0,
        "scanner_tier": "ELITE",
        "price_spike": True,
    }
    d.ingest_discord_alerts([alert])
    stale = time.time() - (d._SPIKE_TTL_SEC + 1)
    with d.STATE.lock:
        for rec in d.STATE.price_spikes:
            rec["unix"] = stale
        for key in list(d.STATE.price_spike_base_ts):
            d.STATE.price_spike_base_ts[key] = stale
    snap = d._snapshot()
    assert snap["price_spikes"] == []
    # De-dupe cleared too — same spike can ingest again after expiry.
    assert d.ingest_discord_alerts([alert]) == 1


def test_price_spike_records_mention_feed_and_snapshot(monkeypatch):
    monkeypatch.setattr(d, "_archive_price_spike", lambda rec: None)
    monkeypatch.setattr(d, "_send_price_spike_push", lambda *a, **k: None)
    n = d.ingest_discord_alerts([{
        "ticker": "LUCY",
        "line": "[ELITE] $LUCY Price Spike! | Float 5.14M",
        "alert_type": "Price Spike",
        "float_size": 5_140_000,
        "scanner_tier": "ELITE",
        "price_spike": True,
    }])
    assert n == 1
    with d.STATE.lock:
        assert d.STATE.mention_daily["LUCY"] == 1
        spikes = list(d.STATE.price_spikes)
        assert spikes[-1]["ticker"] == "LUCY"
        assert spikes[-1]["float_size"] == 5_140_000
        assert d.STATE.discord_alerts[-1]["price_spike"] is True

    monkeypatch.setattr(d, "load_tickers", lambda: ["LUCY"])
    monkeypatch.setattr(d, "load_news", lambda: [])
    monkeypatch.setattr(d, "load_swing", lambda: [])
    monkeypatch.setattr(d, "_load_signal_state", lambda: {})
    snap = d._snapshot()
    assert any(s["ticker"] == "LUCY" for s in snap["price_spikes"])


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


# ── Scanner price seeding ────────────────────────────────────────────────────

def test_scanner_price_seeds_its_own_field_not_the_live_price(monkeypatch):
    """The card price is a snapshot from alert time, not a quote — the two
    disagree in practice. It must never land in "price", where the UI and the
    signal path would read it as a live print."""
    d.ingest_discord_alerts([{
        "ticker": "MB",
        "line": "[ELITE] $MB | Price $5.33 | Float 4.69M",
        "price": 5.33,
        "float_size": 4_690_000,
    }])
    with d.STATE.lock:
        entry = d.STATE.tickers.get("MB", {})
        assert entry.get("scanner_price") == 5.33
        assert entry.get("price") is None


def test_scanner_price_surfaces_with_an_age(monkeypatch):
    """An OTC row would otherwise be permanently blank — Alpaca and Finnhub
    both return empty for those, so this is the only number it will ever get."""
    d.ingest_discord_alerts([{
        "ticker": "FENIA",
        "line": "[ELITE] $FENIA | Price $1.20 | Float 909M",
        "price": 1.20,
    }])
    monkeypatch.setattr(d, "load_tickers", lambda: ["FENIA"])
    monkeypatch.setattr(d, "load_news", lambda: [])
    monkeypatch.setattr(d, "load_swing", lambda: [])
    monkeypatch.setattr(d, "_load_signal_state", lambda: {})
    row = next(r for r in d._snapshot()["tickers"] if r["ticker"] == "FENIA")
    assert row["scanner_price"] == 1.20
    assert row["scanner_price_age_sec"] is not None
    assert row.get("price") is None
    # The raw timestamp is an internal detail; only the age is published.
    assert "scanner_price_ts" not in row


def test_absent_or_zero_scanner_price_seeds_nothing():
    d.ingest_discord_alerts([
        {"ticker": "AMD", "line": "AMD >>>>> x"},
        {"ticker": "NVDA", "line": "NVDA >>>>> x", "price": 0},
    ])
    with d.STATE.lock:
        assert "scanner_price" not in d.STATE.tickers.get("AMD", {})
        assert "scanner_price" not in d.STATE.tickers.get("NVDA", {})
