"""Returns, peers, betas, Z and verdict logic for fair_value_timing. No market data."""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import fair_value_timing as FV  # noqa: E402

S = FV.S


def test_minute_returns_skip_gaps_and_the_open():
    rows = np.array([[570, 0, 0, 0, 10.0, 1], [571, 0, 0, 0, 10.1, 1], [573, 0, 0, 0, 10.2, 1], [574, 0, 0, 0, 10.0, 1]])
    r = FV.minute_returns(rows)
    assert np.isnan(r[0]) and r[1] == pytest.approx(np.log(10.1 / 10.0))
    assert np.isnan(r[2]) and np.isnan(r[3])                      # 572 missing: no return at 572 or 573
    assert r[4] == pytest.approx(np.log(10.0 / 10.2))


def test_top_peers_basket_and_betas():
    rng = np.random.default_rng(1)
    T = 3900
    f = rng.normal(0, 1e-3, T)
    X = np.column_stack([f + rng.normal(0, 2e-4, T)] + [f + rng.normal(0, 2e-4, T) for _ in range(6)]
                        + [rng.normal(0, 1e-3, T) for _ in range(10)])
    X[::390] = np.nan
    peers = FV.top_peers(X, 0, 6)
    assert set(peers) == set(range(1, 7))
    b = FV.basket(X, peers[:4])
    assert np.isnan(b).all()                                      # fewer than 5 peers present -> NaN everywhere
    b = FV.basket(X, peers)
    spy = rng.normal(0, 5e-4, T)
    y = 0.7 * spy + 0.9 * b + rng.normal(0, 1e-5, T)
    be = FV.fit_betas(y, spy, b)
    assert be[0] == pytest.approx(0.7, abs=0.02) and be[1] == pytest.approx(0.9, abs=0.02)


def test_block_sd_and_z():
    resid = np.full(390 * 10, 0.0001)
    resid[np.arange(len(resid)) % 390 == 0] = np.nan
    rng = np.random.default_rng(2)
    resid = resid + rng.normal(0, 1e-4, len(resid))
    sd = FV.block_sd(resid)
    assert sd is not None and sd > 0
    today = np.full(390, np.nan)
    today[1:61] = -0.0002
    m = S.RTH_START + 61                                          # window 31..60
    assert FV.z_at(today, m, 0.001) == pytest.approx(30 * -0.0002 / 0.001)
    today[40:50] = np.nan                                          # 20 of 30 valid < 24
    assert FV.z_at(today, m, 0.001) is None
    assert FV.z_at(today, m, None) is None


def test_sides():
    r = {"z": -1.2, "groups": {"G1": "IN"}}
    assert FV.sides(r) == {"F1": "IN", "F2": "IN"}
    assert FV.sides({"z": -0.5, "groups": {"G1": "IN"}}) == {"F1": "OUT", "F2": "OUT"}
    assert FV.sides({"z": -1.5, "groups": {"G1": None}}) == {"F1": "IN", "F2": None}
    assert FV.sides({"z": None, "groups": {"G1": "IN"}}) == {"F1": None, "F2": None}


def _row(day, sym, side, g, m=630, half="A"):
    return {"day": day, "sym": sym, "m": m, "half": half, "sides": {"F1": side, "F2": side},
            "groups": {"G1": "IN"}, "z": -1.5 if side == "IN" else 0.0, "gross180": g, "net180": g - 0.0003}


def test_grade_same_hour_and_verdicts():
    rng = np.random.default_rng(3)
    rows = []
    for d in range(25):
        day = f"2026-06-{d + 1:02d}"
        for i in range(60):
            side = "IN" if i < 20 else "OUT"
            rows.append(_row(day, f"N{i}", side, (0.0009 if side == "IN" else 0.0) + rng.normal(0, 3e-4),
                             m=600 + 30 * (i % 6)))
    g = FV.grade(rows, "F1", "A")
    assert g["diff"] == pytest.approx(0.0009, abs=0.0002) and g["same_hour"] > 0 and g["t_crit"] > 2
    g["n_in"] = 1200
    assert FV.half_verdict(g, "F1") == "PASS"
    g["n_in"] = 999
    assert FV.half_verdict(g, "F1") == "UNDERPOWERED"
    assert FV.final(["PASS", "PASS"], [0.002, 0.002]) == "PASS"
    assert FV.final(["NOT_PASSED", "NOT_PASSED"], [0.0003, 0.0004]) == "FAIL"
    p = FV.placebo(rows)
    assert set(p) == {"t", "bound", "ok"}
