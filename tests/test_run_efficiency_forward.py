"""run_efficiency_forward: counts only before due, the three suggesting trades are excluded, planted effect passes, null fails."""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import run_efficiency_forward as re_  # noqa: E402

ET = ZoneInfo("America/New_York")


def _rows(path, n, planted, seed=11, first_hour=13):
    rng = random.Random(seed)
    with open(path, "w") as fh:
        for i in range(n):
            day = 6 + (i * 20) // n
            t = datetime(2026, 11, day, first_hour if day == 6 else 10, 0, tzinfo=ET).timestamp() + i
            er = rng.uniform(0, 1)
            bad = planted and er <= 0.33
            ng = rng.random() < (0.7 if bad else 0.25)
            e = 50.0
            fh.write(json.dumps({"symbol": f"S{i}", "entry_price": e, "exit_price": e * (0.996 if ng else 1.0015), "entry_time": t,
                                 "peak_price": e if ng else e * 1.002,
                                 "features": {"rs_er15": er, "rs_rng15_bp": rng.uniform(20, 200), "rs_rise15_bp": rng.uniform(0, 200), "dwell_sec": rng.choice([5, 600])}}) + "\n")


def test_counts_only_before_due(tmp_path, capsys, monkeypatch):
    p = tmp_path / "o.jsonl"; _rows(p, 40, True)
    real = re_.load
    monkeypatch.setattr(re_, "load", lambda: real(str(p)))
    re_.main([])
    out = capsys.readouterr().out
    assert "NOT DUE" in out and "NG" not in out and "net" not in out


def test_suggesting_trades_before_noon_are_excluded(tmp_path):
    p = tmp_path / "e.jsonl"
    t_am = datetime(2026, 11, 6, 10, 21, tzinfo=ET).timestamp()
    t_pm = datetime(2026, 11, 6, 12, 30, tzinfo=ET).timestamp()
    with open(p, "w") as fh:
        for sym, t in (("SMCI", t_am), ("X", t_pm)):
            fh.write(json.dumps({"symbol": sym, "entry_price": 10.0, "exit_price": 10.1, "entry_time": t, "peak_price": 10.1,
                                 "features": {"rs_er15": 0.3}}) + "\n")
    rows, _ = re_.load(str(p))
    assert [r["er"] for r in rows] == [0.3] and len(rows) == 1


def test_planted_effect_passes_and_null_fails(tmp_path):
    a = tmp_path / "a.jsonl"; _rows(a, 450, True)
    assert re_.verdict(re_.load(str(a))[0])[0]
    b = tmp_path / "b.jsonl"; _rows(b, 450, False)
    assert not re_.verdict(re_.load(str(b))[0])[0]


def test_cut_ignores_outcomes(tmp_path):
    p = tmp_path / "c.jsonl"; _rows(p, 300, True)
    rows, _ = re_.load(str(p))
    flipped = [{**r, "ng": not r["ng"], "bp": -r["bp"]} for r in rows]
    assert re_.cut_of(rows) == re_.cut_of(flipped)
