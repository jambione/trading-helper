"""Honesty restamp + engine rt preference (Sep2 stream+stale_quote)."""
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew


def test_honesty_restamp_clears_stream_when_age_past_ceiling(monkeypatch):
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    rec = {
        "symbol": "GTLB",
        "last_ask_src": "stream",
        "last_ask_age_sec": 22.0,
        "last_ask_ts": time.time() - 22.0,
    }
    ew.honesty_restamp_stream_src(rec)
    assert rec["last_ask_src"] == "stale_tape"
    assert rec["price_src"] == "stale_tape"


def test_honesty_restamp_keeps_stream_when_young(monkeypatch):
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    rec = {
        "symbol": "GTLB",
        "last_ask_src": "stream",
        "last_ask_age_sec": 4.0,
        "last_ask_ts": time.time() - 4.0,
    }
    ew.honesty_restamp_stream_src(rec)
    assert rec["last_ask_src"] == "stream"


def test_public_snapshot_never_emits_stream_with_stale_age(monkeypatch):
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "_push_cfg", lambda: {
        "ai_watch_decision_max_age_sec": 15.0,
        "ai_watch_arm_require_stream_price": True,
    })
    now = time.time()
    state = {
        "ALMS": {
            "symbol": "ALMS",
            "status": "watching",
            "last_ask": 10.0,
            "last_ask_src": "stream",
            "last_ask_age_sec": 3.0,  # frozen; map ts is old
            "last_ask_ts": now - 20.0,
            "structure": {
                "entry_low": 9.9,
                "entry_high": 10.1,
                "stop_price": 9.5,
                "wait_kind": "wait_for_zone",
            },
            "score": 1.0,
        }
    }
    rows = ew.public_snapshot(state)
    assert len(rows) == 1
    row = rows[0]
    assert row["last_ask_src"] != "stream"
    assert row["last_ask_src"] == "stale_tape"
    assert row["block_code"] == "stale_quote"
    assert row["last_ask_age_sec"] is not None
    assert row["last_ask_age_sec"] > 15.0


def test_live_print_prefers_younger_engine_rt(monkeypatch):
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": "ASST", "price": 23.0, "price_age_sec": 40.0},
    ])
    monkeypatch.setattr(ew, "_engine_rt_print", lambda sym: (23.5, 1.2))
    got = ew.live_print("ASST")
    assert got == (23.5, 1.2)


def test_live_print_keeps_dash_when_younger_than_engine(monkeypatch):
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": "ASST", "price": 23.0, "price_age_sec": 0.5},
    ])
    monkeypatch.setattr(ew, "_engine_rt_print", lambda sym: (23.5, 2.0))
    got = ew.live_print("ASST")
    assert got == (23.0, 0.5)


def test_live_print_engine_wins_over_undated_dash(monkeypatch):
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": "PPBT", "price": 2.1, "price_age_sec": None},
    ])
    monkeypatch.setattr(ew, "_engine_rt_print", lambda sym: (2.15, 3.0))
    got = ew.live_print("PPBT")
    assert got == (2.15, 3.0)


def test_decision_price_uses_engine_over_rest_when_dash_stale(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda sym: (10.5, 2.0))
    cfg = {"ai_watch_decision_max_age_sec": 15.0}
    px, src, age = ew.decision_price("ALMS", cfg)
    assert src == "stream"
    assert age == 2.0
    assert px == 10.5
