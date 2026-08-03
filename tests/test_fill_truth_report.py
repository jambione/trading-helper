"""Unit tests for tools/fill_truth_report (no live Alpaca)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from fill_truth_report import _pair_round_trips, tag_fills  # noqa: E402


def test_pair_round_trips_fifo():
    fills = [
        {"symbol": "AAA", "side": "buy", "filled_qty": 10, "filled_avg_price": 10.0,
         "filled_at": "2026-01-01T15:00:00Z"},
        {"symbol": "AAA", "side": "sell", "filled_qty": 10, "filled_avg_price": 11.0,
         "filled_at": "2026-01-01T16:00:00Z"},
    ]
    closed = _pair_round_trips(fills)
    assert len(closed) == 1
    assert closed[0]["pnl_pct"] == 10.0
    assert closed[0]["symbol"] == "AAA"


def test_tag_fills_source_priority():
    fills = [
        {"symbol": "AI1", "side": "buy", "filled_qty": 1, "filled_avg_price": 1},
        {"symbol": "ENG1", "side": "buy", "filled_qty": 1, "filled_avg_price": 1},
        {"symbol": "ZZZ", "side": "buy", "filled_qty": 1, "filled_avg_price": 1},
    ]
    tagged = tag_fills(fills, {"AI1"}, {"ENG1", "AI1"})
    by = {t["symbol"]: t["source"] for t in tagged}
    assert by["AI1"] == "ai"
    assert by["ENG1"] == "engine"
    assert by["ZZZ"] == "manual"
