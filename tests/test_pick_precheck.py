"""pick_precheck: observe-only, no door side effects, unknown never refuses, a bug never refuses."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_entry_watch as ew  # noqa: E402
import pick_precheck as pp  # noqa: E402

CFG = {"ai_watch_admit_arm_gates": True, "ai_watch_max_float_m": 800.0, "ai_watch_require_uptrend": True}


def _row(**k):
    return {"symbol": "ABCD", "source": "agy", "price": 25.0, "pct_change": 2.0, **k}


def test_clean_pick_is_not_refused(monkeypatch):
    monkeypatch.setattr(ew, "admit_arm_gates", lambda row, cfg, now=None: (True, ""))
    import float_feed
    monkeypatch.setattr(float_feed, "float_shares", lambda s: 50.0)
    assert pp.refusal(_row(), CFG, now=1.0) == ""


def test_down_on_the_day_is_refused(monkeypatch):
    monkeypatch.setattr(ew, "admit_arm_gates", lambda row, cfg, now=None: (True, ""))
    import float_feed
    monkeypatch.setattr(float_feed, "float_shares", lambda s: None)
    assert pp.refusal(_row(pct_change=-1.5), CFG, now=1.0) == "not_uptrend"


def test_big_float_and_door_reasons_are_reported(monkeypatch):
    import float_feed
    monkeypatch.setattr(float_feed, "float_shares", lambda s: 3000.0)
    monkeypatch.setattr(ew, "admit_arm_gates", lambda row, cfg, now=None: (True, ""))
    assert pp.refusal(_row(), CFG, now=1.0) == "float_too_big"
    monkeypatch.setattr(ew, "admit_arm_gates", lambda row, cfg, now=None: (False, "below_min_price"))
    assert pp.refusal(_row(), CFG, now=1.0) == "below_min_price"


def test_unknown_facts_do_not_refuse(monkeypatch):
    monkeypatch.setattr(ew, "admit_arm_gates", lambda row, cfg, now=None: (True, ""))
    import float_feed
    monkeypatch.setattr(float_feed, "float_shares", lambda s: None)
    assert pp.refusal(_row(pct_change=None), CFG, now=1.0) == ""


def test_pre_check_does_not_touch_door_state(monkeypatch):
    monkeypatch.setattr(ew, "admit_arm_gates", lambda row, cfg, now=None: (True, ""))
    import float_feed
    monkeypatch.setattr(float_feed, "float_shares", lambda s: None)
    before = dict(ew._UPTREND_LAST_UP)
    pp.refusal(_row(pct_change=3.0), CFG, now=1.0)
    assert ew._UPTREND_LAST_UP == before


def test_tick_never_raises_and_respects_the_switch(monkeypatch, tmp_path):
    monkeypatch.setattr(pp, "LATEST", tmp_path / "latest.json")
    monkeypatch.setattr(pp, "OUT_DIR", tmp_path / "d")
    assert pp.tick({"ai_pick_precheck_log": False}) == {}
    monkeypatch.setattr(pp, "check_boards", lambda cfg, now=None: (_ for _ in ()).throw(RuntimeError("x")))
    assert pp.tick({"ai_pick_precheck_log": True}) == {}


def test_tick_writes_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(pp, "LATEST", tmp_path / "latest.json")
    monkeypatch.setattr(pp, "OUT_DIR", tmp_path / "d")
    monkeypatch.setattr(pp, "check_boards", lambda cfg, now=None: [
        {"symbol": "A", "source": "agy", "price": 10, "pct_change": 1, "refused_by": ""},
        {"symbol": "B", "source": "xai", "price": 5, "pct_change": 1, "refused_by": "below_min_price"}])
    pp._LAST_SIG["sig"] = None
    s = pp.tick({"ai_pick_precheck_log": True}, now=1793805960.0)  # 2026-11-04 10:26 ET
    assert s["n"] == 2 and s["refused"] == 1 and s["by_reason"] == {"below_min_price": 1}
    assert (tmp_path / "latest.json").exists() and list((tmp_path / "d").glob("*.jsonl"))


def test_outside_session_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(pp, "LATEST", tmp_path / "latest.json")
    monkeypatch.setattr(pp, "OUT_DIR", tmp_path / "d")
    # 05:50 ET on 2026-11-05
    assert pp.tick({"ai_pick_precheck_log": True}, now=1793875800.0) == {}
    assert not (tmp_path / "latest.json").exists()
