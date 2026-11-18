import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import wr_thrust_gate as W  # noqa: E402


def _rows():
    out = []
    for d in range(10):
        day = "2026-05-%02d" % (d + 1)
        out += [{"day": day, "net": 4.0 + d % 3, "d5": 30.0} for _ in range(60)]
        out += [{"day": day, "net": -6.0 - d % 2, "d5": 5.0} for _ in range(60)]
    return out


def test_split_stats_lift_and_halves():
    st = W.split_stats(_rows(), 25.0)
    assert st["kept_n"] == 600 and st["skip_n"] == 600
    assert st["lift"] > 9 and st["half1_lift"] > 0 and st["half2_lift"] > 0 and st["t_day"] > 2
    assert W.verdict(st)[0] == "PASS"


def test_threshold_is_inclusive_and_missing_dropped():
    rows = [{"day": "d", "net": 1.0, "d5": 25.0}, {"day": "d", "net": -1.0, "d5": 24.99},
            {"day": "d", "net": 9.0, "d5": None}]
    st = W.split_stats(rows, 25.0)
    assert st["n"] == 2 and st["kept_n"] == 1 and st["kept_mean"] == 1.0


def test_verdict_fails_on_one_bad_half():
    st = {"kept_n": 900, "lift": 3.0, "t_day": 2.5, "half1_lift": 4.0, "half2_lift": -0.1}
    assert W.verdict(st)[0] == "FAIL"
    assert W.verdict({"kept_n": 10})[0] == "UNDERPOWERED"
