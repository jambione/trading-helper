"""Unit tests for flow_monitor tape helper (no network)."""
import pytest

from flow_monitor.tape import TapeWindow


def test_tape_quote_rule_buy_at_ask():
    t = TapeWindow(60.0)
    t.side_print(10.05, 100, bid=10.00, ask=10.05, ts=1000.0)
    t.side_print(10.00, 50, bid=10.00, ask=10.05, ts=1001.0)
    snap = t.snapshot(now=1002.0)
    assert snap["buy"] == 100
    assert snap["sell"] == 50
    assert snap["sided_n"] == 2
    assert snap["dom"] == pytest.approx((100 - 50) / 150)


def test_tape_tick_rule_inside_spread():
    t = TapeWindow(60.0)
    t.side_print(10.02, 10, bid=10.00, ask=10.05, ts=1.0)  # inside
    t.side_print(10.05, 20, bid=10.00, ask=10.05, ts=2.0)
    t.side_print(10.03, 30, bid=10.00, ask=10.05, ts=3.0)  # downtick -> S
    snap = t.snapshot(now=4.0)
    assert snap["n"] == 3
    assert snap["sell"] >= 30


def test_tape_window_trims():
    t = TapeWindow(10.0)
    t.side_print(1.0, 100, bid=0.9, ask=1.1, ts=100.0)
    t.side_print(1.1, 100, bid=0.9, ask=1.1, ts=200.0)
    snap = t.snapshot(now=200.0)
    # first print is 100s old > window 10s
    assert snap["n"] == 1
