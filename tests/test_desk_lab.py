"""Observe ghost book — no orders."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import desk_lab as lab  # noqa: E402


def test_spread_classify():
    assert lab.spread_pct(10.00, 10.005) is not None
    assert lab.spread_pct(10.00, 10.005) < 0.10
    assert lab.classify_spread(0.05, 0.10) == "cheap"
    assert lab.classify_spread(0.50, 0.10) == "wide"
    assert lab.classify_spread(None, 0.10) == "unknown"


def test_ghost_through_stop():
    g = lab.ghost_row("AAA", last=97.0, day_open=100.0, stop_pct=2.0)
    assert g is not None
    assert g["through_stop"] is True
    assert g["stop"] == 98.0
    g2 = lab.ghost_row("AAA", last=101.0, day_open=100.0, stop_pct=2.0, spy_chg_pct=0.5)
    assert g2["through_stop"] is False
    assert g2["pnl_pct"] == 1.0
    assert g2["vs_spy"] == 0.5


def test_build_lab_observe_headline():
    tickers = [
        {"ticker": "SPY", "price": 500.0, "day_open": 498.0, "pct_change": 0.4,
         "bid": 499.9, "ask": 500.1},
        {"ticker": "COST", "price": 910.0, "day_open": 900.0,
         "bid": 909.95, "ask": 910.05},
        {"ticker": "JUNK", "price": 3.2, "day_open": 3.0, "bid": 3.0, "ask": 3.4},
    ]
    book = {"entry_book": [
        {"symbol": "TEM", "block_code": "desk_observe"},
        {"symbol": "COST", "block_code": "desk_observe"},
    ]}
    snap = lab.build_lab({"desk_product": "observe", "h4_stop_pct": 2.0,
                          "h4_max_spread_pct": 0.10, "h4_min_price": 10.0},
                         tickers, book)
    assert snap["product"] == "observe"
    assert snap["refused"] == 2
    assert any(g["symbol"] == "COST" for g in snap["ghosts"])
    assert not any(g["symbol"] == "JUNK" for g in snap["ghosts"])
    assert "OBSERVE" in snap["headline"]
    assert snap["note"]
