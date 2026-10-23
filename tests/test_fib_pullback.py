"""Swing / zone / reaction / bracket / control rules of fib_pullback on hand-built bars. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import fib_pullback as F  # noqa: E402


def b(m, o, h, lo, c, v=1000.0):
    return (m, o, h, lo, c, v)


def path(pull_low=101.4, confirm_close=101.9, after=103.2):
    """L = 100 at 09:35, H = 103 at 10:10 (R 3, GP zone 101.146..101.5, break 100.642), pullback, reaction, run."""
    bars = [b(570 + k, 100.5, 100.8, 100.2, 100.6) for k in range(5)]
    bars.append(b(575, 100.6, 100.7, 100.0, 100.4))                          # L = 100.0
    px = 100.4
    for k in range(576, 610):                                                 # climb to 103 by 10:10
        nx = 100.4 + (k - 575) * 2.6 / 34
        bars.append(b(k, px, min(nx + 0.02, 102.99), px - 0.02, nx))
        px = nx
    bars.append(b(610, px, 103.0, px - 0.02, 102.9))                          # H = 103.0 (10:10)
    bars.append(b(611, 102.9, 102.95, 102.2, 102.3))
    bars.append(b(612, 102.3, 102.35, 101.7, 101.8))
    bars.append(b(613, 101.8, 101.85, pull_low, 101.6))                       # touch (low into the zone)
    bars.append(b(614, 101.6, confirm_close + 0.05, 101.55, confirm_close))   # reaction bar
    for k in range(615, 650):
        if after <= confirm_close:                                            # flat, never bullish
            bars.append(b(k, confirm_close, confirm_close + 0.01, confirm_close - 0.05, confirm_close))
            continue
        nx = min(after, confirm_close + (k - 614) * 0.1)
        bars.append(b(k, nx - 0.05, nx + 0.02, nx - 0.08, nx))
    return bars


def test_gp_signal_and_target_exit():
    bars = path()
    sig = F.detect(bars, "GP")
    assert len(sig) == 1 and sig[0]["dec_min"] == 614 and abs(sig[0]["H"] - 103.0) < 1e-9 and sig[0]["L"] == 100.0
    tr = F.trade(bars, sig[0])
    assert tr["why"] == "target" and abs(tr["gross"] - (103.0 / bars[sig[0]["entry_i"]][1] - 1)) < 1e-12


def test_no_reaction_no_trade_and_break():
    assert F.detect(path(confirm_close=101.58, after=101.58), "GP") == []    # no close above the previous high
    bars = path(pull_low=100.5)                                               # pierces 0.786 by low only
    i = next(k for k, x in enumerate(bars) if x[0] == 613)
    bars[i] = b(613, 101.8, 101.85, 100.5, 100.6)                             # closes below 100.642 -> broken
    assert F.detect(bars, "GP") == []


def test_swing_must_be_two_percent_and_shallow_zone_is_different():
    flat = [b(570 + k, 100.0, 100.5, 99.9, 100.2) for k in range(80)]
    assert F.detect(flat, "GP") == []
    assert F.zone_bounds("SHALLOW") == (0.236, 0.382) and F.zone_bounds("RANDOM_DEPTH", 0.62) == (0.62, 0.738)


def test_simulate_rules():
    bars = [b(600, 100, 100.2, 99.9, 100.1), b(601, 100.1, 101.0, 100.0, 100.9), b(602, 100.9, 101.6, 100.8, 101.5)]
    r = F.simulate(bars, 0, 99.0, 101.5)
    assert r["why"] == "target" and r["exit"] == 101.5                       # needs high >= 101.51; bar 602 has 101.6
    eq = [b(600, 100, 100.2, 99.9, 100.1), b(601, 100.1, 101.5, 100.0, 100.9)] + [b(602 + k, 100.9, 101.0, 100.8, 100.9) for k in range(40)]
    assert F.simulate(eq, 0, 99.0, 101.5)["why"] == "time"                   # an equal high is not a fill
    st = [b(600, 100, 100.2, 99.9, 100.1), b(601, 100.1, 100.2, 98.0, 98.5), b(602, 98.4, 98.6, 98.0, 98.2)]
    s = F.simulate(st, 0, 99.0, 101.5)
    assert s["why"] == "stop" and s["exit"] == 98.4                           # next bar's open
    both = [b(600, 100, 100.2, 99.9, 100.1), b(601, 100.1, 101.6, 98.0, 98.5)]
    assert F.simulate(both, 0, 99.0, 101.5)["why"] == "target"                # target wins a bar that does both
    gap = [b(600, 100, 100.2, 99.9, 100.1), b(610, 100.3, 100.4, 100.2, 100.3)]
    assert F.simulate(gap, 0, 99.0, 101.5)["why"] == "gap"


def test_control_same_hour_and_separation():
    bars = path()
    sig = F.detect(bars, "GP")[0]
    tr = F.trade(bars, sig)
    c = F.control(bars, sig, tr, "X", "2026-06-01", "fib-gp-ctrl")
    assert c is None or isinstance(c["net"], float)
    assert c == F.control(bars, sig, tr, "X", "2026-06-01", "fib-gp-ctrl")   # seeded


def test_overlap_and_stats():
    rows = [{"day": "d", "sym": "A", "entry_min": 600, "hold": 10, "net": 0.01, "ctrl": 0.0},
            {"day": "d", "sym": "A", "entry_min": 605, "hold": 5, "net": 0.01, "ctrl": 0.0},
            {"day": "d", "sym": "A", "entry_min": 611, "hold": 5, "net": 0.01, "ctrl": 0.0}]
    assert [r["entry_min"] for r in F.no_overlap(rows)] == [600, 611]
    assert F.final(["PASS", "PASS"], [0.01, 0.01], [30, 30]) == "PASS"
    assert F.final(["NOT_PASSED", "NOT_PASSED"], [0.0002, 0.0001], [30, 30]) == "FAIL"
