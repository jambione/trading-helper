"""Signing, features, pool rules, stratified lift and verdicts for sweep_block. No market data."""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import sweep_block as SB  # noqa: E402

T = 1_000_000.0


def test_iso_signed_against_strictly_earlier_quote():
    qs = [(T - 30, 9.99, 10.01, 100, 100), (T - 10, 10.03, 10.05, 100, 100)]   # the sweep lifts the quote at T - 10
    iso = (T - 10, 10.02, 500, ["F"], "C", "Q")      # at T - 10 the old mid is 10.00 -> buy; the new mid 10.04 -> sell
    reg = (T - 10, 10.02, 500, ["@"], "C", "Q")
    s_iso = SB.sign_trades([iso], qs, T)
    s_reg = SB.sign_trades([reg], qs, T)
    assert s_iso[0][3] == 1 and s_reg[0][3] == -1
    assert SB.sign_trades([(T, 10.0, 1, ["@"], "C", "Q")], qs, T) == []           # [T - 60, T) only


def test_features_iso_and_blocks_with_late_trf():
    qs = [(T - 59, 9.99, 10.01, 100, 100), (T - 20, 9.99, 10.01, 100, 100)]
    trades = [(T - 50 + k, 10.02, 100, ["@"], "C", "Q") for k in range(4)]
    trades += [(T - 40, 10.02, 400, ["F"], "C", "Q"), (T - 30, 9.98, 20_000, ["@"], "C", "Q"),
               (T - 5, 10.02, 20_000, ["@"], "C", "D")]            # a $200k TRF print in the last 10 s: excluded
    f = SB.features({"trades": trades, "quotes": qs}, T)
    vol = 400 + 400 + 20_000 + 20_000
    assert abs(f["iso60"] - 400 / vol) < 1e-12 and f["n_blocks"] == 1
    assert f["blk60"] < 0 and abs(f["ofi60"] - (800 - 20_000 + 20_000) / vol) < 1e-12 and f["mid_T"] == 10.0


def test_pool_rules():
    pilot = [{"day": "d", "sym": "X", "m": 700}]
    ons = [("d", "X", 694), ("d", "X", 706), ("d", "X", 707), ("d", "Y", 700), ("e", "X", 700)]
    assert SB.fresh_pool(ons, pilot) == [("d", "X", 707), ("d", "Y", 700), ("e", "X", 700)]
    rows = [[698, 1, 1, 1, 19.5, 1], [699, 1, 1, 1, 20.5, 1], [700, 1, 1, 1, 30.0, 1]]
    assert SB.iex_close_before(rows, 700) == 20.5 and SB.iex_close_before(rows, 699) == 19.5
    assert SB.iex_close_before(rows, 698) is None


def test_control_and_placebo_minutes():
    sip = {k: (10.0, 10.0) for k in range(570, 960)}
    c = SB.control_minute("A", "d", 700, sip)
    assert 704 <= c <= 760
    p = SB.placebo_minute("A", "d", 700, sip)
    assert 660 <= p <= 719 and not (699 <= p <= 704)


def _rows(lift, days=25, seed=3):
    rng = random.Random(seed)
    rows = []
    for d in range(days):
        day = f"2026-06-{d + 1:02d}"
        for k in range(60):
            g = "IN" if k % 3 == 0 else "OUT"
            y = (lift if g == "IN" else 0.0) + rng.gauss(0, 0.002)
            rows.append({"day": day, "sym": f"S{k % 20}", "half": "A", "g": {"H1": g, "H2": g}, "ofi_t": k % 3 if k % 2 else (k + 1) % 3,
                         "notl_t": (k // 3) % 3, "blk_side": "BUY" if g == "IN" else "SELL", "y": y, "spread": 0.0003,
                         "bar_y": y, "lag1_y": y * 0.5, "placebo_y": rng.gauss(0, 0.002), "ctrl": rng.gauss(0, 0.002)})
    return rows


def test_stratified_lift_and_verdicts():
    rows = _rows(0.004)
    mu, t, days, ub = SB.primary(rows, "H1")
    assert days == 25 and abs(mu - 0.004) < 0.001 and t > 4
    g = SB.grade(rows, "H1")
    g["n_in"] = 1500
    assert SB.half_verdict(g, "H1") == "PASS"
    assert SB.half_verdict(dict(g, bar_placebo_lift=0.0003), "H1") == "NOT_PASSED"
    assert SB.half_verdict(dict(g, latency_lag1=-0.0001), "H1") == "NOT_PASSED"
    assert SB.half_verdict(dict(g, n_in=999), "H1") == "UNDERPOWERED"
    g2 = SB.grade(rows, "H2")
    assert g2["buy_vs_sell_blocks"] > 0.003
    assert SB.half_verdict(SB.grade(_rows(-0.004), "H1"), "H1") == "FAIL"
    assert SB.final(["PASS", "PASS"], [0.01, 0.01], [25, 25]).startswith("PASS_STAGE1")
    assert SB.final(["NOT_PASSED", "NOT_PASSED"], [0.0004, 0.0002], [25, 25]) == "FAIL"
    cnt = SB.count_step(rows)
    assert cnt["H1 A"]["days"] == 25 and cnt["H1 A"]["projected_mde"] is not None
