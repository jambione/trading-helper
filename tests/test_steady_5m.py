import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import steady_5m as m  # noqa: E402


def test_bars5_aggregates_aligned_to_the_open():
    c = np.arange(390, dtype=float) + 100
    o5, h5, l5, c5, v5 = m.bars5(c, c + 1, c - 1, c, np.ones(390))
    assert len(c5) == 78 and c5[0] == 104 and o5[1] == 105 and h5[0] == 105 and l5[0] == 99 and v5[0] == 5


def test_exit_turns_down_after_overbought_or_stops():
    o = np.full(390, 100.0); l = o.copy(); c = o.copy()
    fast = np.full(78, -50.0)
    fast[22], fast[23] = -10.0, -15.0          # overbought at bar 22, turns down at bar 23 (ends minute 119)
    c[119] = 101.0
    g, mm = m.exit5(o, l, c, 100, fast)
    assert mm == 119 and round(g) == 100
    l[105] = 96.0
    g2, mm2 = m.exit5(o, l, c, 100, fast)
    assert mm2 == 105 and round(g2) == -300


def test_lowheat5_and_box5_triggers():
    fast = np.array([-90, -85, -80, -70, -60, -50, -30, -25, -24, -23.0])
    assert m.lowheat5(fast)[0] == 2
    slow = np.full(10, -30.0)
    assert m.box5(fast, slow)[0] == 7


def test_refractory_30_minutes():
    assert m.refract([10, 11, 16, 17]) == [10, 16]
