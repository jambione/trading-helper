"""prior_signal: groups use only earlier signals whose 10-minute outcome is complete."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "studies"))
import prior_signal as ps  # noqa: E402


def test_classify_groups_and_no_lookahead():
    sigs = [{"t": 20, "net": 5.0, "base": False, "down": 0.0},
            {"t": 55, "net": -8.0, "base": True, "down": 1.0},
            {"t": 60, "net": 3.0, "base": True, "down": 0.0},     # t=55 not complete yet (55 + 10 >= 60)
            {"t": 100, "net": 1.0, "base": True, "down": 0.0}]
    out = ps.classify(sigs)
    assert [r["group"] for r in out] == ["WIN", "WIN", "WIN"]          # 55: prev 20 won; 60: last complete is 20; 100: last is 60 (+3)
    sigs[0]["net"] = -1.0
    out = ps.classify(sigs)
    assert [r["group"] for r in out] == ["LOSS", "LOSS", "WIN"]
    assert ps.classify([{"t": 30, "net": 1.0, "base": True, "down": 0.0}])[0]["group"] == "NONE"
