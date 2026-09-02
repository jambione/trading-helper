"""Dashboard must not steal the engine's one Finnhub connection."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fh = pytest.importorskip("finnhub_stream")


def test_same_key_is_a_collision(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY_ENGINE", "abc123")
    assert fh.dashboard_ws_collides_with_engine("abc123") is True


def test_distinct_keys_are_not_a_collision(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY_ENGINE", "engine-key")
    assert fh.dashboard_ws_collides_with_engine("dash-key") is False


def test_empty_dashboard_key_is_not_a_collision(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY_ENGINE", "abc123")
    assert fh.dashboard_ws_collides_with_engine("") is False


def test_request_subscribe_respects_max_cap(monkeypatch):
    """Overflow must be refused loudly — never silent-subscribe past ~50."""
    # Drain any leftover queue state from other tests / prior runs.
    while not fh._pending_subs.empty():
        try:
            fh._pending_subs.get_nowait()
        except Exception:
            break
    with fh.FINNHUB_STATE.lock:
        fh.FINNHUB_STATE.subscribed = {f"S{i}" for i in range(fh.MAX_WS_SUBSCRIPTIONS)}

    fh.request_subscribe(["NEWA", "NEWB"])
    assert fh._pending_subs.empty(), "must not queue past the WS ceiling"


def test_request_subscribe_queues_when_room_exists(monkeypatch):
    while not fh._pending_subs.empty():
        try:
            fh._pending_subs.get_nowait()
        except Exception:
            break
    with fh.FINNHUB_STATE.lock:
        # Leave two slots free.
        fh.FINNHUB_STATE.subscribed = {
            f"S{i}" for i in range(fh.MAX_WS_SUBSCRIPTIONS - 2)
        }

    fh.request_subscribe(["NEWA", "NEWB", "NEWC"])
    queued = []
    while not fh._pending_subs.empty():
        queued.append(fh._pending_subs.get_nowait())
    assert queued == ["NEWA", "NEWB"], f"expected 2 queued, got {queued}"


def test_request_unsubscribe_queues_symbol():
    while not fh._pending_unsubs.empty():
        try:
            fh._pending_unsubs.get_nowait()
        except Exception:
            break
    fh.request_unsubscribe(["aapl", "MSFT"])
    got = []
    while not fh._pending_unsubs.empty():
        got.append(fh._pending_unsubs.get_nowait())
    assert got == ["AAPL", "MSFT"]

