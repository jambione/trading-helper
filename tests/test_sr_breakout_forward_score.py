"""Groups, the day-FE difference and verdicts for sr_breakout_forward_score. No market data."""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import sr_breakout_forward_score as F  # noqa: E402


def test_groups():
    assert F.group({"day": "2026-10-06", "resist_0.3": True, "realized_bp": 1}, "H1") is None
    assert F.group({"day": "2026-10-07", "resist_0.3": True, "realized_bp": 1}, "H1") == "G"
    assert F.group({"day": "2026-10-07", "brk_dist_pct": None, "realized_bp": 1}, "H2") == "R"
    assert F.group({"day": "2026-10-07", "brk_dist_pct": 0.35, "realized_bp": 1}, "H2") == "G"
    assert F.group({"day": "2026-10-07", "brk_dist_pct": 0.2, "realized_bp": 1}, "H2") is None
    assert F.group({"day": "2026-10-07", "brk_dist_pct": 0.2, "realized_bp": 1}, "H3") == "G"
    assert F.group({"day": "2026-10-07", "brk_dist_pct": 0.05, "realized_bp": 1}, "H3") is None
    assert F.group({"day": "2026-10-07", "resist_0.3": True, "realized_bp": None}, "H1") is None


def test_fixed_effect_removes_day_levels_and_verdict():
    rng = random.Random(1)
    rows = []
    for k in range(12):
        day = f"2026-10-{k + 7:02d}"
        level = rng.gauss(0, 50)                       # big day-level swings must not leak into the difference
        share = 0.2 + 0.05 * k                         # and the group share differs by day
        for i in range(40):
            g = rng.random() < share
            rows.append({"day": day, "resist_0.3": g, "realized_bp": level + (-10 if g else 0) + rng.gauss(0, 15)})
    f = F.fe_diff(rows, "H1")
    assert abs(f["diff"] + 10) < 3 and f["t"] < -2.4 and f["days"] == 12
    assert F.verdict(f, [{"diff": -9}, {"diff": -11}], "H1") == "PASS"
    assert F.verdict(f, [{"diff": -9}, {"diff": 1}], "H1") == "NOT_PASSED"
    assert F.verdict(dict(f, n_group=29), [{"diff": -9}, {"diff": -11}], "H1") == "UNDERPOWERED"
    assert F.verdict(dict(f, diff=-4.0), [{"diff": -9}, {"diff": -11}], "H1") == "NOT_PASSED"
