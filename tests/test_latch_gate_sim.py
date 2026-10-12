"""State machine for the %R latch. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

from latch_gate_sim import (  # noqa: E402
    FLATTEN_MIN, Bar, _pair, control_indexes, half_verdict, walk,
)


def _bar(minute, **kw) -> Bar:
    base = dict(
        minute=minute, open=10.0, close=10.0,
        fast_up=False, slow_up=False, fast_dn=False, slow_dn=False,
        rsi=40.0, rsi_up=False, price_ok=True,
    )
    base.update(kw)
    return Bar(**base)


def _session(flags: list[dict], start: int = 9 * 60 + 40) -> list[Bar]:
    return [_bar(start + i, **f) for i, f in enumerate(flags)]


def test_shut_gate_ignores_an_rsi_low():
    bars = _session([
        {"fast_dn": True, "slow_dn": True, "rsi": 20, "rsi_up": True},
        {"fast_dn": True, "slow_dn": True, "rsi": 25, "rsi_up": True},
        {"rsi": 30, "rsi_up": True},
    ])
    assert walk(bars) == []


def test_both_rising_opens_and_the_rsi_low_fires_once():
    flags = [
        {"fast_up": True, "slow_up": True, "rsi": 30, "rsi_up": True, "close": 10},
        {"fast_up": True, "slow_up": True, "rsi": 35, "rsi_up": True, "open": 11, "close": 11},
        {"fast_up": True, "slow_up": True, "rsi": 80, "rsi_up": True, "open": 12, "close": 12},
        {"open": 13, "close": 13, "rsi": 80},
    ]
    trades = walk(_session(flags))
    assert len(trades) == 1
    assert trades[0]["entry"] == 11
    assert trades[0]["exit"] == 13
    assert trades[0]["reason"] == "rsi70"
    assert trades[0]["latch_only"] is False


def test_one_line_turning_down_does_not_shut_the_gate():
    flags = [
        {"fast_up": True, "slow_up": True, "rsi": 60, "rsi_up": False},
        {"fast_dn": True, "slow_up": True, "rsi": 28, "rsi_up": True},
        {"open": 11, "rsi": 40},
        {"rsi": 75},
        {"open": 12},
    ]
    trades = walk(_session(flags))
    assert len(trades) == 1
    assert trades[0]["latch_only"] is True
    assert trades[0]["entry"] == 11


def test_a_later_dip_fires_again_after_the_rsi_sell():
    flags = [
        {"fast_up": True, "slow_up": True, "rsi": 30, "rsi_up": True},
        {"open": 11, "rsi": 40},
        {"rsi": 75},
        {"open": 12, "rsi": 60},
        {"rsi": 22, "rsi_up": True},
        {"open": 13, "rsi": 30},
        {"rsi": 80},
        {"open": 14},
    ]
    trades = walk(_session(flags))
    assert len(trades) == 2
    assert [t["entry"] for t in trades] == [11, 13]
    assert all(t["reason"] == "rsi70" for t in trades)


def test_a_shut_gate_does_not_flatten_an_open_trade():
    flags = [
        {"fast_up": True, "slow_up": True, "rsi": 30, "rsi_up": True},
        {"open": 11, "fast_dn": True, "slow_dn": True, "rsi": 35},
        {"fast_dn": True, "slow_dn": True, "rsi": 40},
        {"rsi": 72},
        {"open": 9},
    ]
    trades = walk(_session(flags))
    assert len(trades) == 1
    assert trades[0]["exit"] == 9
    assert trades[0]["gate_shut_while_in"] is True


def test_conjunction_streak_is_one_fire():
    flags = [
        {"fast_up": True, "slow_up": True, "rsi": 30, "rsi_up": True},
        {"open": 11, "fast_up": True, "slow_up": True, "rsi": 32, "rsi_up": True},
        {"fast_up": True, "slow_up": True, "rsi": 34, "rsi_up": True},
        {"rsi": 80},
        {"open": 12},
    ]
    assert len(walk(_session(flags))) == 1


def test_under_ten_dollars_and_the_window_do_not_fire():
    cheap = _session([
        {"fast_up": True, "slow_up": True, "rsi": 20, "rsi_up": True, "price_ok": False, "close": 4},
        {"open": 4},
    ])
    late = _session([
        {"fast_up": True, "slow_up": True, "rsi": 20, "rsi_up": True},
        {"open": 11},
    ], start=15 * 60 + 31)
    early = _session([
        {"fast_up": True, "slow_up": True, "rsi": 20, "rsi_up": True},
        {"open": 11},
    ], start=8 * 60)
    assert walk(cheap) == []
    assert walk(late) == []
    assert walk(early) == []


def test_still_open_at_the_flatten_sells_at_that_close():
    flags = [
        {"fast_up": True, "slow_up": True, "rsi": 30, "rsi_up": True},
        {"open": 11, "rsi": 40},
    ]
    # 09:40 signal, 09:41 fill, then a 15:50 bar.
    bars = _session(flags) + [_bar(FLATTEN_MIN, open=12, close=12.5, rsi=40)]
    trades = walk(bars)
    assert len(trades) == 1
    assert trades[0]["reason"] == "session_flatten"
    assert trades[0]["exit"] == 12.5


def test_a_premarket_bar_is_not_a_control_for_a_regular_hour_fill():
    # 09:05 has a print five minutes later, and it shares the 09:00 hour with
    # a 09:45 fill. It is still ineligible. 09:40 is the control that remains.
    minutes = [9 * 60 + 5, 9 * 60 + 10, 9 * 60 + 40, 9 * 60 + 45, 9 * 60 + 50]
    times = [0, 5 * 60, 35 * 60, 40 * 60, 45 * 60]
    assert control_indexes(times, minutes, 3, 4) == [2]


def test_a_missing_interior_minute_still_scores():
    # Fill 10:00, exit 10:10, and 10:01 is absent. The prints exist, and
    # 10:05 has a bar exactly ten minutes later, so the trade scores.
    minutes = [10 * 60, 10 * 60 + 2, 10 * 60 + 5, 10 * 60 + 10, 10 * 60 + 15]
    times = [0, 2 * 60, 5 * 60, 10 * 60, 15 * 60]
    trade = {
        "fill_i": 0, "exit_i": 3, "reason": "rsi70", "gross": 0.01,
        "sym": "AAA", "day": "2026-09-04",
        "ctrl_open": [10.0, 10.0, 10.0, 11.0, 10.0],
        "ctrl_close": [10.0, 10.0, 10.0, 11.0, 12.0],
    }
    paired = _pair(times, minutes, trade)
    assert paired is not None
    lift, net = paired
    assert abs(net - (0.01 - 0.002)) < 1e-12
    assert abs(lift - (0.01 - 0.0)) < 1e-12


def test_a_tape_that_ends_before_1550_is_truncated():
    flags = [
        {"fast_up": True, "slow_up": True, "rsi": 30, "rsi_up": True},
        {"open": 11, "rsi": 40},
    ]
    trades = walk(_session(flags))
    assert len(trades) == 1
    assert trades[0]["reason"] == "truncated"


def test_verdict_needs_lift_net_and_sample():
    assert half_verdict(0.001, 2.5, 0.0001, 100, 8) == "PASS"
    assert half_verdict(0.001, 2.5, -0.0001, 100, 8) == "FAIL"
    assert half_verdict(0.0001, 3.0, 0.001, 100, 8) == "FAIL"
    assert half_verdict(0.001, 1.9, 0.001, 100, 8) == "FAIL"
    assert half_verdict(0.001, 2.5, 0.001, 99, 8) == "UNDERPOWERED"
    assert half_verdict(0.001, 2.5, 0.001, 100, 7) == "UNDERPOWERED"


def test_no_second_entry_while_the_first_is_open():
    flags = [
        {"fast_up": True, "slow_up": True, "rsi": 30, "rsi_up": True},
        {"open": 11, "rsi": 55},
        {"rsi": 20, "rsi_up": True},
        {"open": 12, "rsi": 25},
        {"rsi": 80},
        {"open": 13},
    ]
    trades = walk(_session(flags))
    assert len(trades) == 1
    assert trades[0]["entry"] == 11
