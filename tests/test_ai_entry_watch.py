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
