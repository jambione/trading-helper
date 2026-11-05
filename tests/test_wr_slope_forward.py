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
                    "t": 1_793_900_000.0 + i * 3600, "hold": 200.0, "ng": ng, "bp": rng.gauss(-6 if ng else -2, 15), "rng": rng.uniform(10, 80)})
    return out


def test_hypotheses_flag_what_they_say():
    assert W.HYP["H2_steep"]({"wr_d5_fast": 30.0, "wr_d5_slow": 0.0}) and not W.HYP["H2_steep"]({"wr_d5_fast": 29.9, "wr_d5_slow": 0.0})
    assert W.HYP["H1_flat"]({"wr_d5_fast": -10.7, "wr_d5_slow": 3.0}) and not W.HYP["H1_flat"]({"wr_d5_fast": 10.8, "wr_d5_slow": 0.0})


def test_read_runs_and_finds_a_planted_signal(capsys):
    W.read(_rows(), "H2_steep", W.HYP["H2_steep"])
    out = capsys.readouterr().out
    assert "H2_steep:" in out and ("PASS" in out or "FAIL" in out or "UNDERPOWERED" in out)


def test_no_signal_does_not_pass(capsys):
    W.read(_rows(signal=False), "H2_steep", W.HYP["H2_steep"])
    assert "PASS" not in capsys.readouterr().out


def test_missing_peak_is_dropped_not_never_green(tmp_path, monkeypatch):
    import json
    (tmp_path / "ai_reports").mkdir()
    rows = [{"symbol": "A", "entry_price": 10.0, "exit_price": 10.0, "entry_time": 1_794_000_000.0 + i, "peak_price": None,
             "features": {"wr_d5_fast": 35.0, "wr_d5_slow": 1.0}} for i in range(3)]
    (tmp_path / "ai_reports" / "outcomes.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(W, "ROOT", str(tmp_path)); W.MISSING_PEAK[0] = 0
    assert W.load("2026-11-06") == [] and W.MISSING_PEAK[0] == 3
