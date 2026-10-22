"""CR1 means, differences, guards, verdicts and freeze keys for floor20_forward. No market data."""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import floor20_forward as F  # noqa: E402


def _rows(mu, days=22, per=8, seed=1, first=23):
    rng = random.Random(seed)
    return [{"day": f"2026-11-{d:02d}", "sym": f"S{k % 6}", "bp": mu + rng.gauss(0, 20)}
            for d in range(1, days + 1) for k in range(per)]


def test_cr1_and_diff():
    a = F.cr1(_rows(10))
    assert a["n"] == 176 and a["sessions"] == 22 and abs(a["mean"] - 10) < 4 and a["t"] > 3
    assert abs(a["day_equal"] - a["mean"]) < 1e-9          # equal fills per day
    b = F.cr1(_rows(-5, seed=2))
    d = F.diff(a, b)
    assert d["diff"] > 10 and d["t"] > 3


def test_verdicts():
    good = _rows(10)
    fv, cmp_ = F.cr1(good), F.cr1(_rows(-5, seed=2))
    halves = [F.cr1(good[:88]), F.cr1(good[88:])]
    guards = {"minus_spy": 9.0, "drop_top3_sessions": F.drop_top(good, "day"), "drop_top3_names": F.drop_top(good, "sym")}
    assert F.verdict(fv, F.diff(fv, cmp_), halves, guards).startswith("PROFITABLE")
    assert F.verdict(fv, F.diff(fv, cmp_), halves, dict(guards, minus_spy=-1.0)) == "BETTER_NOT_PROFITABLE"
    small = F.cr1(_rows(10, days=5))
    assert F.verdict(small, F.diff(small, cmp_), halves, guards) == "UNDERPOWERED"
    bad = F.cr1(_rows(-20, seed=3))
    assert F.verdict(bad, F.diff(bad, cmp_), halves, guards) == "WORSE"
    same = F.cr1(_rows(-5, seed=4))
    assert F.verdict(same, F.diff(same, cmp_), halves, guards) == "NO_CHANGE"


def test_freeze_keys():
    ref = {"ai_watch_min_price": 20, "ui_theme": "dark", "trade_amount": 1000, "ai_trail_give_r": 0.2}
    cur = dict(ref, ui_theme="light", ai_trail_give_r=0.3)
    assert F.freeze_keys(cur, ref) == ["ai_trail_give_r"]
    assert F.freeze_keys(dict(ref, trade_amount=2000), ref) == ["trade_amount"]
