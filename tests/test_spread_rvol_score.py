"""Costing, opens and the pre-registered pick of the spread x RVOL sweep. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import spread_rvol_score as S  # noqa: E402


def test_arm_day_costs_and_counts():
    closed = [{"symbol": "A", "entry_ts": 0.0, "exit_ts": 600.0, "ret": 0.002},
              {"symbol": "B", "entry_ts": 0.0, "exit_ts": 300.0, "ret": -0.001},
              {"symbol": "C", "entry_ts": 0.0, "exit_ts": None, "ret": None}]
    spreads = {"A": 5.0, "B": None}
    r = S.arm_day(closed, lambda s, ts: spreads.get(s), med=8.0)
    assert r["n"] == 2 and r["no_nbbo"] == 1
    assert abs(r["net"] - ((20 - 5) + (-10 - 8)) / 2) < 1e-9 and abs(r["value"] - (-3.0)) < 1e-9
    assert abs(r["conc"] - 900 / S.WINDOW_SEC) < 1e-12


def test_pick_needs_the_opens_bar():
    summ = {"busy": {"per10": 1.2, "value": {"mean": 10.0}}, "rich": {"per10": 0.5, "value": {"mean": 50.0}},
            "busier": {"per10": 2.0, "value": {"mean": 5.0}}}
    assert S.pick(summ) == "busy"
    assert S.pick({"x": {"per10": 0.2, "value": {"mean": 1.0}}}) is None
