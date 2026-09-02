"""Book-wide Finnhub glue: never expire on_desk book; priority sub; grace."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

se = pytest.importorskip("signal_engine", reason="signal_engine requires pandas")
fh = pytest.importorskip("finnhub_stream")


def test_book_src_on_desk_never_expires():
    ts = se.TickerState("MOVE")
    ts.src = "book"
    ts.on_desk = True
    ts.added_ts = time.time() - 99999
    ts.ever_positive_hist = True
    assert not ts.is_expired()


def test_book_src_off_desk_has_grace_then_expires():
    ts = se.TickerState("ALMS")
    ts.src = "book"
    ts.on_desk = False
    ts.added_ts = time.time() - 99999
    # First call starts grace clock
    assert not ts.is_expired()
    assert ts._book_off_desk_ts is not None
    # Still inside grace
    ts._book_off_desk_ts = time.time() - (se.EXPIRY_COLD - 5)
    assert not ts.is_expired()
    # Past grace
    ts._book_off_desk_ts = time.time() - (se.EXPIRY_COLD + 5)
    assert ts.is_expired()


def test_nonbook_still_expires_normally():
    ts = se.TickerState("NOIS")
    ts.src = ""
    ts.on_desk = False
    ts.ever_positive_hist = False
    ts.added_ts = time.time() - (se.EXPIRY_COLD + 5)
    assert ts.is_expired()


def test_request_unsubscribe_skips_priority_unless_forced():
    while not fh._pending_unsubs.empty():
        try:
            fh._pending_unsubs.get_nowait()
        except Exception:
            break
    fh.set_subscribe_priority(["MOVE", "ALMS", "ASST"])
    fh.request_unsubscribe(["MOVE", "NOIS"])
    got = []
    while not fh._pending_unsubs.empty():
        got.append(fh._pending_unsubs.get_nowait())
    assert got == ["NOIS"]
    fh.request_unsubscribe(["MOVE"], force=True)
    got = []
    while not fh._pending_unsubs.empty():
        got.append(fh._pending_unsubs.get_nowait())
    assert got == ["MOVE"]
    fh.set_subscribe_priority([])


def test_request_subscribe_evicts_nonpriority_for_priority(monkeypatch):
    while not fh._pending_subs.empty():
        try:
            fh._pending_subs.get_nowait()
        except Exception:
            break
    while not fh._pending_unsubs.empty():
        try:
            fh._pending_unsubs.get_nowait()
        except Exception:
            break
    with fh.FINNHUB_STATE.lock:
        fh.FINNHUB_STATE.subscribed = {
            f"D{i}" for i in range(fh.MAX_WS_SUBSCRIPTIONS)
        }
    fh.set_subscribe_priority(["BOOKA"])
    fh.request_subscribe(["BOOKA"])
    unsubs = []
    while not fh._pending_unsubs.empty():
        unsubs.append(fh._pending_unsubs.get_nowait())
    subs = []
    while not fh._pending_subs.empty():
        subs.append(fh._pending_subs.get_nowait())
    assert unsubs, "must free a non-priority slot"
    assert "BOOKA" in subs
    fh.set_subscribe_priority([])
    with fh.FINNHUB_STATE.lock:
        fh.FINNHUB_STATE.subscribed = set()
