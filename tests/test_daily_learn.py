"""daily_learn roll-up + ledger idempotency."""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import daily_learn as dl  # noqa: E402


def test_outcomes_summary_basic():
    rows = [
        {"realized_r_multiple": 1.0, "realized_pl_usd": 10.0,
         "close_reason": "target_hit", "entry_path": "watch",
         "edge_mode": "exhaustion_scalp"},
        {"realized_r_multiple": -0.5, "realized_pl_usd": -5.0,
         "close_reason": "stopped_out", "entry_path": "watch",
         "edge_mode": "exhaustion_scalp"},
        {"realized_r_multiple": None, "close_reason": "unknown"},
    ]
    s = dl.outcomes_summary(rows)
    assert s["n"] == 3
    assert s["n_scored"] == 2
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["avg_r"] == pytest.approx(0.25)
    assert s["sum_pl_usd"] == pytest.approx(5.0)
    assert s["by_close_reason"]["target_hit"] == 1
    assert s["by_entry_path"]["watch"] == 2


def test_append_ledger_idempotent(tmp_path):
    path = tmp_path / "daily_ledger.jsonl"
    a = {"day": "2026-08-11", "avg_r": -0.1, "n_outcomes": 14}
    b = {"day": "2026-08-11", "avg_r": 0.2, "n_outcomes": 14}
    c = {"day": "2026-08-12", "avg_r": 0.0, "n_outcomes": 0}
    dl.append_ledger(a, path)
    dl.append_ledger(b, path)
    dl.append_ledger(c, path)
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    by_day = {r["day"]: r for r in lines}
    assert by_day["2026-08-11"]["avg_r"] == 0.2
    assert by_day["2026-08-12"]["n_outcomes"] == 0


def test_write_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    # re-import path resolution uses env
    from ai_paths import resolve_report_dir
    assert resolve_report_dir() == tmp_path

    payload = {
        "day": "2026-08-11",
        "generated_at": "2026-08-12T00:00:00+00:00",
        "regime": {"edge_mode": "exhaustion_scalp", "git_version": "abc",
                   "config_fp": "fp", "paper": True, "book_owner": "grok",
                   "exit_left_overbought": False},
        "outcomes": {"n": 0, "n_scored": 0, "win_rate": None, "avg_r": None,
                     "sum_r": None, "sum_pl_usd": None,
                     "by_close_reason": {}, "by_entry_path": {},
                     "by_edge_mode": {}, "by_entry_exhaustion_state": {}},
        "n_trades_log": 0,
        "desk": {"funnel": {"admitted": 0, "armed": 0, "filled": 0,
                            "closed_with_outcome": 0}},
        "fill_truth": {"ok": False, "error": "test"},
        "ledger": {"day": "2026-08-11", "n_outcomes": 0, "avg_r": None},
    }
    paths = dl.write_artifacts(payload)
    assert Path(paths["md"]).exists()
    assert Path(paths["json"]).exists()
    assert Path(paths["ledger"]).exists()
    assert "exhaustion_scalp" in Path(paths["md"]).read_text()
