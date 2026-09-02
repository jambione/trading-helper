"""Active-list cap, book priority eviction, unsubscribe-on-expire."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

se = pytest.importorskip("signal_engine", reason="signal_engine requires pandas")


def test_row_triggers_tracking_recognizes_book_src():
    ok, reason = se.row_triggers_tracking({"ticker": "AAA", "src": "book"}, track_desk=False)
    assert ok and reason == "book"


def test_pick_cold_nonbook_eviction_skips_book_and_warm():
    cold_desk = se.TickerState("COLD")
    cold_desk.src = ""
    cold_desk.ever_positive_hist = False
    cold_desk.added_ts = time.time() - 100

    book = se.TickerState("BOOK")
    book.src = "book"
    book.ever_positive_hist = False
    book.added_ts = time.time() - 200

    warm = se.TickerState("WARM")
    warm.src = ""
    warm.ever_positive_hist = True
    warm.added_ts = time.time() - 300

    pinned = se.TickerState("PIN", pinned=True)
    pinned.added_ts = time.time() - 400

    active = {"COLD": cold_desk, "BOOK": book, "WARM": warm, "PIN": pinned}
    assert se.pick_cold_nonbook_eviction(active) == "COLD"


def test_pick_cold_nonbook_eviction_none_when_only_book():
    book = se.TickerState("BOOK")
    book.src = "book"
    assert se.pick_cold_nonbook_eviction({"BOOK": book}) is None


def test_expire_tickers_calls_unsubscribe(monkeypatch):
    unsubbed = []

    monkeypatch.setattr(se, "request_unsubscribe", lambda syms: unsubbed.extend(syms))

    class _Eng:
        active = {}
        _known_mentioned = set()
        _expire_tickers = se.SignalEngine._expire_tickers

    eng = _Eng()
    ts = se.TickerState("ZZZ")
    ts.ever_positive_hist = False
    ts.added_ts = time.time() - (se.EXPIRY_COLD + 5)
    eng.active["ZZZ"] = ts
    eng._known_mentioned.add("ZZZ")

    eng._expire_tickers()
    assert "ZZZ" not in eng.active
    assert unsubbed == ["ZZZ"]


def test_ingest_evicts_cold_nonbook_for_book_when_full(monkeypatch):
    unsubbed = []
    monkeypatch.setattr(se, "request_unsubscribe", lambda syms: unsubbed.extend(syms))
    monkeypatch.setattr(se, "request_subscribe", lambda syms: None)
    monkeypatch.setattr(se, "MAX_ACTIVE_TICKERS", 2)
    monkeypatch.setattr(se, "get_latest_price", lambda s: None)

    class _Eng:
        active = {}
        _known_mentioned = set()
        _stagger_index = 0
        finnhub_key = ""
        _ingest_state = se.SignalEngine._ingest_state

    eng = _Eng()
    a = se.TickerState("NOIS")
    a.src = ""
    a.ever_positive_hist = False
    a.added_ts = time.time() - 50
    b = se.TickerState("KEEP")
    b.src = "book"
    b.ever_positive_hist = False
    eng.active = {"NOIS": a, "KEEP": b}
    eng._known_mentioned = {"NOIS", "KEEP"}

    state = {
        "tickers": [
            {"ticker": "NOIS", "price": 1.0},
            {"ticker": "KEEP", "price": 2.0, "src": "book"},
            {"ticker": "NEWB", "price": 3.0, "src": "book"},
        ]
    }
    eng._ingest_state(state)
    assert "NEWB" in eng.active
    assert "KEEP" in eng.active
    assert "NOIS" not in eng.active
    assert "NOIS" in unsubbed
    assert eng.active["NEWB"].src == "book"


def test_pick_cold_nonbook_eviction_falls_back_to_warm():
    """When every non-book slot is warm, still free the oldest for a book seed."""
    warm_old = se.TickerState("OLDW")
    warm_old.src = ""
    warm_old.ever_positive_hist = True
    warm_old.added_ts = time.time() - 300

    warm_new = se.TickerState("NEWW")
    warm_new.src = ""
    warm_new.ever_positive_hist = True
    warm_new.added_ts = time.time() - 50

    book = se.TickerState("BOOK")
    book.src = "book"
    book.ever_positive_hist = True
    book.added_ts = time.time() - 400

    active = {"OLDW": warm_old, "NEWW": warm_new, "BOOK": book}
    assert se.pick_cold_nonbook_eviction(active) == "OLDW"
