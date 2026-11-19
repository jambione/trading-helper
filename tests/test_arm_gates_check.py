import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import arm_gates_check as m  # noqa: E402


def _grid(today):
    prev = np.linspace(100, 95, 112)
    c = np.concatenate([prev, today])
    return c, c + 0.05, c - 0.05, 112


def test_rising_name_passes_both_gates():
    # dips to 92 then climbs: fast %R rises well over 15 points in 10 minutes and the slow line turns up
    cc, hh, ll, off = _grid(np.concatenate([np.linspace(95, 92, 30), np.linspace(92, 99, 20)]))
    a, b = m.gate_flags(cc, hh, ll, off, 44)
    assert a is False and b is False


def test_flat_then_falling_name_is_blocked():
    cc, hh, ll, off = _grid(np.concatenate([np.linspace(95, 105, 30), np.linspace(105, 101, 30)]))
    a, b = m.gate_flags(cc, hh, ll, off, 55)
    assert a is True and b is True


def test_decide_rules():
    big = {"n_pass": 500, "n_block": 500}
    assert m.decide({**big, "lift": 3.0, "t": 2.5}, {"lift": 1.0}) == "KEEP"
    assert m.decide({**big, "lift": 0.2, "t": 0.3}, {"lift": -1.0}) == "REMOVE"
    assert m.decide({**big, "lift": 1.2, "t": 1.0}, {"lift": 1.0}) == "UNCLEAR"
    assert m.decide({"n_pass": 100, "n_block": 500, "lift": 5, "t": 5}, {"lift": 1}) == "UNDERPOWERED"


def test_five_min_bars_end_on_the_last_minute():
    c = np.arange(1, 21, dtype=float)
    c5, h5, l5 = m.five_min(c, c, c)
    assert list(c5) == [5, 10, 15, 20] and list(l5) == [1, 6, 11, 16]


def test_pace_is_cumulative_volume_over_prior_day_share():
    v = np.full(390, 100.0)
    assert m.pace(v, 39000.0, 38) == 1.0           # 3,900 by minute 38 vs 39,000 x 39/390
    assert m.pace(v, 0.0, 38) is None


def test_scoring_splits_pools():
    rows = [{"day": "2026-05-01", "net": 1.0, "A_blocks": False, "B_blocks": False, "C_blocks": False, "D_blocks": False,
             "pool": "cache"},
            {"day": "2026-05-01", "net": -1.0, "A_blocks": True, "B_blocks": True, "C_blocks": True, "D_blocks": False,
             "pool": "below"}]
    assert m.lift([r for r in rows if r["pool"] == "cache"], "A_blocks")["n_block"] == 0
    assert m.lift(rows, "C_blocks")["n_block"] == 1
