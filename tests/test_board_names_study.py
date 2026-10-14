"""Gate values, day lift and verdicts for the board-names study. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import board_names_study as B  # noqa: E402


def _daily(n, close=50.0, vol=20e6, last=None):
    rows = [(f"2026-08-{(i % 28) + 1:02d}", close, vol) for i in range(n)]
    if last is not None:
        rows[-1] = (rows[-1][0], last, vol)
    return rows


def test_prior_daily_is_strictly_before_the_day():
    rows = [("2026-10-01", 1.0, 1.0), ("2026-10-02", 2.0, 1.0), ("2026-10-03", 3.0, 1.0)]
    assert [r[0] for r in B.prior_daily(rows, "2026-10-02")] == ["2026-10-01"]


def test_price_and_runway_gates():
    prior = _daily(60, close=40.0)
    g = B.gate_values(41.0, 10 * 60 + 30, 40.5, prior)
    assert g["G1_price"] is True
    assert g["G2_runway"] is True            # +2.5% by 10:30
    g = B.gate_values(41.0, 10 * 60 + 47, 40.5, prior)
    assert g["G2_runway"] is None            # after 10:46: not graded (out group is morning seats only)
    assert g["G2_any_seat"] is False         # the as-first-written version, information only
    g = B.gate_values(40.9, 10 * 60, 40.5, prior)
    assert g["G2_runway"] is False           # +2.25% < 2.35%
    assert B.gate_values(29.99, 600, 30.0, prior)["G1_price"] is False


def test_above_50d_uses_the_50_closes_ending_with_the_prior_session():
    prior = _daily(60, close=10.0, last=12.0)  # prior close 12 > mean(49x10, 12)
    assert B.gate_values(12.0, 600, 12.0, prior)["G3_above_50d"] is True
    assert B.gate_values(12.0, 600, 12.0, _daily(49))["G3_above_50d"] is None


def test_liquidity_gate_on_20_day_dollar_volume():
    assert B.gate_values(50.0, 600, 50.0, _daily(20, close=50.0, vol=11e6))["G4_liquid"] is True   # $550M
    assert B.gate_values(50.0, 600, 50.0, _daily(20, close=50.0, vol=9e6))["G4_liquid"] is False   # $450M
    assert B.gate_values(50.0, 600, 50.0, _daily(19))["G4_liquid"] is None


def test_unknown_inputs_are_none_not_false():
    g = B.gate_values(None, 600, None, [])
    assert all(g[k] is None for k in B.GATES)


def test_day_lift_needs_both_sides():
    rows = [(True, 0.001)] * 30 + [(False, -0.001)] * 30
    mi, li, ni, no = B.day_lift(rows)
    assert abs(mi - 0.001) < 1e-12 and abs(li - 0.002) < 1e-12 and (ni, no) == (30, 30)
    assert B.day_lift([(True, 0.001)] * 30 + [(False, 0.0)] * 29)[1] is None


def test_half_verdict():
    assert B.half_verdict(0.0006, 2.1, 0.0, 8, 20) == "PASS"
    assert B.half_verdict(0.0006, 2.1, -0.0001, 8, 20) == "FAIL"   # gated board still fades
    assert B.half_verdict(0.0006, 2.1, 0.0, 7, 20) == "UNDERPOWERED"
    assert B.half_verdict(0.0006, 2.1, 0.0, 8, 19) == "UNDERPOWERED"


def test_rvol_counts_rth_only():
    prior = _daily(20, vol=390.0)            # 1 share per RTH minute on average
    vol = {9 * 60 + 15: 1000.0, 9 * 60 + 30: 1.0, 9 * 60 + 31: 1.0}
    assert abs(B.rvol_at(vol, 9 * 60 + 31, prior) - 1.0) < 1e-9   # premarket excluded


def test_split_scale_converts_the_prior_close_to_raw_minutes():
    daily = [("2026-10-01", 10.0, 1.0), ("2026-10-02", 10.0, 1.0)]   # adjusted (1:4 reverse split later)
    assert B.split_scale(daily, "2026-10-02", 40.0) == 4.0
    assert B.split_scale(daily, "2026-10-02", 10.2) == 1.0                # <5%: noise, not a split
    g = B.gate_values(41.0, 600, 40.0, [("2026-10-01", 10.0, 1.0)], pc_scale=4.0)
    assert g["G2_runway"] is True             # 41 vs raw prior close 40: +2.5%
    assert g["gap_down"] is False             # open 40 vs 40: flat
    g = B.gate_values(41.0, 600, 40.0, [("2026-10-01", 10.0, 1.0)])
    assert g["G2_runway"] is True and g["gap_down"] is False            # unscaled: +310% (the bug the scale fixes reads as a huge gain here)
    g = B.gate_values(10.25, 600, 10.0, [("2026-10-01", 40.0, 1.0)])
    assert g["G2_runway"] is False and g["gap_down"] is True            # unscaled forward split: reads -75%
