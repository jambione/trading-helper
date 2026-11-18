import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import scout_trade as S  # noqa: E402


def test_label_sequence_uses_first_and_previous():
    out = S.label_sequence([(20, 14.0), (40, -56.0), (60, 14.0)])
    assert [r["k"] for r in out] == [1, 2]
    assert all(r["first_win"] for r in out)
    assert out[0]["prev_win"] and not out[1]["prev_win"]
    assert S.label_sequence([]) == [] and S.label_sequence([(20, 5.0)]) == []


def test_compare_and_verdict():
    rows = []
    for d in range(10):
        day = "2026-05-%02d" % (d + 1)
        rows += [{"day": day, "net": 5.0 + d % 2, "first_win": True} for _ in range(60)]
        rows += [{"day": day, "net": -5.0 - d % 3, "first_win": False} for _ in range(60)]
    st = S.compare(rows, "first_win")
    assert st["lift"] > 9 and st["t_day"] > 2 and st["half1_lift"] > 0 and st["half2_lift"] > 0
    assert S.verdict(st)[0] == "PASS"
    assert S.verdict({"win_n": 3})[0] == "UNDERPOWERED"
