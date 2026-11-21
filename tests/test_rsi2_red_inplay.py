import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import rsi2_red_inplay as I  # noqa: E402


def test_mean_range_completed_bars_only():
    n = 40
    c = np.full(n, 100.0); h = np.full(n, 100.5); l = np.full(n, 99.5)
    h[20] = 105.0                                       # a big bar at minute 20
    mr = I.mean_range(h, l, c)
    assert abs(mr[20] - 0.01) < 1e-12                   # minute 20's own range is not used at t = 20
    assert mr[21] > 0.01                                # it enters at t = 21


def test_inplay_mask_is_per_minute_cross_section():
    MR = np.tile(np.arange(30, dtype=float).reshape(30, 1), (1, 3))
    MR[:, 1] = MR[::-1, 1]                              # minute 1 reverses the ranking
    m = I.inplay_masks(MR, 0.9)
    assert m[29, 0] and not m[0, 0] and m[0, 1] and not m[29, 1]
