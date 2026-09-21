"""A price cell that never asks what time it is.

2026-09-21, 06:00 ET premarket: the board showed USDE at 10.33 while it
traded 11.87. Not a bad quote — the last trade IEX itself saw, from Friday
16:10, 62 hours earlier. Eleven of sixteen watchlist names were in the same
state, every one of them rendered as a live price, because the price cell
asked whether a price EXISTED and never how old it was. The engine's
admission gate was not fooled (it reads the dated rt_* pair and refused all
15 candidates with stale_tape_admit); only the display was.

The clock was already on the row — ``price_age_sec``, stamped in the merge
from the trade's own timestamp. These tests pin the verdict that reads it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard as d   # noqa: E402


# ── the verdict ───────────────────────────────────────────────────────────

def test_a_print_older_than_the_ceiling_is_stale():
    assert d.price_is_stale(222_400.0) is True      # USDE, Friday 16:10
    assert d.price_is_stale(d.BOARD_PRICE_STALE_SEC + 1) is True


def test_a_young_print_is_not_stale():
    assert d.price_is_stale(0.0) is False
    assert d.price_is_stale(14.0) is False
    assert d.price_is_stale(d.BOARD_PRICE_STALE_SEC) is False


def test_unknown_age_is_neither():
    """None means the winning source could not date its quote — the Finnhub
    REST path never can. Unknown must not read as stale (half a normal
    session would grey out, burying the real case) nor be claimed as fresh
    anywhere else."""
    assert d.price_is_stale(None) is False
    assert d.price_is_stale("") is False
    assert d.price_is_stale("not-a-number") is False


# ── the row ───────────────────────────────────────────────────────────────

def _snapshot_rows(monkeypatch, tickers: dict):
    monkeypatch.setattr(d, "load_tickers", lambda: list(tickers))
    monkeypatch.setattr(d, "load_trending", lambda: [])
    monkeypatch.setattr(d, "_ai_book_symbols", lambda: [])
    monkeypatch.setattr(d, "_ticker_src_map", lambda: {})
    monkeypatch.setattr(d, "_build_mention_rank", lambda syms: {})
    monkeypatch.setattr(d, "refresh_ticker_timestamps", lambda syms: None)
    with d.STATE.lock:
        d.STATE.tickers.clear()
        d.STATE.tickers.update(tickers)
    return {r["ticker"]: r for r in d._snapshot()["tickers"]}


def test_stale_row_is_marked_and_loses_its_percentage(monkeypatch):
    rows = _snapshot_rows(monkeypatch, {
        "USDE": {"price": 10.33, "price_age_sec": 222_400.0, "day_open": 10.20},
    })
    r = rows["USDE"]
    assert r["price"] == 10.33          # still shown — it is the best estimate
    assert r["price_stale"] is True
    # Friday's close against today's open is not this morning's move.
    assert r["pct_change"] is None


def test_live_row_keeps_its_percentage(monkeypatch):
    rows = _snapshot_rows(monkeypatch, {
        "HOOD": {"price": 124.85, "price_age_sec": 8.0, "day_open": 119.82},
    })
    r = rows["HOOD"]
    assert r["price_stale"] is False
    assert r["pct_change"] == 4.20


def test_undated_row_is_left_alone(monkeypatch):
    """A Finnhub REST quote carries no trade clock. It must keep behaving
    exactly as it did before this change existed."""
    rows = _snapshot_rows(monkeypatch, {
        "SNAP": {"price": 5.52, "price_age_sec": None, "day_open": 5.54},
    })
    r = rows["SNAP"]
    assert r["price_stale"] is False
    assert r["pct_change"] == -0.36


def test_row_with_no_price_is_not_called_stale(monkeypatch):
    """Absent is not stale: the scanner-snapshot path owns that cell."""
    rows = _snapshot_rows(monkeypatch, {
        "OIG": {"price": None, "price_age_sec": None},
    })
    assert rows["OIG"]["price_stale"] is False
