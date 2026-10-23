import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import etf_bars as B  # noqa: E402
import fence_bar as F  # noqa: E402


def bar(o, h, lo, c, end):
    return (o, h, lo, c, end)


def seq():
    # fence 100-101; bar1 closes 101.5 (breakout); bar2 opens 101.4, dips to 100.9, closes 101.6 (retest)
    b = [bar(100.5, 101, 100, 100.8, 574), bar(100.8, 101.6, 100.7, 101.5, 579),
         bar(101.4, 101.7, 100.9, 101.6, 584)] + [bar(101.6, 101.8, 101.5, 101.7, 589 + 5 * i) for i in range(75)]
    return b


def test_breakout_then_retest_long():
    b = seq()
    sma = [100.0 + 0.01 * i for i in range(len(b))]
    sig = F.find_trade(b, sma, 1)
    assert sig["k"] == 2 and sig["sma_ok"] and sig["mid"] == 100.5


def test_cancel_and_sma_filter():
    b = seq()
    b[2] = bar(101.4, 101.5, 100.9, 100.95, 584)          # closes back inside: cancel, no retest
    b[3] = bar(100.95, 101.2, 100.9, 101.1, 589)          # new breakout close (arms)
    b[4] = bar(101.2, 101.5, 101.0, 101.4, 594)           # retest after the re-arm
    sma = [100.0 + 0.01 * i for i in range(len(b))]
    assert F.find_trade(b, sma, 1)["k"] == 4
    sma_down = [101.0 - 0.01 * i for i in range(len(b))]
    assert F.find_trade(b, sma_down, 1)["sma_ok"] is False


def test_window_ends_at_1059():
    b = seq()
    b[2] = bar(101.6, 101.8, 101.5, 101.7, 584)            # no retest early
    b[18] = bar(101.4, 101.7, 100.9, 101.6, 664)          # retest ending 11:04: too late
    assert F.find_trade(b, [100.0] * len(b), 1) is None


def test_simulate_stop_target_and_both():
    r = np.array([[600, 100, 100.5, 99.9, 100.2, 1], [601, 100.2, 102.1, 100.1, 101.9, 1],
                  [959, 101, 101, 101, 101, 1]], dtype=float)
    assert B.simulate(r, 0, 1, 99.0, 102.0)["why"] == "target"
    both = np.array([[600, 100, 102.5, 98.5, 100, 1], [959, 100, 100, 100, 100, 1]], dtype=float)
    assert B.simulate(both, 0, 1, 99.0, 102.0)["why"] == "stop"
    gap = np.array([[600, 100, 100.1, 99.9, 100, 1], [601, 98.0, 98.2, 97.9, 98, 1],
                    [959, 98, 98, 98, 98, 1]], dtype=float)
    s = B.simulate(gap, 0, 1, 99.0, 102.0)
    assert s["why"] == "stop" and abs(s["gross"] - (98.0 / 100 - 1)) < 1e-12


def test_early_close_detected_by_volume():
    full = np.array([[m, 100, 100, 100, 100, 1000] for m in range(570, 960)], dtype=float)
    assert B.is_full_session(full)
    half = np.array([[m, 100, 100, 100, 100, 1000 if m < 780 else 5] for m in range(570, 960)], dtype=float)
    assert not B.is_full_session(half)


def test_target_fills_on_open_beyond_and_scope():
    r = np.array([[600, 100, 100.1, 99.9, 100, 1], [601, 102.004, 102.008, 102.0, 102.0, 1],
                  [959, 102, 102, 102, 102, 1]], dtype=float)
    s = B.simulate(r, 0, 1, 99.0, 102.0)
    assert s["why"] == "target" and abs(s["gross"] - (102.004 / 100 - 1)) < 1e-12
    assert not B.in_scope("2026-02-02") and not B.in_scope("2026-10-16") and B.in_scope("2026-05-01")
