"""Proposal ledger — dedupe, heartbeat, multi-proposer, hooks."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew
import proposal_ledger as pl
import source_norm as sn


CFG_ON = {
    "ai_proposal_ledger_enabled": True,
    "ai_proposal_ledger_seed": True,
    "ai_proposal_ledger_inclusion": True,
    "ai_proposal_ledger_heartbeat_sec": 300,
}


@pytest.fixture(autouse=True)
def _ledger_tmp(tmp_path):
    path = tmp_path / "proposal_ledger.jsonl"
    pl.set_ledger_path_for_tests(path)
    pl.reset_dedupe_state()
    yield path
    pl.set_ledger_path_for_tests(None)
    pl.reset_dedupe_state()


def _lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_log_proposal_appends(tmp_path):
    path = tmp_path / "proposal_ledger.jsonl"
    pl.set_ledger_path_for_tests(path)
    pl.reset_dedupe_state()
    assert pl.log_proposal(
        stage="seed",
        symbol="veea",
        proposer="momentum",
        decision="dropped",
        reason="thin_rvol",
        extra={"price": 5.93, "rvol": 0.61, "pct": 12.4},
        cfg=CFG_ON,
        ts=1_700_000_000.0,
    ) is True
    assert pl.log_proposal(
        stage="inclusion",
        symbol="VEEA",
        proposer="trending",
        decision="kept",
        row={"price": 5.93, "source": "trending"},
        cfg=CFG_ON,
        ts=1_700_000_100.0,
    ) is True
    rows = _lines(path)
    assert len(rows) == 2
    a, b = rows
    for row in (a, b):
        for k in ("ts", "stage", "symbol", "proposer", "proposer_norm", "decision"):
            assert k in row
    assert a["stage"] == "seed"
    assert a["symbol"] == "VEEA"
    assert a["decision"] == "dropped"
    assert a["reason"] == "thin_rvol"
    assert a["proposer"] == "momentum"
    assert a["proposer_norm"] == "momentum"
    assert a["owner"] is None
    assert a["px"] == 5.93
    assert a["rvol"] == 0.61
    assert b["stage"] == "inclusion"
    assert b["decision"] == "kept"
    assert "reason" not in b
    assert b["proposer_norm"] == "trending"


def test_disabled_knob_no_write(tmp_path):
    path = tmp_path / "proposal_ledger.jsonl"
    pl.set_ledger_path_for_tests(path)
    ok = pl.log_proposal(
        stage="seed",
        symbol="X",
        proposer="momentum",
        decision="dropped",
        reason="thin_rvol",
        cfg={"ai_proposal_ledger_enabled": False},
    )
    assert ok is True
    assert not path.exists()


def test_bad_path_returns_false_no_raise(tmp_path, monkeypatch):
    path = tmp_path / "proposal_ledger.jsonl"
    pl.set_ledger_path_for_tests(path)
    pl.reset_dedupe_state()

    def _open_fail(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", _open_fail)
    ok = pl.log_proposal(
        stage="seed",
        symbol="X",
        proposer="momentum",
        decision="dropped",
        reason="thin_rvol",
        cfg=CFG_ON,
    )
    assert ok is False


def test_identical_state_suppressed_reason_change_writes(tmp_path):
    path = tmp_path / "proposal_ledger.jsonl"
    pl.set_ledger_path_for_tests(path)
    pl.reset_dedupe_state()
    t0 = 1_700_000_000.0
    assert pl.log_proposal(
        stage="seed", symbol="AAA", proposer="momentum",
        decision="dropped", reason="thin_rvol", cfg=CFG_ON, ts=t0,
    )
    assert pl.log_proposal(
        stage="seed", symbol="AAA", proposer="momentum",
        decision="dropped", reason="thin_rvol", cfg=CFG_ON, ts=t0 + 10,
    )
    assert len(_lines(path)) == 1
    assert pl.log_proposal(
        stage="seed", symbol="AAA", proposer="momentum",
        decision="dropped", reason="below_min_price", cfg=CFG_ON, ts=t0 + 20,
    )
    rows = _lines(path)
    assert len(rows) == 2
    assert rows[1]["reason"] == "below_min_price"
    assert rows[1]["heartbeat"] is False


def test_heartbeat_after_interval(tmp_path):
    path = tmp_path / "proposal_ledger.jsonl"
    pl.set_ledger_path_for_tests(path)
    pl.reset_dedupe_state()
    cfg = dict(CFG_ON)
    cfg["ai_proposal_ledger_heartbeat_sec"] = 300
    t0 = 1_700_000_000.0
    pl.log_proposal(
        stage="seed", symbol="BBB", proposer="trending",
        decision="kept", cfg=cfg, ts=t0,
    )
    pl.log_proposal(
        stage="seed", symbol="BBB", proposer="trending",
        decision="kept", cfg=cfg, ts=t0 + 100,
    )
    assert len(_lines(path)) == 1
    pl.log_proposal(
        stage="seed", symbol="BBB", proposer="trending",
        decision="kept", cfg=cfg, ts=t0 + 301,
    )
    rows = _lines(path)
    assert len(rows) == 2
    assert rows[1]["heartbeat"] is True


def test_two_proposers_two_rows(tmp_path):
    path = tmp_path / "proposal_ledger.jsonl"
    pl.set_ledger_path_for_tests(path)
    pl.reset_dedupe_state()
    t0 = 1_700_000_000.0
    pl.log_proposal(
        stage="seed", symbol="CCC", proposer="momentum",
        decision="kept", cfg=CFG_ON, ts=t0,
    )
    pl.log_proposal(
        stage="seed", symbol="CCC", proposer="research",
        decision="kept", cfg=CFG_ON, ts=t0,
    )
    rows = _lines(path)
    assert len(rows) == 2
    norms = {r["proposer_norm"] for r in rows}
    assert norms == {"momentum", "research"}


def test_proposer_norm_matches_source_norm(tmp_path):
    path = tmp_path / "proposal_ledger.jsonl"
    pl.set_ledger_path_for_tests(path)
    pl.reset_dedupe_state()
    for raw in ("agy", "xai", "mom", "bro"):
        pl.reset_dedupe_state()
        pl.log_proposal(
            stage="seed", symbol="DDD", proposer=raw,
            decision="kept", cfg=CFG_ON, ts=time.time(),
        )
        row = _lines(path)[-1]
        assert row["proposer_norm"] == sn.normalize_proposer(raw)


def test_note_seed_drop_writes_proposal_ledger(tmp_path, monkeypatch):
    path = tmp_path / "proposal_ledger.jsonl"
    pl.set_ledger_path_for_tests(path)
    pl.reset_dedupe_state()
    monkeypatch.setattr(
        ew,
        "_push_cfg",
        lambda: {
            **CFG_ON,
            "ai_admit_ledger_enabled": False,
        },
    )
    ew._clear_seed_drops()
    ew._note_seed_drop("momentum", "abcd", "thin_rvol", price=4.2, rvol=0.5)
    rows = _lines(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "seed"
    assert row["symbol"] == "ABCD"
    assert row["decision"] == "dropped"
    assert row["reason"] == "thin_rvol"
    assert row["proposer"] == "momentum"
    assert row["proposer_norm"] == "momentum"
    assert row["px"] == 4.2


def test_inclusion_gate_logs_kept_and_dropped(tmp_path, monkeypatch):
    path = tmp_path / "proposal_ledger.jsonl"
    pl.set_ledger_path_for_tests(path)
    pl.reset_dedupe_state()
    ew._admit_ticks.clear()
    cfg = {
        **CFG_ON,
        "ai_admit_ledger_enabled": False,
        "ai_watch_admit_ticks": 1,
        "ai_watch_require_indicators": False,
        "ai_watch_require_uptrend": False,
        "ai_watch_min_price": 1.0,
        "ai_watch_min_rvol": 0.0,
        "ai_watch_admit_max_tape_age_sec": 30.0,
        "ai_watch_admit_max_range_pos": 100.0,
    }

    def _fail_no_tape(row, cfg, indicators=None):
        return False, [], "no_tape"

    monkeypatch.setattr(ew, "passes_inclusion", _fail_no_tape)
    rows = [{"symbol": "ZZZZ", "source": "movers", "price": 3.0, "rvol": 2.0}]
    kept, rejected = ew.apply_inclusion_gate(rows, cfg, indicators={})
    assert kept == []
    assert any(r.get("reason") == "no_tape" for r in rejected)
    out = _lines(path)
    assert len(out) >= 1
    row = out[-1]
    assert row["stage"] == "inclusion"
    assert row["decision"] == "dropped"
    assert row["reason"] == "no_tape"
    assert row["proposer"] == "movers"

    # Kept path
    pl.reset_dedupe_state()
    if path.exists():
        path.write_text("", encoding="utf-8")

    def _ok(row, cfg, indicators=None):
        return True, ["uptrend"], ""

    monkeypatch.setattr(ew, "passes_inclusion", _ok)
    ew._admit_ticks.clear()
    kept2, rejected2 = ew.apply_inclusion_gate(
        [{"symbol": "KEEP", "source": "trending", "price": 4.0}],
        cfg,
        indicators={},
    )
    assert len(kept2) == 1
    assert rejected2 == []
    kept_rows = _lines(path)
    assert any(
        r.get("symbol") == "KEEP" and r.get("decision") == "kept"
        for r in kept_rows
    )


def test_range_pos_reads_admit_range_pos_spelling(_ledger_tmp):
    """admission_filter stamps rows as ``admit_range_pos``; the ledger must
    capture it, including a legitimate 0.0 (bottom of range)."""
    rows = [
        ("AAA", {"symbol": "AAA", "source": "momentum", "admit_range_pos": 88.5}, 88.5),
        ("BBB", {"symbol": "BBB", "source": "momentum", "admit_range_pos": 0.0}, 0.0),
        ("CCC", {"symbol": "CCC", "source": "momentum", "range_pos": 42.0}, 42.0),
    ]
    for sym, row, _ in rows:
        pl.log_proposal(
            stage="inclusion",
            symbol=sym,
            proposer="momentum",
            decision="dropped",
            reason="admit_range_pos",
            row=row,
            cfg=CFG_ON,
        )
    got = {d["symbol"]: d.get("range_pos") for d in _lines(_ledger_tmp)}
    for sym, _, want in rows:
        assert got[sym] == want, f"{sym}: expected range_pos {want}, got {got[sym]!r}"


def test_range_pos_absent_when_row_has_neither(_ledger_tmp):
    pl.log_proposal(
        stage="inclusion",
        symbol="DDD",
        proposer="momentum",
        decision="dropped",
        reason="not_uptrend",
        row={"symbol": "DDD", "source": "momentum"},
        cfg=CFG_ON,
    )
    assert "range_pos" not in _lines(_ledger_tmp)[0]


def _et_ts(day: int, hh: int, mm: int = 0) -> float:
    import datetime
    from zoneinfo import ZoneInfo
    return datetime.datetime(
        2026, 9, day, hh, mm, tzinfo=ZoneInfo("America/New_York")
    ).timestamp()


def test_inclusion_carries_all_seed_proposers(_ledger_tmp):
    """Shortlist rows carry only the first-wins source; inclusion must still
    name everyone who proposed the symbol, and flag that the label collapsed."""
    for proposer in ("momentum", "grok"):
        pl.log_proposal(stage="seed", symbol="GME", proposer=proposer,
                        decision="kept", cfg=CFG_ON, ts=_et_ts(16, 9))
    pl.log_proposal(stage="seed", symbol="VEEA", proposer="trending",
                    decision="kept", cfg=CFG_ON, ts=_et_ts(16, 9))
    for sym, src in (("GME", "momentum"), ("VEEA", "trending")):
        pl.log_proposal(stage="inclusion", symbol=sym, proposer=src,
                        decision="kept", owner=src, cfg=CFG_ON,
                        ts=_et_ts(16, 9, 1))

    inc = {d["symbol"]: d for d in _lines(_ledger_tmp) if d["stage"] == "inclusion"}
    assert inc["GME"]["proposers_seen"] == ["momentum", "research:xai"]
    assert inc["GME"]["proposer_collapsed"] is True
    # Single proposer is not a collapse — do not cry wolf.
    assert inc["VEEA"]["proposers_seen"] == ["trending"]
    assert inc["VEEA"]["proposer_collapsed"] is False


def test_day_rollover_writes_fresh_first_sight(_ledger_tmp):
    """Unchanged state on a new ET day must write a real first-sight row, not
    wait for a heartbeat — the scorecard anchors on first proposal."""
    args = dict(stage="seed", symbol="AAA", proposer="momentum",
                decision="dropped", reason="thin_rvol", cfg=CFG_ON)
    pl.log_proposal(**args, ts=_et_ts(16, 9))
    pl.log_proposal(**args, ts=_et_ts(16, 9, 1))   # same day, suppressed
    pl.log_proposal(**args, ts=_et_ts(17, 9))      # next day, fresh

    rows = _lines(_ledger_tmp)
    assert [r["day"] for r in rows] == ["2026-09-16", "2026-09-17"]
    assert all(r["heartbeat"] is False for r in rows)
    # Rollover bounds memory to one session.
    assert len(pl._last) == 1 and len(pl._seed_proposers) == 1
