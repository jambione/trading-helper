"""Hold check, entry timing and verdict rules of the wait-and-hold study. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import wait_held_study as W  # noqa: E402


def _bars(rows, start=600):
    return {start + i: r for i, r in enumerate(rows)}


def test_held_reads_only_the_wait_window():
    lows = {601: 99.95, 602: 99.91, 606: 98.0}            # ref 100, k 10 bp -> floor 99.90
    assert W.held(lows, 600, 100.0, 5, 10)                  # 606 is after m+5: still held
    lows[603] = 99.90
    assert not W.held(lows, 600, 100.0, 5, 10)              # touching the floor is a dip
    assert W.held({}, 600, 100.0, 5, 10)                    # no prints = no visible dip


def test_entry_is_the_open_after_the_wait_with_a_10_minute_hold():
    bars = _bars([(100, 100, 100, 100.0)] * 30)
    bars[606] = (101, 101, 101, 101.0)                      # m=600, W=5 -> entry at 606's open
    bars[615] = (102, 102, 102, 102.0)                      # exit at 606 + 9
    assert abs(W.entry_trade(bars, 606) - (102 / 101 - 1)) < 1e-12
    assert W.entry_trade(bars, 700) is None                 # no entry bar
    assert W.entry_trade({955 + i: (1, 1, 1, 1.0) for i in range(10)}, 955) is None   # exit past 16:00


def test_score_separates_filter_from_delay():
    def sig(day, low_dip, gain):
        bars = _bars([(100, 100, 100, 100.0)] * 30)
        bars[615] = (100, 100, 100, 100.0 * (1 + gain))
        iex_low = {602: 99.5} if low_dip else {}
        return {"day": day, "sym": "X", "m": 600, "ref": 100.0, "bars": bars, "iex_low": iex_low,
                "sip_low": iex_low, "iex_close": {}}
    sigs = []
    for i in range(12):
        d = f"2026-10-{15 + i:02d}"
        sigs += [sig(d, False, 0.004 + 0.0001 * i)] * 6 + [sig(d, True, -0.002)] * 4
    x = W.score(sigs, 5, 10)
    assert x["held"] == 72 and x["signals"] == 120 and abs(x["keep"] - 0.6) < 1e-9
    assert x["lift"] > 0 and x["held_net"] > 0              # held trades beat entering every signal late
    assert x["verdict"] == "PASS"


def test_verdict_and_kill():
    assert W.half_verdict(0.001, 2.8, 0.0006, 2.8, 0.0, 60, 10) == "PASS"
    assert W.half_verdict(0.001, 1.9, 0.0006, 2.1, 0.0, 60, 10) == "UNDERPOWERED"   # points pass, t short
    assert W.half_verdict(-0.001, 2.5, 0.0006, 2.1, 0.0, 60, 10) == "FAIL"
    assert W.half_verdict(0.001, 2.8, 0.0004, 2.8, 0.0, 60, 10) == "FAIL"     # lift under 5 bp
    assert W.half_verdict(0.001, 2.8, 0.0006, 2.8, -0.0001, 60, 10) == "FAIL"  # worse than today's entry
    assert W.half_verdict(0.001, 2.1, 0.0006, 2.1, 0.0, 59, 10) == "UNDERPOWERED"
    assert W.kill_check({"A": {"lift": -0.0001}, "B": {"lift": 0.0}})
    assert not W.kill_check({"A": {"lift": -0.0001}, "B": {"lift": 0.0001}})


def test_extension_only_after_an_underpowered_first_read():
    days = [f"d{i:02d}" for i in range(25)]
    assert W.read_sessions(days[:9], lambda ds: "PASS") is None
    assert W.read_sessions(days, lambda ds: "FAIL") == days[:10]
    assert W.read_sessions(days, lambda ds: "PASS") == days[:10]
    assert W.read_sessions(days, lambda ds: "UNDERPOWERED") == days[:20]
    assert W.read_sessions(days[:15], lambda ds: "UNDERPOWERED") == days[:10]


def test_lag_and_full_coverage():
    bars = _bars([(100, 100, 100, 100.0)] * 30)
    bars[607] = (100, 100, 100, 100.0)
    s = {"day": "2026-10-15", "sym": "X", "m": 600, "ref": 100.0, "bars": bars, "iex_low": {601: 100.0},
         "sip_low": {}, "iex_close": {}}
    assert W.score([s], 5, 10, full_only=True)["skips"] == {"iex_gap_in_wait": 1}
    assert W.score([s], 5, 10, lag=1)["held"] == 1
