"""one_two_three: the 1-2-3 detector (higher low, break of point 1's high, cancels) and the simulated exit."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import one_two_three as ot  # noqa: E402

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 20, 10, 30, tzinfo=ET).timestamp()


def bar(i, o, h, l, c):  # noqa: E741
    return (T0 + 60 * i, o, h, l, c, 1000.0)


def base():
    # 20 bars of leg 1 before t0: from 10.00 rising to 10.40 (L0 = 9.99)
    return [bar(i - 20, 10.0 + 0.02 * (i), 10.0 + 0.02 * i + 0.01, 10.0 + 0.02 * i - 0.01, 10.0 + 0.02 * i) for i in range(20)]


def test_higher_low_then_break_of_point_one_is_the_open():
    b = base() + [bar(0, 10.40, 10.50, 10.38, 10.48),   # run to H = 10.50
                  bar(1, 10.48, 10.49, 10.30, 10.32),   # pullback confirmed: close 10.32 <= 10.50 - max(30% of leg, 10 bp)
                  bar(2, 10.32, 10.36, 10.25, 10.34),   # point 2 low 10.25 > L0 9.99
                  bar(3, 10.34, 10.55, 10.33, 10.53)]   # breaks 10.51 -> point 3
    s = ot.find_123(b, T0)
    assert s and abs(s["H1"] - 10.50) < 1e-9 and abs(s["fill_raw"] - 10.51) < 1e-9 and s["bar_idx"] == len(b) - 1


def test_breaking_l0_cancels_even_on_the_trigger_bar():
    b = base() + [bar(0, 10.40, 10.50, 10.38, 10.48), bar(1, 10.48, 10.49, 10.30, 10.32), bar(2, 10.32, 10.60, 9.90, 10.55)]
    assert ot.find_123(b, T0) is None


def test_no_pullback_no_trade():
    b = base() + [bar(i, 10.40 + 0.02 * i, 10.42 + 0.02 * i, 10.39 + 0.02 * i, 10.41 + 0.02 * i) for i in range(10)]
    assert ot.find_123(b, T0) is None


def test_exit_stop_and_trail():
    entry = 10.0
    b = [bar(0, 10.0, 10.02, 9.99, 10.01), bar(1, 10.01, 10.30, 10.0, 10.28), bar(2, 10.28, 10.29, 10.20, 10.22)]
    px, ts, why = ot.simulate_exit(b, entry, 0, "X", {})
    # trail after bar 1 = 10.30 * (1 - 0.006) = 10.2382; bar 2 low 10.20 hits it; exit 10.2382 less 2 bp
    assert why == "stop" and abs(px - 10.30 * 0.994 * (1 - 2e-4)) < 1e-9 and ts == b[2][0]
    gap = [bar(0, 10.0, 10.02, 9.99, 10.01), bar(1, 9.80, 9.85, 9.70, 9.75)]
    px2, _, why2 = ot.simulate_exit(gap, entry, 0, "X", {})
    assert why2 == "stop" and abs(px2 - 9.80 * (1 - 2e-4)) < 1e-9          # gapped below the stop: fills at the open
