"""Package 1 decision ledger writer — fail-open, round-trip, shadow hook."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import decision_ledger as dl
import desk_arm_buckets as buckets


@pytest.fixture(autouse=True)
def _ledger_tmp(tmp_path, monkeypatch):
    path = tmp_path / "decision_ledger.jsonl"
    dl.set_ledger_path_for_tests(path)
    yield path
    dl.set_ledger_path_for_tests(None)


def test_row_from_shadow_has_required_fields():
    now = time.time()
    sample = {
        "ts": now,
        "symbol": "GTLB",
        "last_ask_src": "stream",
        "last_ask_age_sec": 3.5,
        "exhaustion_state": "rising",
        "pctr_rising": True,
        "cm_rsi": 48.0,
        "arm_confirm_rsi_max": 52.0,
        "macd_gap_falling": True,
        "macd_gap_rising": False,
        "macd_bull": False,
        "macd_gap": -0.01,
        "arm_ok": False,
        "arm_why": "tape_only",
    }
    row = dl.row_from_shadow(sample)
    for k in (
        "ts", "symbol", "stage", "tape_src", "tape_age_sec",
        "exh_state", "cm_rsi", "cm_rsi_peak",
        "macd_narrowing", "macd_bearish",
        "arm_ok", "arm_why", "arm_bucket",
    ):
        assert k in row
    assert row["symbol"] == "GTLB"
    assert row["stage"] == "arm"
    assert row["tape_src"] == "stream"
    assert row["tape_age_sec"] == 3.5
    assert row["cm_rsi"] == 48.0
    assert row["cm_rsi_peak"] == 52.0
    assert row["macd_narrowing"] is True
    assert row["macd_bearish"] is True
    assert row["arm_ok"] is False
    assert row["arm_why"] == "tape_only"
    assert row["arm_bucket"] == "readiness"


def test_arm_ok_true_clears_bucket():
    row = dl.row_from_shadow({
        "ts": time.time(), "symbol": "AAPL",
        "arm_ok": True, "arm_why": "",
        "last_ask_src": "stream", "last_ask_age_sec": 1.0,
    })
    assert row["arm_bucket"] is None
    assert row["arm_ok"] is True


def test_append_round_trip(tmp_path):
    path = tmp_path / "decision_ledger.jsonl"
    dl.set_ledger_path_for_tests(path)
    row = dl.row_from_shadow({
        "ts": time.time(),
        "symbol": "NIKI",
        "last_ask_src": "stale_tape",
        "last_ask_age_sec": 40.0,
        "arm_ok": False,
        "arm_why": "macd_gap_narrowing",
        "macd_gap_falling": True,
        "cm_rsi": 55.0,
        "exhaustion_state": "rising",
    })
    assert dl.append_row(row) is True
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    got = json.loads(lines[0])
    assert got["symbol"] == "NIKI"
    assert got["arm_bucket"] == "macd_dir"
    assert got["arm_why"] == "macd_gap_narrowing"
    assert "git_version" in got or "config_fp" in got


def test_append_swallows_errors(monkeypatch, tmp_path):
    """Ledger write failure must not raise into the caller."""
    path = tmp_path / "decision_ledger.jsonl"
    dl.set_ledger_path_for_tests(path)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(dl, "ledger_path_for_ts", lambda ts=None: path)
    # Force the open inside append_row to fail.
    real_open = Path.open

    def _open_fail(self, *a, **k):
        if self == path or self.name.endswith("decision_ledger.jsonl"):
            raise OSError("disk full")
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", _open_fail)
    ok = dl.append_row({
        "ts": time.time(), "symbol": "X", "stage": "arm",
        "arm_ok": False, "arm_why": "tape_only", "arm_bucket": "readiness",
    })
    assert ok is False


def test_log_from_shadow_and_event(_ledger_tmp):
    assert dl.log_from_shadow({
        "ts": time.time(), "symbol": "CAPR",
        "arm_ok": False, "arm_why": "exh_falling",
        "last_ask_src": "stream", "last_ask_age_sec": 2.0,
    }) is True
    assert dl.log_from_event(
        "entry_fail", symbol="CAPR", reason="spread", ts=time.time(),
    ) is True
    assert dl.log_from_event("synth_zone", symbol="CAPR") is False  # not mirrored
    lines = _ledger_tmp.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    a, b = json.loads(lines[0]), json.loads(lines[1])
    assert a["arm_bucket"] == "exh"
    assert b["stage"] == "entry"
    assert b["arm_bucket"] == "spread"
    assert b["event_kind"] == "entry_fail"


def test_shadow_hook_via_log_shadow_sample(tmp_path, monkeypatch):
    """ai_positions.log_shadow_sample must append a ledger row (fail-open)."""
    import ai_positions as cp
    from learn_stamps import clear_caches

    clear_caches()
    shadow = tmp_path / "shadow.jsonl"
    ledger = tmp_path / "decision_ledger.jsonl"
    monkeypatch.setattr(cp, "SHADOW_PATH", shadow)
    dl.set_ledger_path_for_tests(ledger)
    cp.log_shadow_sample({
        "ts": time.time(),
        "symbol": "FLYE",
        "arm_ok": False,
        "arm_why": "mistimed_heat",
        "last_ask_src": "stream",
        "last_ask_age_sec": 4.0,
        "cm_rsi": 58.0,
        "exhaustion_state": "overbought",
    })
    assert shadow.exists()
    assert ledger.exists()
    got = json.loads(ledger.read_text(encoding="utf-8").strip().splitlines()[0])
    assert got["symbol"] == "FLYE"
    assert got["arm_bucket"] == "heat"
    assert got["arm_why"] == "mistimed_heat"


def test_mapper_coverage_matches_buckets_module():
    assert buckets.arm_bucket("cheap_ob_band") == "heat"


def test_log_event_hook_does_not_double_kind(tmp_path, monkeypatch):
    """log_event must reach the ledger (kind only once in kwargs)."""
    import ai_positions as cp

    events = tmp_path / "events.jsonl"
    ledger = tmp_path / "decision_ledger.jsonl"
    monkeypatch.setattr(cp, "EVENTS_PATH", events)
    dl.set_ledger_path_for_tests(ledger)
    cp.log_event("entry_fail", symbol="HOOK", reason="spread")
    assert ledger.exists()
    got = json.loads(ledger.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert got["symbol"] == "HOOK"
    assert got["stage"] == "entry"
    assert got["arm_bucket"] == "spread"
    assert got["event_kind"] == "entry_fail"
