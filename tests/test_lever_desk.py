"""Lever Desk scorer: hold_capture, verdicts, classify wrap, no bot_config writes."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import lever_desk as ld  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = "2026-09-11"


def _ts(hour=15, minute=0, day=DAY):
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    return (d.replace(hour=hour, minute=minute)).timestamp()


def _outcome(
    *,
    r,
    mfe,
    day=DAY,
    close_reason="local_trail",
    give_r_at_exit=None,
    usd=None,
):
    e, st = 10.0, 9.5
    return {
        "ts": _ts(15, day=day),
        "symbol": "TEST",
        "entry_price": e,
        "stop_price": st,
        "entry_time": _ts(10, day=day),
        "exit_price": e,
        "exit_time": _ts(15, day=day),
        "realized_r_multiple": r,
        "realized_pl_usd": usd if usd is not None else r * 5.0,
        "mfe_r": mfe,
        "close_reason": close_reason,
        "give_r_at_exit": give_r_at_exit,
        "features": {},
    }


def _write_registry(path: Path, lever: dict):
    path.write_text(json.dumps({"version": 1, "levers": [lever]}, indent=2), encoding="utf-8")


def _base_lever(**score_overrides):
    score = {
        "kind": "hold_capture",
        "min_mfe_r": 0.25,
        "min_n": 5,
        "pass": {"median_capture_gte": 0.40},
        "kill": {"median_capture_lt": 0.15},
    }
    score.update(score_overrides)
    return {
        "id": "green_catchup_trail",
        "title": "Green catch-up",
        "hypothesis": "test",
        "status": "live",
        "shipped_at": "2026-09-10",
        "baseline_sessions": ["2026-09-09"],
        "knobs": {},
        "score": score,
        "next_on_kill": "revert manually",
    }


def test_hold_capture_median_and_excludes_tiny_mfe():
    trades = [
        _outcome(r=0.40, mfe=0.50),   # capture 0.80
        _outcome(r=0.20, mfe=0.50),   # capture 0.40
        _outcome(r=0.10, mfe=0.50),   # capture 0.20
        _outcome(r=0.05, mfe=0.01),   # excluded from qualifying (< 0.25)
        _outcome(r=-0.1, mfe=0.30, close_reason="eod_liquidate"),
    ]
    out = ld.hold_capture(trades, min_mfe_r=0.25)
    assert out["n"] == 5
    assert out["n_qualifying"] == 4  # three 0.50 + one 0.30
    # captures: 0.8, 0.4, 0.2, -0.1/0.3≈-0.333 → median of four
    assert out["median_capture"] is not None
    assert abs(out["median_capture"] - 0.30) < 0.01 or abs(out["median_capture"] - 0.20) < 0.15
    assert out["close_reasons"]["local_trail"] == 4


def test_thin_n_is_measure():
    by_day = {
        "2026-09-11": [
            _outcome(r=0.5, mfe=0.5),
            _outcome(r=0.5, mfe=0.5),
        ],
    }
    scored = ld.score_lever(_base_lever(), by_day, days_back=10, gap=None)
    assert scored["verdict"] == "MEASURE"
    assert "2/2 fills with MFE≥0.25R" in scored["reason"]
    assert "need 5" in scored["reason"]


def test_thin_n_explains_zero_qualifying():
    # Many fills, none reach the MFE bar — the Wed scorecard empty-sample case.
    trades = [_outcome(r=0.05, mfe=0.10) for _ in range(8)]
    by_day = {"2026-09-11": trades}
    scored = ld.score_lever(_base_lever(), by_day, days_back=10, gap=None)
    assert scored["verdict"] == "MEASURE"
    assert "0/8 fills with MFE≥0.25R" in scored["reason"]
    assert scored["current"]["max_mfe_r"] == 0.10
    assert "max MFE 0.10R" in scored["reason"]


def test_pass_threshold_keep():
    # 5 qualifying with capture 0.5 each → KEEP
    trades = [_outcome(r=0.25, mfe=0.50) for _ in range(5)]
    by_day = {"2026-09-11": trades}
    scored = ld.score_lever(_base_lever(), by_day, days_back=10, gap=None)
    assert scored["verdict"] == "KEEP"
    assert scored["current"]["n_qualifying"] == 5
    assert scored["current"]["median_capture"] == 0.5


def test_kill_threshold():
    trades = [_outcome(r=0.05, mfe=0.50) for _ in range(5)]  # capture 0.10
    by_day = {"2026-09-11": trades}
    scored = ld.score_lever(_base_lever(), by_day, days_back=10, gap=None)
    assert scored["verdict"] == "KILL"


def test_gap_forces_measure(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(ld, "REGISTRY_PATH", tmp_path / "lever_desk.json")
    _write_registry(tmp_path / "lever_desk.json", _base_lever())

    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps({"ts": _ts(), "kind": "arm"}) for _ in range(101)) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "outcomes.jsonl").write_text(
        json.dumps(_outcome(r=-0.1, mfe=0.3, day="2026-08-05")) + "\n",
        encoding="utf-8",
    )
    # shipped_at is 2026-09-10 — outcome day is before window; still gap wins
    snap = ld.snapshot(days_back=10, report_dir=tmp_path, repo=tmp_path,
                       cfg={"desk_product": "observe"})
    assert snap["gap"]
    assert snap["corpus"]["outcomes_lines"] < 30
    assert snap["lever"]["score"]["verdict"] == "MEASURE"
    assert "gap" in snap["lever"]["score"]["reason"]


def test_snapshot_clears_gap_when_outcomes_are_plentiful(tmp_path, monkeypatch):
    """Events ≫ outcomes is normal on the mini; do not cry missing ledger."""
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(ld, "REGISTRY_PATH", tmp_path / "lever_desk.json")
    _write_registry(tmp_path / "lever_desk.json", _base_lever())

    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps({"ts": _ts(), "kind": "arm"}) for _ in range(200)) + "\n",
        encoding="utf-8",
    )
    rows = [
        json.dumps(_outcome(r=0.05, mfe=0.10, day="2026-09-11"))
        for _ in range(40)
    ]
    (tmp_path / "outcomes.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    snap = ld.snapshot(days_back=10, report_dir=tmp_path, repo=tmp_path,
                       cfg={"desk_product": "observe"})
    assert snap["corpus"]["outcomes_lines"] >= 30
    assert snap["gap"] is None
    assert "gap" not in (snap["lever"]["score"]["reason"] or "")


def test_classify_passthrough():
    k = ld.classify("widen the give so we stop getting stomped")
    assert k["verdict"] == "KILL"
    assert any(f["id"] == "widen_give" for f in k["families"])

    m = ld.classify("Screen names over $50 for 15m MFE vs round-trip cost")
    assert m["verdict"] == "MEASURE"
    assert m["families"] == []


def test_record_verdict_does_not_write_bot_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(ld, "REGISTRY_PATH", tmp_path / "lever_desk.json")
    _write_registry(tmp_path / "lever_desk.json", _base_lever())

    bot = tmp_path / "bot_config.json"
    bot.write_text('{"ai_local_trail_give_r": 0.2}\n', encoding="utf-8")
    before = bot.read_bytes()

    (tmp_path / "outcomes.jsonl").write_text("", encoding="utf-8")
    out = ld.record_verdict("KEEP", note="ok", operator="jmb", report_dir=tmp_path)
    assert out["recorded"]["verdict"] == "KEEP"
    assert (tmp_path / "lever_desk" / "verdicts.jsonl").exists()
    assert bot.read_bytes() == before
    # tracked registry untouched
    assert "green_catchup_trail" in (tmp_path / "lever_desk.json").read_text()


def test_unknown_score_kind_is_measure():
    lever = _base_lever(kind="occupancy_magic")
    by_day = {"2026-09-11": [_outcome(r=0.5, mfe=0.5) for _ in range(5)]}
    scored = ld.score_lever(lever, by_day, days_back=10, gap=None)
    assert scored["verdict"] == "MEASURE"
    assert "unknown score.kind" in scored["reason"]
