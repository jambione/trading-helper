import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import low_heat_sim as m  # noqa: E402


def _bars(px=100.0):
    a = np.full(390, px)
    return a.copy(), a.copy(), a.copy(), a.copy()


def test_book_respects_three_slots_and_one_per_name():
    bars = {s: _bars() for s in "ABCDE"}
    sig = [(30, "A"), (30, "B"), (31, "C"), (32, "D"), (33, "A"), (60, "E")]
    tr = m.run_day(sig, m.bracket_exit, bars, lambda s, t: 2.0)
    assert [x["sym"] for x in tr] == ["A", "B", "C", "E"]          # D: book full; A again: still held until 45
    assert all(x["net_bp"] == -2.0 for x in tr) and tr[0]["pnl"] == round(3200 * -2 / 1e4, 2)


def test_wide_spread_is_skipped_and_slot_stays_free():
    bars = {"A": _bars(), "B": _bars()}
    tr = m.run_day([(30, "A"), (31, "B")], m.bracket_exit, bars, lambda s, t: 20.0 if s == "A" else 3.0)
    assert [x["sym"] for x in tr] == ["B"]


def test_summary_drawdown():
    days = [{"day": "d1", "n": 2, "pnl": 10.0, "wins": 1, "bp_sum": 5.0}, {"day": "d2", "n": 2, "pnl": -30.0, "wins": 0, "bp_sum": -9.0},
            {"day": "d3", "n": 1, "pnl": 5.0, "wins": 1, "bp_sum": 2.0}]
    s = m.summary(days)
    assert s["total_pnl"] == -15.0 and s["max_drawdown"] == 30.0 and s["opens_per_day"] == 1.7
