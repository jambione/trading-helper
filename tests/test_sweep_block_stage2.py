"""Stage-2 conditions, combo labels, gate and verdicts. No market data."""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import sweep_block_stage2 as S2  # noqa: E402

T = 1_000_000.0


def test_bar_context_uses_only_bars_before_the_window():
    bars = [[569, 9, 99, 1, 9, 1000],                    # premarket: ignored
            [570, 10.0, 10.5, 9.9, 10.2, 100], [571, 10.2, 10.8, 10.1, 10.6, 300],
            [572, 10.6, 11.5, 10.5, 11.0, 200],          # minute m - 1 = 572: in VWAP, not in the day's high
            [573, 11.0, 12.0, 10.9, 11.8, 999]]          # minute m: never used
    c = S2.bar_context(bars, 573)
    assert c["hod_before"] == 10.8 and c["open0"] == 10.0
    assert abs(c["vwap"] - (10.2 * 100 + 10.6 * 300 + 11.0 * 200) / 600) < 1e-12


def test_window_extras_and_conditions():
    qs = [(T - 59, 9.99, 10.01, 100, 100), (T - 20, 10.04, 10.06, 100, 100)]
    trades = [(T - 50, 10.02, 300, ["F"], "C", "Q"), (T - 45, 10.03, 100, ["F"], "C", "Q"),
              (T - 10, 10.07, 100, ["@"], "C", "Q"), (T - 40, 10.02, 12_000, ["@"], "C", "D")]
    x = S2.window_extras({"trades": trades, "quotes": qs}, T)
    assert x["max_px"] == 10.07
    assert abs(x["early_buy_iso_vwp"] - (10.02 * 300 + 10.03 * 100) / 400) < 1e-12
    assert x["early_buy_blk_vwp"] == 10.02                  # $120k TRF print at T - 40 (not late) counts
    r = {"mid_T": 10.05, **x, "hod_before": 10.06, "vwap": 10.0, "open0": 9.9}
    c = S2.conditions(r, "H1")
    assert c == {"C1_LOCATION": True, "C2_CONTEXT": True, "C3_FOLLOW_THROUGH": True}
    assert not S2.conditions(dict(r, hod_before=10.08), "H1")["C1_LOCATION"]
    assert not S2.conditions(dict(r, mid_T=10.0), "H1")["C3_FOLLOW_THROUGH"]
    assert not S2.conditions(dict(r, open0=10.1), "H1")["C2_CONTEXT"]


def test_combo_label_and_gate():
    r = {"g": {"H1": "IN"}, "cond": {"H1": {"C1_LOCATION": False}}}
    assert S2.combo_label(r, "H1", "C1_LOCATION") == "OUT"
    r["cond"]["H1"]["C1_LOCATION"] = True
    assert S2.combo_label(r, "H1", "C1_LOCATION") == "IN"
    assert S2.combo_label({"g": {"H1": None}, "cond": {}}, "H1", "C1_LOCATION") is None
    st = {"graded": {"H1": {"A": {"lift": 0.0001}, "B": {"lift": 0.0002}}, "H2": {"A": {"lift": 0.0001}, "B": {"lift": -0.0001}}}}
    assert S2.qualifying(st) == ["H1"]
    assert S2.qualifying({"graded": {}}) == []


def _rows(lift, seed=4):
    rng = random.Random(seed)
    rows = []
    for d in range(25):
        day = f"2026-06-{d + 1:02d}"
        for k in range(60):
            g = "IN" if k % 3 == 0 else "OUT"
            cond = k % 2 == 0
            y = (lift if (g == "IN" and cond) else 0.0) + rng.gauss(0, 0.002)
            rows.append({"day": day, "sym": f"S{k % 20}", "half": "A", "g": {"H1": g, "H2": g}, "ofi_t": (k // 3) % 3,
                         "notl_t": (k // 9) % 3, "y": y, "spread": 0.0003, "lag1_y": y * 0.5,
                         "cond": {"H1": {c: cond for c in S2.CELLS}}})
    return rows


def test_grade_cell_and_verdicts():
    g = S2.grade_cell(_rows(0.007), "H1", "C1_LOCATION", 3)
    assert g["days"] == 25 and abs(g["lift"] - 0.007) < 0.0015
    g["n_combo"] = 400
    assert S2.cell_verdict(g) == "PASS"
    assert S2.cell_verdict(dict(g, n_combo=299)) == "UNDERPOWERED"
    assert S2.cell_verdict(S2.grade_cell(_rows(-0.007), "H1", "C1_LOCATION", 3)) == "FAIL"
    assert S2.final(["PASS", "PASS"], [0.01, 0.01], [25, 25]).startswith("PASS_STAGE2")
    assert S2.final(["NOT_PASSED", "UNDERPOWERED"], [0.0003, 0.0004], [25, 25]) == "FAIL"
