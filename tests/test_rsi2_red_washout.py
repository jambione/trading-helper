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


def _day(n=390):
    d = {"o": [np.full(n, 30.0)], "h": [np.full(n, 30.05)], "l": [np.full(n, 29.95)], "c": [np.full(n, 30.0)], "pc": [30.0],
         "red_pine": [np.zeros(n, bool)], "U_pine": [np.full(n, 0.01)], "D_pine": [np.full(n, 0.01)],
         "m4_pine": [np.full(n, 30.0)], "m199_pine": [np.full(n, 40.0)]}
    return d


def test_b1_triangle_window_and_entry():
    d = _day()
    s = 50
    d["l"][0][s - 2] = 29.0                                    # 15-minute low 2 bars before s
    d["red_pine"][0][s] = True
    for tri_at, fires in ((s, True), (s + 1, True), (s + 2, True), (s + 3, False)):
        fast = np.full(400, -50.0); slow = np.full(400, -50.0)
        fast[tri_at - 1] = slow[tri_at - 1] = -90.0            # in the oversold triangle the bar before
        fast[tri_at] = -60.0; slow[tri_at] = -85.0             # leaves it, fast rising
        sig = [x for x in W.signals(d, 0, fast, slow, 0) if x["rule"] == "B1"]
        assert bool(sig) == fires
        if fires:
            assert sig[0]["ent"] == tri_at + 1 and sig[0]["entry"] == d["o"][0][tri_at + 1]


def test_a_signal_uses_nothing_after_t():
    d = _day()
    t = 60
    d["l"][0][t - 1] = 29.0
    d["U_pine"][0][t - 1] = 0.0; d["D_pine"][0][t - 1] = 0.01   # trigger P = c + 0.09 + tick
    d["h"][0][t] = 31.0
    fast = slow = np.full(400, -50.0)
    a1 = [(x["t"], x["gap"], x["entry"]) for x in W.signals(d, 0, fast, slow, 0) if x["rule"] == "A" and x["t"] == t]
    for arr in ("h", "l", "c", "o"):
        d[arr][0][t + 1:] = 99.0 if arr != "l" else 1.0
    a2 = [(x["t"], x["gap"], x["entry"]) for x in W.signals(d, 0, fast, slow, 0) if x["rule"] == "A" and x["t"] == t]
    assert a1 and a1 == a2
