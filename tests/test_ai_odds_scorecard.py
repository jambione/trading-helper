import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import ai_odds_scorecard as A  # noqa: E402


def test_start_minute():
    assert A.start_minute("2026-11-19T09:25") == 1
    assert A.start_minute("2026-11-19T12:00") == 151
    assert A.start_minute("2026-11-19T15:00") == 331


def test_outcome_up_first_down_first_neither():
    o = np.full(60, 100.0); h = np.full(60, 100.1); l = np.full(60, 99.9)
    h2 = h.copy(); h2[5] = 100.6
    assert A.outcome(o, h2, l, 1) == 1
    l2 = l.copy(); l2[3] = 99.4
    assert A.outcome(o, h2, l2, 1) == 0
    h3 = h.copy(); l3 = l.copy(); h3[4] = 100.6; l3[4] = 99.4
    assert A.outcome(o, h3, l3, 1) == 0                       # same minute -> down first
    assert A.outcome(o, h, l, 1) == 0                         # neither
    assert A.outcome(o, h, l, 70) is None


def test_model_stats_skill_and_calibration():
    rows = [{"p": 0.8, "y": 1}] * 30 + [{"p": 0.2, "y": 0}] * 30 + [{"p": None, "y": 1}]
    s = A.model_stats(rows)
    assert s["n"] == 60 and s["hit"] == 0.5 and s["skill"] > 0.5
    assert {c["bucket"] for c in s["calibration"]} == {"0.00-0.40", "0.70-1.00"}
    bad = A.model_stats([{"p": 0.9, "y": 0}] * 10 + [{"p": 0.9, "y": 1}] * 10)
    assert bad["skill"] < 0
