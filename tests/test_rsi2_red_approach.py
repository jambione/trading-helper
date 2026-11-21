import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import rsi2_red_approach as A  # noqa: E402


def test_group_uses_prior_bar_only():
    assert A.group(85.0) == "G" and A.group(80.0) == "G" and A.group(59.9) == "J" and A.group(61.0) == "M"
    assert A.group(float("nan")) is None


def test_signals_closed_red_start_and_entry_next_open():
    n = 390
    d = {"c": [np.full(n, 30.0)], "pc": [30.0], "red_pine": [np.zeros(n, bool)], "rsi_pine": [np.full(n, 50.0)]}
    d["red_pine"][0][40:43] = True
    d["rsi_pine"][0][39] = 85.0
    d["red_pine"][0][60] = True
    d["rsi_pine"][0][59] = 30.0
    sig = A.signals(d, 0)
    assert ("G", 40, 41) in sig and ("J", 60, 61) in sig and len(sig) == 2
    d["rsi_pine"][0][41:] = 0.0                     # anything after t cannot change the group
    assert A.signals(d, 0)[0] == ("G", 40, 41)
