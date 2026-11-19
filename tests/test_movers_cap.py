import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import movers_cap as M  # noqa: E402


def test_ten_min_gross_and_paced_rvol():
    o = np.full(40, 100.0); c = np.full(40, 100.0); c[11] = 101.0
    assert abs(M.ten_min_gross(o, c, 1) - 100.0) < 1e-9
    assert M.ten_min_gross(o, c, 35) is None
    v = np.full(390, 10.0)
    assert abs(M.paced_rvol(v, 38, 3900.0) - 390 / 3900 / (39 / 390)) < 1e-9
    assert M.paced_rvol(v, 0, 3900.0) == 10.0 / (3900 * 0.05)
    assert M.paced_rvol(v, 5, 0) is None


def test_range_pos_and_refractory():
    h = np.array([10.0, 11, 12, 11, 10, 11]); l = h - 1; c = h - 0.5
    assert M.range_pos(h, l, c, 3) is None              # < 5 bars
    rp = M.range_pos(h, l, c, 5)
    assert abs(rp - (10.5 - 9) / (12 - 9) * 100) < 1e-9
    assert M.refractory([20, 30, 50, 79, 81]) == [20, 50, 81]


def test_judge():
    ref = {"n": 800, "mean": -6.0, "win": 45.0, "sd": 30.0}
    ok = {"n": 300, "mean": -6.5, "win": 43.0, "sd": 30.0}
    assert M.judge(ok, ref, rate=0.2)[0] == "PASS"
    assert M.judge({**ok, "mean": -7.5}, ref)[0] == "FAIL"
    assert M.judge({**ok, "win": 41.0}, ref)[0] == "FAIL"
    assert M.judge(ok, ref, rate=0.05)[0] == "FAIL"
    assert M.judge({**ok, "n": 100}, ref)[0] == "UNDERPOWERED"


def test_plan_sets_split_and_cap():
    sig = {"cap": [{"day": "d", "sym": "A", "t": 1}],
           "base": [{"day": "d", "sym": "S%d" % i, "t": 1} for i in range(900)],
           "range": [{"day": "d", "sym": "R%d" % i, "t": 1, "rpos": 95.0 if i % 2 else 50.0} for i in range(100)]}
    s = M.plan_sets(sig)
    assert len(s["base"]) == 800 and len(s["high"]) == 50 and len(s["low"]) == 50 and len(s["cap"]) == 1
