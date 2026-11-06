"""stall_score_forward: counts only before due, early entries excluded, planted composite passes, null fails, cuts are outcome-blind."""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import stall_score_forward as ss  # noqa: E402

ET = ZoneInfo("America/New_York")


def _rows(path, n, planted, seed=5):
    rng = random.Random(seed)
    with open(path, "w") as fh:
        for i in range(n):
            day = 6 + (i * 20) // n
            t = datetime(2026, 11, day, 13 if day == 6 else 10, 0, tzinfo=ET).timestamp() + i
            rise, er, pos = rng.uniform(0, 300), rng.uniform(0, 1), rng.random()
            nsign = (rise >= 200) + (er <= 0.33) + (pos >= 0.8)
            bad = planted and nsign >= 2
            ng = rng.random() < (0.75 if bad else 0.25)
            fh.write(json.dumps({"symbol": f"S{i}", "entry_price": 50.0, "exit_price": 50.0 * (0.996 if ng else 1.0015), "entry_time": t,
                                 "peak_price": 50.0 if ng else 50.1,
                                 "features": {"rs_rise15_bp": rise, "rs_er15": er, "rs_pos30": pos, "rs_rng15_bp": rng.uniform(20, 300)}}) + "\n")


def test_counts_only_before_due(tmp_path, capsys, monkeypatch):
    p = tmp_path / "o.jsonl"; _rows(p, 40, True)
    real = ss.load
    monkeypatch.setattr(ss, "load", lambda: real(str(p)))
    ss.main([])
    out = capsys.readouterr().out
    assert "NOT DUE" in out and "NG" not in out and "net" not in out


def test_entries_before_noon_excluded(tmp_path):
    p = tmp_path / "e.jsonl"
    with open(p, "w") as fh:
        for sym, hh in (("SMCI", 10), ("X", 12)):
            t = datetime(2026, 11, 6, hh, 21, tzinfo=ET).timestamp()
            fh.write(json.dumps({"symbol": sym, "entry_price": 10.0, "exit_price": 10.1, "entry_time": t, "peak_price": 10.1,
                                 "features": {"rs_rise15_bp": 100, "rs_er15": 0.3, "rs_pos30": 0.5}}) + "\n")
    rows, _ = ss.load(str(p))
    assert len(rows) == 1


def test_planted_composite_passes_and_null_fails(tmp_path):
    a = tmp_path / "a.jsonl"; _rows(a, 600, True)
    assert ss.verdict(ss.load(str(a))[0])[0]
    b = tmp_path / "b.jsonl"; _rows(b, 600, False)
    assert not ss.verdict(ss.load(str(b))[0])[0]


def test_cuts_ignore_outcomes_and_missing_pos_is_sign_absent(tmp_path):
    p = tmp_path / "c.jsonl"; _rows(p, 300, True)
    rows, _ = ss.load(str(p))
    flipped = [{**r, "ng": not r["ng"], "bp": -r["bp"]} for r in rows]
    assert ss.cuts(rows) == ss.cuts(flipped)
    r = {"rise": 999.0, "er": 0.01, "pos": None}
    assert ss.signs(r, (100.0, 0.3)) == {"A": True, "B": True, "C": False} and ss.score(r, (100.0, 0.3)) == 2
