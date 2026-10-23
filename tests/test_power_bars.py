import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import etf_bars as B  # noqa: E402


def test_power_bar_rules():
    assert B.power_bar((100, 101.0, 99.9, 100.95, 600), 0.5, 1)          # range 1.1 >= 1.0, body 0.95, top 20%
    assert not B.power_bar((100, 101.0, 99.9, 100.95, 600), 0.6, 1)      # range < 2 x ref
    assert not B.power_bar((100, 101.0, 99.9, 100.4, 600), 0.5, 1)       # body too small / not top 20%
    assert B.power_bar((101, 101.1, 100.0, 100.05, 600), 0.5, -1)
    assert not B.power_bar((100, 101.0, 99.9, 100.95, 600), None, 1)


def test_size_ref_needs_five_and_resets():
    s = B.SizeRef()
    for i in range(4):
        s.add([(0, 1.0 + i, 0.0, 0.5, 571)])
    assert s.ref(0) is None
    s.add([(0, 3.0, 0.0, 0.5, 571)])
    assert s.ref(0) == 2.0
    s.reset()
    assert s.ref(0) is None
