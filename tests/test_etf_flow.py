import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import etf_flow as F  # noqa: E402


def test_tick_sign_carries_zero_ticks():
    assert F.tick_sign([10, 10.01, 10.01, 10.0, 10.0, 10.02]) == [0, 1, 1, -1, -1, 1]


def test_imbalance():
    tr = [(0, 10.0, 100), (1, 10.01, 300), (2, 10.01, 100), (3, 10.0, 200)]
    assert abs(F.imbalance(tr) - (300 + 100 - 200) / 600) < 1e-12
    assert F.imbalance([(0, 10.0, 5)]) is None


def test_ret_and_decision_ts():
    o = np.full(390, 100.0); c = np.full(390, 100.0); c[32] = 100.1
    assert abs(F.ret_bp(o, c, 30, 2) - 10.0) < 1e-9
    assert F.ret_bp(o, c, 389, 2) is None
    t = F.decision_ts("2026-11-18", 30)
    from datetime import datetime
    assert datetime.fromtimestamp(t, F.ET).strftime("%H:%M") == "10:00"


def test_verdict_bars():
    g = {"n": 1500, "mean": 1.4, "t_day": 2.3}
    assert F.verdict(g, {"n": 3000, "mean": 0.1}, {"n": 1500, "mean": 0.5})[0] == "PASS"
    assert F.verdict(g, {"n": 3000, "mean": 0.6}, {"n": 1500, "mean": 0.5})[0] == "FAIL"     # lift 0.8
    assert F.verdict(g, {"n": 3000, "mean": 0.1}, {"n": 1500, "mean": -0.1})[0] == "FAIL"    # train disagrees
    assert F.verdict({"n": 10}, {}, {})[0] == "UNDERPOWERED"
