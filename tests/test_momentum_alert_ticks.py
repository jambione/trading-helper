"""Tick-scalp mechanics. No market data."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import momentum_alert_ticks as T  # noqa: E402


def test_entry_pays_the_ask_and_exit_takes_the_bid():
    qts = [100.0, 104.0, 130.0]
    quotes = [(9.90, 10.00), (9.95, 10.05), (10.20, 10.30)]
    trades = [(106.0, 10.05), (120.0, 10.20), (129.0, 10.25)]
    got = T.scalp(trades, qts, quotes, t_alert=100.0)            # entry at 105 -> ask 10.05
    assert abs(got["hold30"] - (10.20 / 10.05 - 1)) < 1e-12        # exit at 135 -> bid of the 130 quote
    assert got["hold60"] is None                                   # exit at 165: quote 35 s old, no price
    assert got["spread_bp"] > 0


def test_rules_exit_when_they_should():
    tr = [(1.0, 100.5), (2.0, 101.0), (3.0, 100.4)]               # high 101, then -0.59%
    assert T.exit_time("trail05", 0.0, 100.0, tr) == 3.0
    tr2 = [(1.0, 100.2), (20.0, 100.1)]                          # no new high for 19 s
    assert T.exit_time("leash15", 0.0, 100.0, tr2) == 20.0
    assert T.exit_time("leash15", 0.0, 100.0, [(5.0, 98.9)]) == 5.0     # 1% hard stop
    assert T.exit_time("hold60", 0.0, 100.0, tr) == 60.0
    assert T.exit_time("trail05", 0.0, 100.0, []) == 180.0


def test_stale_quote_means_no_entry():
    assert T.scalp([], [10.0], [(9.9, 10.0)], t_alert=100.0) is None   # quote 95 s old at entry


def test_clock_buckets():
    assert T.clock_bucket(4 * 60 + 10) == "t04"
    assert T.clock_bucket(8 * 60 + 30) == "t0830"
    assert T.clock_bucket(9 * 60 + 31) == "t0930-34"
    assert T.clock_bucket(15 * 60 + 30) == "t15-power"
