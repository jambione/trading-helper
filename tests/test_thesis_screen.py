"""Slice predicates for the turnaround screen — no network."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from entry_rule_screen import RULES
from thesis_screen import SLICES


def test_slices_partition_tod_and_source():
    open_t = {"tod": "open_drive", "research": False, "scanner": True,
              "champion": False, "catalyst": False, "feature_ok": True,
              "chase": False, "fresh": True, "rvol": 1.0}
    late_t = dict(open_t, tod="late", fresh=False, chase=True, open_move=4.0)
    res_t = dict(open_t, research=True, scanner=False, champion=True,
                 catalyst=True)

    assert SLICES["open_drive"][1](open_t, None) is True
    assert SLICES["open_drive"][1](late_t, None) is False
    assert SLICES["late"][1](late_t, None) is True
    assert SLICES["research"][1](res_t, None) is True
    assert SLICES["scanner"][1](res_t, None) is False
    assert SLICES["champion"][1](res_t, None) is True
    assert SLICES["catalyst_text"][1](res_t, None) is True
    assert SLICES["chase"][1](late_t, None) is True
    assert SLICES["fresh"][1](open_t, None) is True
    assert SLICES["all"][1](open_t, None) is True


def test_high_rvol_requires_a_number():
    pred = SLICES["high_rvol"][1]
    assert pred({"rvol": 2.0}, None) is True
    assert pred({"rvol": 1.9}, None) is False
    assert pred({"rvol": None}, None) is False


def test_cool_rule_does_not_fire_when_exhaustion_is_missing():
    """IEX-blind names must not be scored as 'cool' — missing is not 0."""
    _, cool = RULES["cool_only"]
    assert cool({"exhaustion": None}) is False
    assert cool({"exhaustion": 20}) is True
    assert cool({"exhaustion": 50}) is False
