"""Onsets, controls, grading and verdicts for fair_value_trigger. No market data."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import fair_value_trigger as FT  # noqa: E402


def test_onsets_cross_below_with_refractory_on_all_minutes():
    zs = {m: 0.0 for m in range(599, 700)}
    for m in (605, 606, 612, 640):
        zs[m] = -1.2                                 # 605 onset; 606 continues; 612 within 15 of 605; 640 onset
    zs[611] = 0.0
    assert FT.onsets(zs, 600, 699) == [605, 640]
    zs[620] = None
    assert FT.onsets({600: -1.5, 599: None}, 600, 600) == []   # Z(m-1) missing: no onset
    rich = {599: 0.5, 600: 1.2}
    assert FT.onsets(rich, 600, 600, cut=1.0, below=False) == [600]


def test_halves():
    assert FT.half_of("P1", "2026-09-25") == "A" and FT.half_of("P1", "2026-09-28") == "B"
    assert FT.half_of("P2", "2026-07-29") == "A" and FT.half_of("P2", "2026-07-30") == "B"


def test_control_and_cross_are_seeded_and_eligible():
    sip = {k: (10.0, 10.0) for k in range(570, 960)}
    c = FT.control_minute("AAA", "2026-09-10", 700, sip)
    assert 701 <= c <= 760 and c == FT.control_minute("AAA", "2026-09-10", 700, sip)
    sips = {"A": sip, "B": sip, "C": {}}
    assert FT.cross_name("A", "2026-09-10", 700, ["A", "B", "C"], lambda n: sips[n]) == "B"
    assert FT.cross_name("B", "2026-09-10", 700, ["B", "C"], lambda n: sips[n]) is None


def _row(day, sym, arm, ctrl, cross, half="A"):
    return {"day": day, "sym": sym, "half": half, "o": {"arm": arm, "ctrl": ctrl, "cross": cross,
                                                        "arm_lag1": arm, "ctrl_lag1": ctrl}}


def test_grade_and_verdicts():
    import random as _r
    rng = _r.Random(6)
    rows = [_row(f"2026-06-{(k % 22) + 1:02d}", f"N{k % 50}", 0.0012 + rng.gauss(0, 5e-4), rng.gauss(0, 3e-4),
               rng.gauss(0, 3e-4)) for k in range(1200)]
    g = FT.grade(rows)
    assert g["n"] == 1200 and g["days"] == 22 and g["lift_d"] > 0.001 and g["lift_x"] > 0.001 and g["lag1"] > 0
    assert FT.half_verdict(g, "P2") == "PASS"
    assert FT.half_verdict(dict(g, n=999), "P2") == "UNDERPOWERED"
    assert FT.half_verdict(dict(g, t_x=-2.1), "P2") == "FAIL"
    assert FT.final(["PASS", "PASS"], [(0.002, 0.002)] * 2, [22, 22], [0.0001, 0.0002]) == "PASS"
    assert FT.final(["PASS", "PASS"], [(0.002, 0.002)] * 2, [22, 22], [0.0001, -0.0001]) == "PASS_BOUNCE"
    assert FT.final(["NOT_PASSED", "UNDERPOWERED"], [(0.0002, 0.0003), (0.0001, 0.0004)], [22, 5], [0, 0]) == "FAIL"
    assert FT.final(["NOT_PASSED", "UNDERPOWERED"], [(0.0002, 0.0003), (0.0001, 0.0004)], [22, 1], [0, 0]) == "UNDERPOWERED"
