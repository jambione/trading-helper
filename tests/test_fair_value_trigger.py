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
    rows = [_prow(f"2026-06-{(k % 22) + 1:02d}", f"N{k % 50}", 0.0012 + rng.gauss(0, 5e-4), rng.gauss(0, 3e-4),
                  rng.gauss(0, 3e-4), rng.gauss(0, 5e-4), rng.gauss(0, 3e-4), rng.gauss(0, 3e-4)) for k in range(1200)]
    g = FT.grade(rows)
    assert g["n"] == 1200 and g["days"] == 22 and g["lift_d"] > 0.001 and g["lift_x"] > 0.001 and g["lag1"] > 0
    assert FT.half_verdict(g, "P2") == "PASS"
    assert FT.half_verdict(dict(g, n=999), "P2") == "UNDERPOWERED"
    assert FT.half_verdict(dict(g, t_x=-2.1), "P2") == "FAIL"
    assert FT.final(["PASS", "PASS"], [(0.002, 0.002)] * 2, [22, 22], [0.0001, 0.0002]) == "PASS"
    assert FT.final(["PASS", "PASS"], [(0.002, 0.002)] * 2, [22, 22], [0.0001, -0.0001]) == "PASS_BOUNCE"
    assert FT.final(["NOT_PASSED", "UNDERPOWERED"], [(0.0002, 0.0003), (0.0001, 0.0004)], [22, 5], [0, 0]) == "FAIL"
    assert FT.final(["NOT_PASSED", "UNDERPOWERED"], [(0.0002, 0.0003), (0.0001, 0.0004)], [22, 1], [0, 0]) == "UNDERPOWERED"


def test_armed_sees_only_rows_strictly_before_t():
    ts = [1000.0, 1012.0, 1100.0]
    assert FT.armed(ts, 1013.0, 60.0)               # 1012 is in [953, 1013)
    assert not FT.armed([1100.0], 1100.0, 60.0)      # a row AT T is after the entry
    assert not FT.armed([1101.0], 1100.0, 60.0)      # look-ahead row
    assert not FT.armed([1039.0], 1100.0, 60.0)      # older than 60 s
    assert FT.armed([1040.0], 1100.0, 60.0)


def test_placebo_minute_same_hour_seeded_not_m():
    sip = {k: (10.0, 10.0) for k in range(570, 960)}
    pm = FT.placebo_minute("AAA", "2026-06-10", 700, sip)
    assert 660 <= pm <= 719 and pm != 700 and pm == FT.placebo_minute("AAA", "2026-06-10", 700, sip)
    pm = FT.placebo_minute("AAA", "2026-06-10", 605, sip)
    assert 600 <= pm <= 659                          # clipped to 10:00
    pm = FT.placebo_minute("AAA", "2026-06-10", 925, sip)
    assert 900 <= pm <= 927                          # clipped to 15:27


def _prow(day, sym, arm, ctrl, cross, parm, pctrl, pcross, half="A", pop="P2"):
    r = _row(day, sym, arm, ctrl, cross, half)
    r["pop"] = pop
    r["po"] = {"arm": parm, "ctrl": pctrl, "cross": pcross, "arm_lag1": parm, "ctrl_lag1": pctrl}
    return r


def test_placebo_verdict_gates_on_pseudo_x_only():
    import random as _r
    rng = _r.Random(3)
    pr = [_prow(f"2026-0{6 if h == 'A' else 9}-{(k % 22) + 1:02d}", f"N{k % 40}", 0.0, 0.0, 0.0,
                rng.gauss(0, 5e-4) - 0.0001, rng.gauss(0, 5e-4), rng.gauss(0, 5e-4) - 0.0001, half=h)
          for h in ("A", "B") for k in range(800)]
    v = FT.placebo_verdict(pr)
    assert v["ok"] and v["days"] == {"A": 22, "B": 22} and abs(v["bound"]["A"] - FT.t_crit2(22)) < 1e-12
    biased_d = [dict(r, po=dict(r["po"], ctrl=r["po"]["ctrl"] + 0.002)) for r in pr]
    assert FT.placebo_verdict(biased_d)["ok"]                    # a biased d is the adjustment, not a gate
    biased_x = [dict(r, po=dict(r["po"], arm=r["po"]["arm"] + 0.002, ctrl=r["po"]["ctrl"] + 0.002)) for r in pr]
    assert not FT.placebo_verdict(biased_x)["ok"]


def test_adjusted_lift_is_paired_by_day():
    rows = [_prow("2026-06-01", "A", 0.003, 0.001, 0.0, 0.0005, 0.0015, 0.0),     # trig +20 bp, pseudo -10 bp
            _prow("2026-06-01", "B", 0.001, 0.001, 0.0, 0.001, 0.001, 0.0),       # trig 0, pseudo 0
            _prow("2026-06-02", "A", 0.002, 0.0, 0.0, 0.0, 0.0, 0.0),             # trig +20, pseudo 0
            _prow("2026-06-03", "A", 0.002, 0.0, 0.0, None, None, None)]          # no pseudo: day dropped
    mu, t, n, ub, means, ntr = FT.adj_lift(rows)
    assert n == 2 and ntr == 3
    assert abs(means["2026-06-01"] - (0.001 - (-0.0005))) < 1e-12 and abs(means["2026-06-02"] - 0.002) < 1e-12
    g = FT.grade(rows)
    assert abs(g["lift_d"] - mu) < 1e-15 and abs(g["lift_d_raw"] - (0.001 + 0.002 + 0.002) / 3) < 1e-12
    # name-day: (06-01, A) +30, (06-01, B) 0, (06-02, A) +20
    assert abs(g["nameday"] - (0.003 + 0.0 + 0.002) / 3) < 1e-12


def test_drop_days_uses_day_share():
    def r(day, sym, arm):
        return _prow(day, sym, arm, 0.0, 0.0, 0.0, 0.0, 0.0)               # zero pseudo: adjusted = raw
    rows = [r("2026-06-01", f"N{k}", 0.01) for k in range(10)]                     # big day by sum
    rows += [r(f"2026-06-{d:02d}", "M", 0.0001) for d in range(2, 12)]
    rows += [r("2026-06-20", "Z", 0.02)]                                            # one row, large mean
    rows += [r("2026-06-21", "Y", -0.001), r("2026-06-22", "Y", 0.0005)]
    g = FT.grade(rows)
    # the top 3 days by summed share are 06-01, 06-20, 06-22; dropping them leaves 06-02..06-11 and 06-21
    assert abs(g["drop_days"] - (10 * 0.0001 - 0.001) / 11) < 1e-12
    assert abs(g["net_10bp"] - (g["net"] - 0.0005)) < 1e-15 and abs(g["lift_d_vs_11bp"] - (g["lift_d"] - 0.0011)) < 1e-15
