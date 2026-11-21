import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import rsi2_red_washout as W  # noqa: E402
from early_entry_honest import os_leave_flag  # noqa: E402


def test_gap_completed_bars_ties_latest():
    l = np.array([10.0] * 40)
    l[37] = 9.0
    assert W.gap_to_low(l, 39) == 2 and W.gap_to_low(l, 38) == 1
    l[35] = 9.0                                          # tie: latest wins
    assert W.gap_to_low(l, 39) == 2
    l2 = np.array([10.0] * 40); l2[30] = 8.0
    assert W.gap_to_low(l2, 39) == 9
    l2[38] = np.nan
    assert W.gap_to_low(l2, 39) == 9                     # missing bar skipped


def test_triangle_is_the_imported_function():
    assert W.os_leave_flag is os_leave_flag
    fast = np.array([-90.0, -90.0, -70.0]); slow = np.array([-88.0, -88.0, -85.0])
    assert os_leave_flag(fast, slow, 2)                  # left the oversold triangle, fast rising
