"""Runner exit mechanics. No market data."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import right_tail_runner as R  # noqa: E402


def _flat(n=30, start=600, px=100.0):
    return {start + i: (px, px, px, px) for i in range(n)}


def test_hard_stop_and_gap():
    bars = _flat()
    bars[603] = (99.0, 99.0, 97.5, 98.0)                    # low through the 2% stop at 98.00
    assert R.runner_exit(bars, 600, 100.0) == (98.0, 603, "stop")
    bars[603] = (97.0, 97.0, 96.0, 96.5)                    # opens below the stop: fills at the open
    assert R.runner_exit(bars, 600, 100.0)[0] == 97.0


def test_trail_arms_after_one_percent_and_eod():
    bars = _flat()
    bars[602] = (100.0, 103.0, 100.0, 102.5)                # high 103 arms the trail: stop 101.97
    bars[603] = bars[604] = (102.5, 102.6, 102.2, 102.4)    # holds above the trail
    bars[605] = (102.0, 102.0, 101.5, 101.8)                # low 101.5 hits it
    px, m, why = R.runner_exit(bars, 600, 100.0, worse=False)
    assert abs(px - 101.97) < 1e-9 and m == 605 and why == "trail"
    late = {949 + i: (100.0, 100.0, 100.0, 100.5) for i in range(3)}
    assert R.runner_exit(late, 948, 100.0) == (100.5, 949, "eod")   # 15:49 close is the latest exit


def test_tail_stats():
    t = R.tail_stats([0.06, 0.03, 0.0, -0.01] + [0.0] * 16)
    assert t["share_ge_2pct"] == 0.1 and t["share_ge_5pct"] == 0.05 and abs(t["max_bp"] - 600) < 1e-9


def test_entry_bar_low_and_worse_ordering():
    bars = _flat()
    bars[600] = (100.0, 100.5, 97.9, 100.0)                 # the entry bar itself trades through the 2% stop
    assert R.runner_exit(bars, 600, 100.0) == (98.0, 600, "stop")
    bars = _flat()
    bars[602] = (100.0, 103.0, 101.5, 102.0)                # new high 103 arms 101.97 and the same bar's low 101.5
    assert R.runner_exit(bars, 600, 100.0)[2] == "trail"                    # graded: worse ordering exits
    assert R.runner_exit(bars, 600, 100.0, worse=False)[1] != 602           # information ordering keeps it open
    assert R.max_concurrent([(600, 610), (605, 620), (611, 612)]) == 2


def test_stop_raised_by_the_bar_fills_at_the_new_stop():
    bars = _flat()
    bars[601] = (101.8, 103.0, 101.5, 102.0)                # opens 101.8, high 103 raises the trail to 101.97, low 101.5
    px, m, why = R.runner_exit(bars, 600, 100.0)
    assert abs(px - 101.97) < 1e-9 and m == 601 and why == "trail"
