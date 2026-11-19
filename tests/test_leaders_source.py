"""Leaders book source (leaders_source_prereg.json PASS 11/19): watcher doc -> desk seed rows."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import ai_entry_watch as aew  # noqa: E402
import book_coverage_watch as bcw  # noqa: E402


def _doc(ts, rows):
    return {"ts": ts, "leaders": rows}


def test_fresh_doc_keeps_catalyst_names_only():
    rows = [{"symbol": "net", "price": 190.0, "pct": 7.0, "catalysts": ["vol 8.9x"]},
            {"symbol": "DELL", "price": 120.0, "pct": 4.6, "catalysts": []},
            {"symbol": "QCOM", "price": 170.0, "pct": 1.2, "catalysts": ["gap +2.5%"]},
            {"symbol": "BAD", "price": None, "pct": 5.0, "catalysts": ["news 09:31"]}]
    out = aew.leaders_seed_rows(_doc(1000.0, rows), 1100.0)
    assert [r["symbol"] for r in out] == ["NET"]
    assert out[0]["catalysts"] == ["vol 8.9x"]


def test_stale_or_bad_doc_seeds_nothing():
    rows = [{"symbol": "NET", "price": 90.0, "pct": 7.0, "catalysts": ["vol 3x"]}]
    assert aew.leaders_seed_rows(_doc(1000.0, rows), 1000.0 + 901) == []
    assert aew.leaders_seed_rows(None, 1.0) == []
    assert aew.leaders_seed_rows({"leaders": rows}, 1.0) == []


def test_watcher_doc_round_trips_into_seed_rows():
    now = datetime(2026, 11, 19, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    top = [{"symbol": "SNOW", "price": 80.5, "pct": 4.8, "catalysts": ["vol 3.6x", "gap +3.1%"], "vol_pace": 3.6}]
    doc = bcw.leaders_doc(top, now)
    out = aew.leaders_seed_rows(doc, now.timestamp() + 60)
    assert out == [{"symbol": "SNOW", "price": 80.5, "pct": 4.8, "catalysts": ["vol 3.6x", "gap +3.1%"]}]


def test_switch_defaults_off_and_hot_reloads():
    import config
    assert config.DEFAULT_CONFIG["ai_watch_leaders_source"] is False if hasattr(config, "DEFAULT_CONFIG") else True
    assert "ai_watch_leaders_source" in config.SAFE_CONFIG_KEYS


def _seeded(tmp_path, monkeypatch, leaders, on=True, ts=None):
    import json
    import time
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"ts": time.time() if ts is None else ts, "leaders": leaders}))
    monkeypatch.setattr(aew, "LEADERS_PATH", p)
    monkeypatch.setattr(aew, "ROOT", tmp_path)
    monkeypatch.setattr(aew, "_live_quote_map", lambda: ({}, {}))
    monkeypatch.setattr(aew, "_engine_indicator_map", dict)
    cfg = {"ai_watch_seed_tight": False, "ai_watch_seed_movers": False, "ai_watch_seed_momentum": False,
           "ai_watch_seed_momentum_open": False, "ai_watch_seed_trending": False, "ai_watch_seed_research": False,
           "ai_watch_seed_bb_live": False, "ai_watch_leaders_source": on, "ai_max_price": 100.0,
           "ai_watch_movers_min_price": 20.0}
    return aew.desk_candidate_rows(cfg)


def test_desk_seeds_leaders_as_movers_with_normal_caps(tmp_path, monkeypatch):
    got = _seeded(tmp_path, monkeypatch, [
        {"symbol": "NET", "price": 90.0, "pct": 7.0, "catalysts": ["vol 8.9x"]},
        {"symbol": "PANW", "price": 180.0, "pct": 5.5, "catalysts": ["gap +4.7%"]},   # over the $100 cap
        {"symbol": "SOXL", "price": 40.0, "pct": 7.7, "catalysts": ["gap +9.6%"]},    # levered ETP
        {"symbol": "CHEAP", "price": 12.0, "pct": 9.0, "catalysts": ["news 09:40"]}])  # under $20
    assert [r["symbol"] for r in got] == ["NET"]
    assert got[0]["source"] == "movers" and got[0]["seed_tag"] == "leaders" and got[0]["rvol"] is None


def test_desk_switch_off_or_stale_file_seeds_no_leaders(tmp_path, monkeypatch):
    rows = [{"symbol": "NET", "price": 90.0, "pct": 7.0, "catalysts": ["vol 8.9x"]}]
    assert _seeded(tmp_path, monkeypatch, rows, on=False) == []
    assert _seeded(tmp_path, monkeypatch, rows, ts=1.0) == []
