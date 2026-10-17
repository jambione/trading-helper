"""Arithmetic of the frozen cost re-grade. Does not read market data."""
import json
from pathlib import Path

from tools.studies.cost_regrade import bonferroni_t, grade_family, net3_of, t_net

ROOT = Path(__file__).resolve().parents[1]
DOC = json.loads((ROOT / "docs" / "studies" / "cost_regrade_prereg.json").read_text())


def test_net3_converts_a_published_net_back_to_gross_before_the_3bp():
    assert net3_of("gross", 10.5, None) == 7.5
    assert net3_of("net", -11.9, 10.0) == -4.9
    assert net3_of("realized", -5.0, None) == -5.0


def test_t_shifts_with_the_mean_and_is_absent_without_a_published_t():
    assert t_net(2.0, 10.0, 7.0) == 1.4
    assert t_net(None, 10.5, 7.5) is None
    assert t_net(-2.0, -4.0, -7.0) == -3.5


def test_bonferroni_is_stricter_than_the_single_test_bar():
    assert bonferroni_t(1.5, 4) > 1.5
    assert bonferroni_t(2.0, 1) == 2.0 or abs(bonferroni_t(2.0, 1) - 2.0) < 1e-6


def test_published_family_fails_and_leaves_the_unread_rows_out_of_k():
    rows = grade_family(DOC)
    by = {r["id"]: r for r in rows}
    assert by["sip_breakout_capped_rawfloor"]["verdict"] == "FAIL"
    assert by["reentry_after_win"]["verdict"] == "FAIL"
    assert by["needle_c2"]["verdict"] == "FAIL"
    assert by["mid_rise"]["verdict"] == "FAIL"
    assert by["mid_rise"]["net3"] == [7.5, -5.1]
    assert all(r["k"] == 4 for r in rows)
    assert "day_hold_pilot" not in by
    assert "pct_r_plus_rsi_sip" not in by


def test_per_half_t_between_the_bars_is_not_promising_without_a_pooled_object():
    # K=4. net3 = 17. t_net = t * 17/20 = 2.3, between bonferroni(1.5)=2.127
    # and bonferroni(2)=2.531. No pooled object.
    t = 2.3 * 20.0 / 17.0
    halves = [
        {"name": "A", "mean_bp": 20.0, "t": t},
        {"name": "B", "mean_bp": 20.0, "t": t},
    ]
    doc = {
        "candidates": [
            {"id": "toy", "kind": "gross", "halves": halves},
            {"id": "d1", "kind": "realized", "realized_bp": -1.0},
            {"id": "d2", "kind": "realized", "realized_bp": -1.0},
            {"id": "d3", "kind": "realized", "realized_bp": -1.0},
        ]
    }
    row = grade_family(doc)[0]
    assert row["k"] == 4
    assert 2.127 < row["t_net"][0] < 2.531
    assert row["verdict"] == "FAIL"


def test_an_explicit_pooled_object_can_be_promising():
    t = 1.6 * 20.0 / 17.0
    doc = {
        "candidates": [
            {
                "id": "toy",
                "kind": "gross",
                "halves": [
                    {"name": "A", "mean_bp": 20.0, "t": t},
                    {"name": "B", "mean_bp": 20.0, "t": t},
                ],
                "pooled": {"mean_bp": 20.0, "t": t},
            }
        ]
    }
    assert grade_family(doc)[0]["verdict"] == "PROMISING"
    bare = json.loads(json.dumps(doc))
    del bare["candidates"][0]["pooled"]
    assert grade_family(bare)[0]["verdict"] == "FAIL"


def test_a_both_halves_positive_row_can_pass_the_family_bar():
    doc = {
        "candidates": [
            {
                "id": "toy",
                "kind": "gross",
                "halves": [
                    {"name": "A", "mean_bp": 20.0, "t": 4.0},
                    {"name": "B", "mean_bp": 20.0, "t": 4.0},
                ],
            }
        ]
    }
    row = grade_family(doc)[0]
    assert row["verdict"] == "PASS"
    assert row["net3"] == [17.0, 17.0]
