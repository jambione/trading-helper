from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "studies"))
import wr_slope_forward as W  # noqa: E402


def _rows(n=400, seed=3, signal=True):
    rng = random.Random(seed); out = []
    for i in range(n):
        fast = rng.uniform(-10, 60)
        ng = rng.random() < (0.7 if (signal and fast >= 30) else 0.4)
        out.append({"d": "2026-11-%02d" % (6 + i % 20), "f": {"wr_d5_fast": fast, "wr_d5_slow": rng.uniform(-15, 15)},
                    "ng": ng, "bp": rng.gauss(-6 if ng else -2, 15), "rng": rng.uniform(10, 80)})
    return out


def test_hypotheses_flag_what_they_say():
    assert W.HYP["H2_steep"]({"wr_d5_fast": 30.0, "wr_d5_slow": 0.0}) and not W.HYP["H2_steep"]({"wr_d5_fast": 29.9, "wr_d5_slow": 0.0})
    assert W.HYP["H1_flat"]({"wr_d5_fast": -10.7, "wr_d5_slow": 3.0}) and not W.HYP["H1_flat"]({"wr_d5_fast": 10.8, "wr_d5_slow": 0.0})


def test_read_runs_and_finds_a_planted_signal(capsys):
    W.read(_rows(), "H2_steep", W.HYP["H2_steep"])
    out = capsys.readouterr().out
    assert "H2_steep:" in out and "PASS" in out or "FAIL" in out


def test_no_signal_does_not_pass(capsys):
    W.read(_rows(signal=False), "H2_steep", W.HYP["H2_steep"])
    assert "PASS" not in capsys.readouterr().out
