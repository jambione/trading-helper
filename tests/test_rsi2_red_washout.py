import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import rsi2_red_washout as W  # noqa: E402


def test_low_bar_completed_only_and_early():
    l = np.array([10.0] * 20)
    l[17] = 9.0
    assert W.low_bar(l, 19) == 17 and W.early(l, 19)       # low 2 bars back
    assert W.early(l, 18)                                    # low 1 bar back
    assert not W.early(l, 17)                                # bar 17 itself is not yet completed at t=17 -> low elsewhere
    l2 = l.copy(); l2[12] = 8.0
    assert not W.early(l2, 19)                               # the 15-minute low is 7 bars back
