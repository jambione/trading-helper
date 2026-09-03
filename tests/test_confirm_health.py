"""Confirm-streak health aggregator (IREN-class daily metric)."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import confirm_health as ch  # noqa: E402

ET = ZoneInfo("America/New_York")


def _recheck(sym, streak, need=2, stage=None, ts=None):
    if stage is None:
        stage = "pass" if streak >= need else "confirm"
    row = {
        "kind": "arm_recheck",
        "symbol": sym,
        "stage": stage,
        "streak": streak,
        "need": need,
        "ok": stage == "pass",
    }
    if ts is not None:
        row["ts"] = ts
    return row


def _shadow(sym, n=1):
    return [{"symbol": sym, "arm_ok": True} for _ in range(n)]


def test_rate_is_ready_over_streak_ge1():
    # 8 names reach 2, 5 stay at 1 → 8/13, the Sep 3 shape.
    events = []
    shadow = []
    for i in range(8):
        s = f"R{i}"
        events += [_recheck(s, 1), _recheck(s, 2)]
        shadow += _shadow(s, 3)
    for i in range(5):
        s = f"S{i}"
        events += [_recheck(s, 1)]
        shadow += _shadow(s, 3)
    stats = ch.accumulate(shadow, events)
    out = ch.summarize(stats, need=2, stuck_min=5)
    assert out["n_streak"] == 13
    assert out["n_ready"] == 8
    assert out["confirm_ready_rate"] == pytest.approx(8 / 13)


def test_iren_class_many_arm_ok_max_streak_1():
    events = [_recheck("IREN", 1) for _ in range(21)]
    shadow = _shadow("IREN", 21)
    stats = ch.accumulate(shadow, events)
    out = ch.summarize(stats, need=2, stuck_min=5)
    assert out["n_streak"] == 1
    assert out["n_ready"] == 0
    assert out["confirm_ready_rate"] == 0.0
    assert out["n_stuck"] == 1
    assert out["streak1_stuck"][0]["symbol"] == "IREN"
    assert out["streak1_stuck"][0]["max_streak"] == 1
    assert out["streak1_stuck"][0]["n_arm_ok"] == 21


def test_gtlb_refresh_veto_is_not_stuck():
    """Many batch arm_ok, never reached the streak counter → GTLB, not IREN."""
    shadow = _shadow("GTLB", 40)
    events = [{"kind": "arm_recheck", "symbol": "GTLB", "stage": "refresh",
               "ok": False, "why": "rsi_not_rising"} for _ in range(40)]
    stats = ch.accumulate(shadow, events)
    out = ch.summarize(stats, need=2, stuck_min=5)
    assert out["n_streak"] == 0
    assert out["n_arm_ok_syms"] == 1
    assert out["n_stuck"] == 0
    assert out["confirm_ready_rate"] is None


def test_a_single_flicker_is_not_stuck():
    stats = ch.accumulate(_shadow("X", 1), [_recheck("X", 1)])
    out = ch.summarize(stats, need=2, stuck_min=5)
    assert out["n_stuck"] == 0
    assert out["n_streak"] == 1
    assert out["n_ready"] == 0


def test_reaching_need_is_not_stuck_even_with_many_ok():
    events = [_recheck("OK", 1)] + [_recheck("OK", 2)] * 10
    stats = ch.accumulate(_shadow("OK", 20), events)
    out = ch.summarize(stats, need=2, stuck_min=5)
    assert out["n_ready"] == 1
    assert out["n_stuck"] == 0
    assert stats["OK"]["max_streak"] == 2


def test_empty_is_none_rate_no_warn():
    out = ch.summarize({}, need=2)
    assert out["n_streak"] == 0
    assert out["n_ready"] == 0
    assert out["confirm_ready_rate"] is None
    assert out["warn"] is False


def test_need_comes_from_events():
    events = [_recheck("A", 1, need=2), _recheck("B", 2, need=2)]
    stats = ch.accumulate([], events)
    assert ch.resolve_need(stats, fallback=1) == 2


def test_warn_on_rate_collapse_vs_prior():
    # 8/10 = 0.80 yesterday; 2/10 = 0.20 today.
    events = [_recheck(f"R{i}", 2) for i in range(2)] + [
        _recheck(f"S{i}", 1) for i in range(8)]
    stats = ch.accumulate([], events)
    out = ch.summarize(
        stats, need=2, stuck_min=5,
        prior={"confirm_ready_rate": 0.80, "n_stuck": 0, "n_streak": 10},
    )
    assert out["confirm_ready_rate"] == pytest.approx(0.20)
    assert out["warn"] is True
    assert "collapsed" in (out["warn_reason"] or "")


def test_warn_on_stuck_spike():
    events = [_recheck(s, 1) for s in ("IREN", "FOO", "BAR")]
    # n_confirm=1 each is below stuck_min=5; boost confirm counts.
    events = []
    shadow = []
    for s in ("IREN", "FOO", "BAR"):
        events.extend(_recheck(s, 1) for _ in range(6))
        shadow.extend(_shadow(s, 6))
    stats = ch.accumulate(shadow, events)
    out = ch.summarize(
        stats, need=2, stuck_min=5,
        prior={"confirm_ready_rate": 0.70, "n_stuck": 0, "n_streak": 8},
    )
    assert out["n_stuck"] == 3
    assert out["warn"] is True
    assert "stuck" in (out["warn_reason"] or "")


def test_no_warn_on_tiny_sample():
    stats = ch.accumulate(_shadow("X", 2), [_recheck("X", 1), _recheck("Y", 2)])
    out = ch.summarize(
        stats, need=2,
        prior={"confirm_ready_rate": 1.0, "n_stuck": 0, "n_streak": 10},
    )
    # n_streak=2 < MIN_N_FOR_WARN
    assert out["n_streak"] == 2
    assert out["warn"] is False


def test_collect_day_filters_et_and_split(tmp_path):
    day = date(2026, 9, 3)
    # 10:00 ET and 14:00 ET.
    pre = datetime(2026, 9, 3, 10, 0, tzinfo=ET).timestamp()
    post = datetime(2026, 9, 3, 14, 0, tzinfo=ET).timestamp()
    other = datetime(2026, 9, 2, 15, 0, tzinfo=ET).timestamp()
    events = tmp_path / "events.jsonl"
    shadow = tmp_path / "shadow.jsonl"
    rows = [
        {**_recheck("IREN", 1), "ts": pre},
        {**_recheck("IREN", 1), "ts": pre},
        {**_recheck("IREN", 1), "ts": pre},
        {**_recheck("IREN", 1), "ts": pre},
        {**_recheck("IREN", 1), "ts": pre},
        {**_recheck("PATH", 1), "ts": post},
        {**_recheck("PATH", 2), "ts": post},
        {**_recheck("OLD", 2), "ts": other},
    ]
    events.write_text("".join(json.dumps(r) + "\n" for r in rows))
    shadow.write_text("".join(
        json.dumps({"ts": pre, "symbol": "IREN", "arm_ok": True}) + "\n"
        for _ in range(5)
    ) + json.dumps({"ts": post, "symbol": "PATH", "arm_ok": True}) + "\n")
    split = datetime(2026, 9, 3, 12, 36, 39, tzinfo=ET).timestamp()
    pre_stats = ch.collect_day(
        day, shadow_path=shadow, events_path=events, ts_max=split)
    post_stats = ch.collect_day(
        day, shadow_path=shadow, events_path=events, ts_min=split)
    assert pre_stats["IREN"]["max_streak"] == 1
    assert pre_stats["IREN"]["n_arm_ok"] == 5
    assert "PATH" not in pre_stats
    assert post_stats["PATH"]["max_streak"] == 2
    assert "IREN" not in post_stats
    assert "OLD" not in pre_stats and "OLD" not in post_stats


def test_build_for_day_and_emit(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    day = date(2026, 9, 3)
    ts = datetime(2026, 9, 3, 10, 30, tzinfo=ET).timestamp()
    events = tmp_path / "events.jsonl"
    shadow = tmp_path / "shadow.jsonl"
    recs = []
    for s in ("IREN", "FOO", "BAR", "BAZ", "QUX"):
        recs.extend({**_recheck(s, 1), "ts": ts} for _ in range(6))
    events.write_text("".join(json.dumps(r) + "\n" for r in recs))
    shadow.write_text("".join(
        json.dumps({"ts": ts, "symbol": s, "arm_ok": True}) + "\n"
        for s in ("IREN", "FOO", "BAR", "BAZ", "QUX")
        for _ in range(6)
    ))
    out = ch.build_for_day(
        day, shadow_path=shadow, events_path=events,
        ledger_path=tmp_path / "daily_ledger.jsonl",
        need=2, stuck_min=5,
    )
    assert out["n_stuck"] == 5
    assert out["warn"] is True
    ch.emit_summary_events(out)
    kinds = [json.loads(l)["kind"] for l in events.read_text().splitlines()
             if l.strip() and json.loads(l).get("kind") in
             ("confirm_health", "confirm_health_warn")]
    # emit appends to the same events.jsonl
    assert "confirm_health" in kinds
    assert "confirm_health_warn" in kinds


def test_one_liner_and_ledger_fields():
    stats = ch.accumulate(_shadow("A", 3), [_recheck("A", 2), _recheck("B", 1)])
    out = ch.summarize(stats, need=2)
    line = ch.one_liner(out)
    assert "1/2 ready" in line
    lf = ch.ledger_fields(out)
    assert lf["confirm_n_ready"] == 1
    assert lf["confirm_n_streak"] == 2
    assert lf["confirm_need"] == 2


def test_prior_from_ledger_picks_previous_day(tmp_path):
    path = tmp_path / "daily_ledger.jsonl"
    path.write_text(
        json.dumps({"day": "2026-09-01", "confirm_ready_rate": 0.5,
                    "confirm_n_streak": 4, "confirm_stuck_n": 0}) + "\n"
        + json.dumps({"day": "2026-09-02", "confirm_ready_rate": 0.8,
                      "confirm_n_streak": 10, "confirm_stuck_n": 1}) + "\n"
        + json.dumps({"day": "2026-09-03", "confirm_ready_rate": 0.1,
                      "confirm_n_streak": 13, "confirm_stuck_n": 5}) + "\n"
    )
    prior = ch.prior_from_ledger(path, date(2026, 9, 3))
    assert prior["day"] == "2026-09-02"
    assert prior["confirm_ready_rate"] == 0.8
    assert prior["n_stuck"] == 1
