"""Fill and exit mechanics of the limit-below study. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import limit_below_study as L  # noqa: E402


def _bars(rows, start=600):
    """rows: list of (open, high, low, close) from minute `start`."""
    return {start + i: r for i, r in enumerate(rows)}


def test_fill_needs_trade_through_and_takes_the_better_open():
    bars = _bars([(100, 100, 100, 100.0),            # signal minute 600, close 100 -> limit 99.90 (10 bp)
                  (100, 100, 99.90, 99.95),           # touches 99.90 only: NOT a fill
                  (99.95, 99.96, 99.87, 99.90),       # 99.87 <= 99.90 * (1 - 0.0002) = 99.880 -> fill at 99.90
                  ] + [(100, 100, 100, 100.0)] * 15)
    assert L.limit_fill(bars, 600, 10, 5) == (602, 99.90)
    gap = _bars([(100, 100, 100, 100.0), (99.5, 99.6, 99.4, 99.5)] + [(100, 100, 100, 100.0)] * 15)
    assert L.limit_fill(gap, 600, 10, 5) == (601, 99.5)       # opens below the limit: fill at the open


def test_no_fill_outside_the_window():
    bars = _bars([(100, 100, 100, 100.0)] * 6 + [(99, 99, 99, 99.0)] + [(100, 100, 100, 100.0)] * 15)
    assert L.limit_fill(bars, 600, 10, 5) is None             # the dip comes at minute 606, after m+5


def test_exit_and_16_00_drop():
    bars = _bars([(100, 100, 100, 100.0)] * 20)
    bars[611] = (100, 100, 100, 101.0)
    assert L.exit_close(bars, 602) == 101.0                   # fill minute 602 + 9
    late = {955 + i: (100, 100, 100, 100.0) for i in range(6)}
    assert L.exit_close(late, 955) is None                    # exit bar 964 >= 16:00


def test_limit_trade_gross_and_market_reference():
    bars = _bars([(100, 100, 100, 100.0), (100, 100, 99.80, 99.85)] + [(100, 100, 100, 100.0)] * 15)
    bars[610] = (100, 100, 100, 100.4)
    lt = L.limit_trade(bars, 600, 10, 5)
    assert lt["filled"] and not lt["dropped"] and abs(lt["gross"] - (100.4 / 99.90 - 1)) < 1e-12
    assert abs(L.market_trade(bars, 600) - (100.4 / 100.0 - 1)) < 1e-12   # open of 601 .. close of 610


def test_half_verdict():
    assert L.half_verdict(0.0006, 2.1, 0.0001, 60, 10, fill_t=2.0) == "PASS"
    assert L.half_verdict(0.0006, 2.1, 0.0001, 60, 10, fill_t=1.9) == "FAIL"    # filled net needs t >= 2
    assert L.half_verdict(0.0006, 2.1, -0.0001, 60, 10, fill_t=2.5) == "FAIL"   # filled trades still lose
    assert L.half_verdict(0.0004, 3.0, 0.0001, 60, 10, fill_t=2.5) == "FAIL"
    assert L.half_verdict(0.0006, 2.1, 0.0001, 59, 10, fill_t=2.5) == "UNDERPOWERED"


def test_limit_uses_the_iex_reference_when_given():
    bars = _bars([(100, 100, 100, 100.0), (100, 100, 99.79, 99.8)] + [(100, 100, 100, 100.0)] * 15)
    assert L.limit_fill(bars, 600, 10, 5, ref=99.9) is None            # IEX ref 99.90 -> limit 99.80, needs <= 99.78; low 99.79 is not through
    assert L.limit_fill(bars, 600, 10, 5) == (601, 99.90)             # SIP ref 100 -> limit 99.90


def test_exit_carries_the_last_close_over_a_missing_bar():
    bars = _bars([(100, 100, 100, 100.0)] * 20)
    bars[611] = None
    del bars[611]
    bars[610] = (100, 100, 100, 100.7)
    assert L.exit_close(bars, 602) == 100.7
