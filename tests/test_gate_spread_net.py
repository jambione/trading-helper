"""Quoted spread at the entry, board pick and the net difference for gate_spread_net. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import gate_spread_net as N  # noqa: E402


def test_spread_from_last_clean_quote():
    t = 1000.0
    qs = [(980.0, 9.99, 10.01, 100, 100), (995.0, 10.00, 10.00, 100, 100),    # locked: skipped
          (999.0, 9.98, 10.02, 100, 0), (1001.0, 9.0, 11.0, 1, 1)]            # zero size; after t
    assert abs(N.spread_from(qs, t) - 0.02 / 10.0) < 1e-12
    assert N.spread_from([(960.0, 9.99, 10.01, 1, 1)], t) is None             # older than 30 s


def test_board_pick_seeded_and_net():
    a = N.board_pick("2026-10-05", "X", ["A", "B", "C", "D", "E"])
    assert a == N.board_pick("2026-10-05", "X", ["A", "B", "C", "D", "E"]) and len(a) == 3
    rows = [{"key": "k", "sym": "X", "board_pick": ["A", "B"], "ret": 0.0020, "ctrl": 0.0005, "day": "d"}]
    q = {"k": {"X": 0.0012, "A": 0.0004, "B": None}}
    r = N.net_rows(rows, q)[0]
    assert abs(r["net_diff"] - ((0.0020 - 0.0012) - (0.0005 - 0.0004))) < 1e-12
    assert N.net_rows(rows, {"k": {"X": None, "A": 0.0004}}) == []
