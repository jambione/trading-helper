"""Cut, reclaim, rotation mechanics of the dip-ideas study. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import dip_ideas_study as D  # noqa: E402


def _bars(n=30, start=600, px=100.0):
    return {start + i: (px, px, px, px) for i in range(n)}


def test_cut_sells_at_the_next_open():
    bars = _bars()
    bars[603] = (100, 100, 99.8, 99.85)
    bars[604] = (99.7, 99.8, 99.6, 99.7)
    ct = D.cut_trade(bars, {603: 99.8}, 600)                 # entry 100 at 601's open, stop 99.90
    assert ct["cut"] and ct["cut_min"] == 3 and abs(ct["gross"] - (99.7 / 100 - 1)) < 1e-12   # sold at 604's open
    del bars[604]
    gap = D.cut_trade(bars, {603: 99.8}, 600)                # missing next bar: the next one after it, never earlier
    assert abs(gap["gross"] - (bars[605][0] / 100 - 1)) < 1e-12
    late = D.cut_trade(bars, {607: 99.0}, 600)               # dip after the 5-minute window: held
    assert not late["cut"] and late["gross"] == 0.0


def test_reclaim_needs_the_dip_first():
    ref = 100.0
    assert D.reclaim_entry({}, {601: 100.2}, 600, ref) is None                         # never dipped
    assert D.reclaim_entry({602: 99.8}, {602: 99.85, 604: 100.0}, 600, ref) == 605     # dip 602, reclaim 604
    assert D.reclaim_entry({602: 99.8}, {602: 100.1}, 600, ref) == 603                 # same bar: low then close
    assert D.reclaim_entry({612: 99.0}, {613: 101.0}, 600, ref) is None                # outside the 10-minute window


def test_rotation_ratio():
    assert D.rotation(5e6, 4.0) == 1.25
    assert D.rotation(5e6, None) is None and D.rotation(None, 4.0) is None and D.rotation(1.0, 0) is None


def test_verdict_family_bar():
    assert D.verdict(0.001, 2.7, 0.0006, 2.7, 60, 10) == "PASS"
    assert D.verdict(0.001, 2.6, 0.0006, 2.8, 60, 10) == "UNDERPOWERED"
    assert D.verdict(-0.001, 3.0, 0.0006, 2.6, 60, 10) == "FAIL"
    assert D.verdict(0.001, 3.0, 0.0004, 2.6, 60, 10) == "FAIL"
    assert D.verdict(0.001, 3.0, 0.0006, 2.6, 59, 10) == "UNDERPOWERED"


def test_reclaim_verdict_needs_to_beat_the_control():
    days = {f"d{i}": [0.002 + i * 1e-4] for i in range(10)}
    x = {"verdict": "PASS", "by_day": days}
    assert D.reclaim_verdict(x, {"by_day": {d: [v[0] - 0.001] for d, v in days.items()}})[0] == "PASS"
    assert D.reclaim_verdict(x, {"by_day": {d: [v[0] + 0.001] for d, v in days.items()}})[0] == "FAIL"
    assert D.kill([-0.001, 0.0]) and not D.kill([-0.001, 0.0001])
