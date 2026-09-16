"""Admit ledger — refuse rows, fail-open, seed/inclusion hooks."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import admit_ledger as al
import ai_entry_watch as ew


@pytest.fixture(autouse=True)
def _ledger_tmp(tmp_path):
    path = tmp_path / "admit_ledger.jsonl"
    al.set_ledger_path_for_tests(path)
    yield path
    al.set_ledger_path_for_tests(None)


def test_log_refuse_appends(tmp_path):
    path = tmp_path / "admit_ledger.jsonl"
    al.set_ledger_path_for_tests(path)
    cfg = {
        "ai_admit_ledger_enabled": True,
        "ai_admit_ledger_seed": True,
        "ai_admit_ledger_inclusion": True,
    }
    assert al.log_refuse(
        stage="seed",
        symbol="veea",
        reason="thin_rvol",
        source="momentum",
        extra={"price": 5.93, "rvol": 0.61, "pct": 12.4},
        cfg=cfg,
    ) is True
    assert al.log_refuse(
        stage="inclusion",
        symbol="VEEA",
        reason="no_tape",
        source="trending",
        row={"price": 5.93, "source": "trending"},
        cfg=cfg,
    ) is True
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    a = json.loads(lines[0])
    b = json.loads(lines[1])
    for row in (a, b):
        for k in ("ts", "stage", "symbol", "reason", "kept"):
            assert k in row
        assert row["kept"] is False
    assert a["stage"] == "seed"
    assert a["symbol"] == "VEEA"
    assert a["reason"] == "thin_rvol"
    assert a["source"] == "momentum"
    assert a["rvol"] == 0.61
    assert b["stage"] == "inclusion"
    assert b["reason"] == "no_tape"


def test_disabled_knob_no_write(tmp_path):
    path = tmp_path / "admit_ledger.jsonl"
    al.set_ledger_path_for_tests(path)
    ok = al.log_refuse(
        stage="seed",
        symbol="X",
        reason="thin_rvol",
        cfg={"ai_admit_ledger_enabled": False},
    )
    assert ok is True
    assert not path.exists()


def test_bad_path_returns_false_no_raise(tmp_path, monkeypatch):
    path = tmp_path / "admit_ledger.jsonl"
    al.set_ledger_path_for_tests(path)

    def _open_fail(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", _open_fail)
    ok = al.log_refuse(
        stage="seed",
        symbol="X",
        reason="thin_rvol",
        cfg={"ai_admit_ledger_enabled": True, "ai_admit_ledger_seed": True},
    )
    assert ok is False


def test_note_seed_drop_writes_ledger(tmp_path, monkeypatch):
    path = tmp_path / "admit_ledger.jsonl"
    al.set_ledger_path_for_tests(path)
    monkeypatch.setattr(
        ew,
        "_push_cfg",
        lambda: {
            "ai_admit_ledger_enabled": True,
            "ai_admit_ledger_seed": True,
        },
    )
    ew._clear_seed_drops()
    ew._note_seed_drop("momentum", "abcd", "thin_rvol", price=4.2, rvol=0.5)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["stage"] == "seed"
    assert row["symbol"] == "ABCD"
    assert row["reason"] == "thin_rvol"
    assert row["source"] == "momentum"
    assert row["price"] == 4.2
    assert row["rvol"] == 0.5


def test_inclusion_gate_hook_no_tape(tmp_path, monkeypatch):
    path = tmp_path / "admit_ledger.jsonl"
    al.set_ledger_path_for_tests(path)
    ew._admit_ticks.clear()
    cfg = {
        "ai_admit_ledger_enabled": True,
        "ai_admit_ledger_inclusion": True,
        "ai_watch_admit_ticks": 1,
        "ai_watch_require_indicators": False,
        "ai_watch_require_uptrend": False,
        "ai_watch_min_price": 1.0,
        "ai_watch_min_rvol": 0.0,
        "ai_watch_admit_max_tape_age_sec": 30.0,
        "ai_watch_admit_max_range_pos": 100.0,
    }
    # Force a no_tape-style refuse via passes_inclusion monkeypatch for stability.
    def _fail_no_tape(row, cfg, indicators=None):
        return False, [], "no_tape"

    monkeypatch.setattr(ew, "passes_inclusion", _fail_no_tape)
    rows = [{"symbol": "ZZZZ", "source": "movers", "price": 3.0, "rvol": 2.0}]
    kept, rejected = ew.apply_inclusion_gate(rows, cfg, indicators={})
    assert kept == []
    assert any(r.get("reason") == "no_tape" for r in rejected)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    row = json.loads(lines[-1])
    assert row["stage"] == "inclusion"
    assert row["symbol"] == "ZZZZ"
    assert row["reason"] == "no_tape"
    assert row["source"] == "movers"
    assert row["kept"] is False


def test_schema_required_keys(tmp_path):
    path = tmp_path / "admit_ledger.jsonl"
    al.set_ledger_path_for_tests(path)
    al.log_refuse(
        stage="seed",
        symbol="AAA",
        reason="thin_rvol",
        cfg={"ai_admit_ledger_enabled": True, "ai_admit_ledger_seed": True},
    )
    row = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[0])
    for k in ("ts", "stage", "symbol", "reason", "kept"):
        assert k in row
    assert row["day"]
