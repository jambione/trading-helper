# tests/test_ai_entry_watch.py
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import DEFAULT_CONFIG, load_config

def test_watch_config_defaults_present():
    for key in (
        "ai_watch_enabled",
        "ai_watch_require_agreement",
        "ai_watch_single_source",
        "ai_watch_poll_sec",
        "ai_structure_ttl_sec",
        "ai_watch_expire_at_close",
        "ai_entry_zone_pad_pct",
        "ai_max_structure_calls_per_hour",
        "ai_persist_entry_decisions",
    ):
        assert key in DEFAULT_CONFIG
    cfg = load_config()
    assert cfg["ai_watch_enabled"] is True
    assert cfg["ai_watch_require_agreement"] is True
    assert cfg["ai_watch_poll_sec"] == 20.0


def test_upsert_requires_agreement_by_default(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    cfg = {"ai_watch_require_agreement": True, "ai_watch_single_source": False}
    rows = [
        {"symbol": "SMCI", "agreement": True, "trending_score": 8.2, "reason": "ai"},
        {"symbol": "HOOD", "agreement": False, "trending_score": 7.5, "reason": "x"},
    ]
    state = ew.upsert_from_rows(rows, cfg=cfg, now=1_000.0)
    assert "SMCI" in state
    assert "HOOD" not in state


def test_drop_missing_invalidates(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    state = {
        "SMCI": {"symbol": "SMCI", "status": "watching", "updated_ts": 1.0},
        "OLD": {"symbol": "OLD", "status": "watching", "updated_ts": 1.0},
    }
    out = ew.drop_missing(state, {"SMCI"}, now=2.0)
    assert out["SMCI"]["status"] == "watching"
    assert out["OLD"]["status"] == "invalidated"
