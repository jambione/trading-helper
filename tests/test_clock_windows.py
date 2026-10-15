"""Clock-window mechanics. No market data."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import clock_windows as C  # noqa: E402


def _bars(lo, hi, px=10.0):
    return {m: (px, px * 1.001) for m in range(lo, hi)}


def test_return_forward_fills_the_exit_inside_the_hold_only():
    b = {600: (10.0, 10.0), 602: (10.0, 10.2)}
    assert abs(C.ret(b, 600, 5) - 0.02) < 1e-12           # exit minute 604 missing: last close at/before it
    assert C.ret(b, 601, 5) is None                        # no entry print
    assert C.ret({600: (10.0, 10.0)}, 600, 5) == 0.0       # the entry minute's own close counts


def test_control_is_after_the_first_alert_near_the_window_and_outside_every_window():
    lo, hi = C.WINDOWS["w0830"]
    b = _bars(lo - 40, hi + 40)
    fa = lo - 10
    for i in range(50):
        c = C.control_minute(b, "2026-10-15", "AAA", f"x{i}", lo, fa)
        assert c is not None and c > fa and c not in C.IN_ANY_WINDOW and lo - C.SPAN <= c < lo + C.SPAN


def test_controls_stay_on_their_side_of_the_open():
    assert all(m < C.OPEN for m in C.control_span("w0900", C.WINDOWS["w0900"][0]))
    assert all(m >= C.OPEN + 5 for m in C.control_span("w1000", C.WINDOWS["w1000"][0]))
    assert list(C.control_span("w0930_avoid", C.OPEN)) == list(range(C.OPEN + 5, C.OPEN + 35))


def test_power_picks_are_after_the_alert_and_in_their_spans():
    b = _bars(13 * 60, 16 * 60)
    pw = C.power_minutes(b, "d", "AAA", 14 * 60, C.POWER, "power")
    pc = C.power_minutes(b, "d", "AAA", 14 * 60, C.POWER_CTL, "power_ctl")
    assert len(pw) == C.POWER_PICKS and all(C.POWER[0] <= m < C.POWER[1] for m in pw)
    assert all(14 * 60 < m < C.POWER_CTL[1] for m in pc)


def test_window_entry_is_the_first_printing_minute():
    lo, hi = C.WINDOWS["w0800"]
    b = {lo + 2: (10.0, 10.0), lo + 3: (10.0, 10.1)}
    assert C.window_entry(b, lo, hi) == lo + 2


def test_verdict_direction_power_and_halves():
    assert C.verdict("w0800", 3.1, [1.0, 2.0], 100, 10) == "PASS"
    assert C.verdict("w0800", 2.9, [1.0, 2.0], 100, 10) == "FAIL"      # family-wise bar 3.0
    assert C.verdict("w0800", 3.1, [1.0, -2.0], 100, 10) == "FAIL"
    assert C.verdict("w0800", 3.1, [1.0, 2.0], 20, 10) == "UNDERPOWERED"
    assert C.verdict("w0930_avoid", -3.1, [-1.0, -2.0], 100, 10) == "PASS"
    assert C.verdict("w0930_avoid", 3.1, [1.0, 2.0], 100, 10) == "FAIL"
