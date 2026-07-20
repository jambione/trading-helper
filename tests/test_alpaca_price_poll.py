"""Unit tests for alpaca_price_poll (no network)."""
import time

import alpaca_price_poll as px


def test_update_and_get_price():
    px.STATE.prices.clear()
    px.STATE.update("aapl", 190.5, 10)
    assert px.get_latest_price("AAPL") == 190.5
    assert px.get_latest_price("MSFT") is None


def test_subscribe_set():
    px.STATE.subscribed.clear()
    px.request_subscribe(["nvda", "amd"])
    assert "NVDA" in px.STATE.subscribed
    assert "AMD" in px.STATE.subscribed
    px.request_unsubscribe(["nvda"])
    assert "NVDA" not in px.STATE.subscribed


def test_stale_price_returns_none():
    px.STATE.prices.clear()
    px.STATE.update("X", 1.0, 1)
    with px.STATE.lock:
        px.STATE.prices["X"]["ts_unix"] = time.time() - 60
    assert px.get_latest_price("X") is None
