"""Clock-window mechanics. No market data."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import clock_windows as C  # noqa: E402


def _bars(lo, hi, px=10.0):
    return {m: (px, px * 1.001) for m in range(lo, hi)}


def test_return_needs_both_bars():
    b = {600: (10.0, 10.0), 604: (10.0, 10.2)}
    assert abs(C.ret(b, 600, 5) - 0.02) < 1e-12
    assert C.ret(b, 601, 5) is None


def test_control_is_near_the_window_but_outside_it():
    lo, hi = C.WINDOWS["w0830"]
    b = _bars(lo - 40, hi + 40)
    for i in range(50):
        c = C.control_minute(b, "2026-10-15", "AAA", f"x{i}", lo, hi)
        assert c is not None and not lo <= c < hi and lo - C.SPAN <= c < lo + C.SPAN
    assert C.control_minute(b, "d", "AAA", "k", lo, hi) == C.control_minute(b, "d", "AAA", "k", lo, hi)


def test_power_control_is_early_afternoon():
    b = _bars(13 * 60, 16 * 60)
    c = C.power_control(b, "d", "AAA")
    assert C.POWER_CTL[0] <= c < C.POWER_CTL[1]


def test_verdict_direction_power_and_halves():
    assert C.verdict("w0800", 3.0, [1.0, 2.0], 100, 10) == "PASS"
    assert C.verdict("w0800", 3.0, [1.0, -2.0], 100, 10) == "FAIL"
    assert C.verdict("w0800", 3.0, [1.0, 2.0], 20, 10) == "UNDERPOWERED"
    assert C.verdict("w0930_avoid", -3.0, [-1.0, -2.0], 100, 10) == "PASS"   # predicted worse
    assert C.verdict("w0930_avoid", 3.0, [1.0, 2.0], 100, 10) == "FAIL"
