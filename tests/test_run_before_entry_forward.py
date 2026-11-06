"""run_before_entry_forward: counts only before due, cut is outcome-blind, a planted effect passes and a null does not."""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import run_before_entry_forward as rb  # noqa: E402

ET = ZoneInfo("America/New_York")


def _rows(path, n, planted, seed=7):
    rng = random.Random(seed)
    with open(path, "w") as fh:
        for i in range(n):
            day = 6 + (i * 20) // n
            t = datetime(2026, 11, day if day <= 30 else 30, 10, 0, tzinfo=ET).timestamp() + i
            rise = rng.uniform(0, 150)
            bad = planted and rise >= 100
            ng = rng.random() < (0.7 if bad else 0.25)
            e = 50.0
            pk = e if ng else e * 1.002
            x = e * (1 - 0.004) if ng else e * 1.0015
            fh.write(json.dumps({"symbol": f"S{i}", "entry_price": e, "exit_price": x, "entry_time": t, "peak_price": pk,
                                 "features": {"rs_rise15_bp": rise, "rs_rng15_bp": rng.uniform(20, 200), "rs_pos30": rng.random()}}) + "\n")


def test_counts_only_before_due(tmp_path, capsys, monkeypatch):
    p = tmp_path / "o.jsonl"; _rows(p, 40, True)
    real = rb.load
    monkeypatch.setattr(rb, "load", lambda: real(str(p)))
    rb.main([])
    out = capsys.readouterr().out
    assert "NOT DUE" in out and "NG" not in out and "net" not in out


def test_planted_effect_passes_and_null_fails(tmp_path):
    p = tmp_path / "a.jsonl"; _rows(p, 400, True)
    rows, _ = rb.load(str(p))
    assert rb.verdict(rows)[0]
    q = tmp_path / "b.jsonl"; _rows(q, 400, False)
    rows, _ = rb.load(str(q))
    assert not rb.verdict(rows)[0]


def test_cut_ignores_outcomes(tmp_path):
    p = tmp_path / "c.jsonl"; _rows(p, 300, True)
    rows, _ = rb.load(str(p))
    flipped = [{**r, "ng": not r["ng"], "bp": -r["bp"]} for r in rows]
    assert rb.cut_of(rows) == rb.cut_of(flipped)
