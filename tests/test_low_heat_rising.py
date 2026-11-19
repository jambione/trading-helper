import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import low_heat_rising as m  # noqa: E402


def test_turn_fires_on_second_rising_bar_below_40():
    off = 112
    c = np.concatenate([np.full(off, 100.0), np.linspace(100, 90, 40), [90.2, 90.5, 90.9], np.full(347, 90.9)])
    h, l = c + 0.02, c - 0.02
    turns, heat = m.heat_turns(c, h, l, off)
    assert turns and turns[0] == 41
    assert heat[turns[0]] < 40


def test_hold_exit_stop_and_close():
    o = np.full(390, 50.0); l = o.copy(); c = o.copy()
    c[385] = 51.0
    assert round(m.hold_exit(o, l, c, 100)) == 200
    l[200] = 48.0
    assert round(m.hold_exit(o, l, c, 100)) == -300


def test_sample_caps_per_arm_and_period():
    rows = [{"arm": "low_heat", "day": "2026-05-01", "sym": f"S{i}", "t": 20} for i in range(5000)]
    assert len(m.sample(rows)) == m.CAP
