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
    """When engine is past the decision ceiling, a younger dash print wins."""
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": "ASST", "price": 23.0, "price_age_sec": 0.5},
    ])
    monkeypatch.setattr(ew, "_engine_rt_print", lambda sym: (23.5, 20.0))
    got = ew.live_print("ASST")
    assert got == (23.0, 0.5)


def test_live_print_young_engine_wins_even_if_dash_younger(monkeypatch):
    """GTLB-class: eng≤ceiling always wins decision last_ask (book-wide)."""
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": "GTLB", "price": 51.5, "price_age_sec": 1.0},
    ])
    monkeypatch.setattr(ew, "_engine_rt_print", lambda sym: (51.59, 2.9))
    got = ew.live_print("GTLB")
    assert got == (51.59, 2.9)


def test_live_print_young_engine_wins_over_stale_dash(monkeypatch):
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": "GTLB", "price": 51.5, "price_age_sec": 19.9},
    ])
    monkeypatch.setattr(ew, "_engine_rt_print", lambda sym: (51.59, 2.9))
    px, src, age = ew.decision_price("GTLB", {"ai_watch_decision_max_age_sec": 15.0})
    assert src == "stream"
    assert age == 2.9
    assert px == 51.59


def test_public_snapshot_refreshes_young_engine_over_stale_rec(monkeypatch):
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "_push_cfg", lambda: {
        "ai_watch_decision_max_age_sec": 15.0,
        "ai_watch_arm_require_stream_price": True,
    })
    monkeypatch.setattr(ew, "_engine_rt_print", lambda sym: (51.59, 2.9))
    now = time.time()
    state = {
        "GTLB": {
            "symbol": "GTLB",
            "status": "watching",
            "last_ask": 51.0,
            "last_ask_src": "stale_tape",
            "last_ask_age_sec": 19.9,
            "last_ask_ts": now - 19.9,
            "structure": {
                "entry_low": 50.0,
                "entry_high": 52.0,
                "stop_price": 48.0,
                "wait_kind": "wait_for_zone",
            },
            "score": 1.0,
        }
    }
    rows = ew.public_snapshot(state)
    assert len(rows) == 1
    row = rows[0]
    assert row["last_ask_src"] == "stream"
    assert row["last_ask"] == 51.59
    assert row["last_ask_age_sec"] is not None
    assert row["last_ask_age_sec"] <= 15.0
    assert row["block_code"] != "stale_quote"


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


def test_live_quote_for_prefers_young_engine(monkeypatch):
    """Overlay paint must use young engine rt_* (desk-vs-engine lag fix)."""
    import dashboard as d

    monkeypatch.setattr(d, "_load_signal_state", lambda: {
        "tickers": {"GTLB": {"rt_price": 51.59, "rt_price_age_sec": 2.9}},
    })
    # Make mtime advance a no-op (age stays 2.9).
    monkeypatch.setattr(d.os.path, "getmtime", lambda path: __import__("time").time())
    monkeypatch.setattr(
        "ai_entry_watch.decision_max_age_sec", lambda cfg=None: 15.0, raising=False)
    # Stale STATE must not win.
    class _Lock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(d.STATE, "lock", _Lock())
    d.STATE.tickers["GTLB"] = {"price": 51.5, "price_age_sec": 19.9}
    px, age = d._live_quote_for("GTLB")
    assert px == 51.59
    assert age is not None and age <= 15.0


def test_book_table_rows_young_eng_wins_over_stale_quote_ts(monkeypatch):
    """entry_book paint must not let stale _LAST_QUOTE_TS beat young eng.

    GTLB Sep2 ~12:25 ET residual after 3f007f3: public_snapshot aligned, but
    book_table_rows set stream age from live_print then apply_tape_blocker
    recomputed age from an older map ts and restamped stale_tape.
    """
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "_push_cfg", lambda: {
        "ai_watch_decision_max_age_sec": 15.0,
        "ai_watch_arm_require_stream_price": True,
    })
    monkeypatch.setattr(ew, "stream_quote", lambda sym: (51.59, 3.5))
    # Stale map clock from an earlier poll — the bug trigger.
    ew._LAST_QUOTE_TS["GTLB"] = time.time() - 18.6
    state = {
        "GTLB": {
            "symbol": "GTLB",
            "status": "watching",
            "last_ask": 51.0,
            "last_ask_src": "stale_tape",
            "last_ask_age_sec": 18.6,
            "last_ask_ts": time.time() - 18.6,
            "structure": {
                "entry_low": 50.0,
                "entry_high": 52.0,
                "stop_price": 48.0,
                "wait_kind": "wait_for_zone",
            },
            "score": 1.0,
        }
    }
    rows = ew.book_table_rows(state=state, positions={})
    assert len(rows) == 1
    row = rows[0]
    assert row["last_ask_src"] == "stream"
    assert row["last_ask"] == 51.59
    assert row["last_ask_age_sec"] is not None
    assert float(row["last_ask_age_sec"]) <= 15.0
    assert row.get("block_code") != "stale_quote"


def test_overlay_young_eng_does_not_restamp_stale_from_old_ts(monkeypatch):
    """Overlay must stamp last_ask_ts so apply_tape_blocker sees young age."""
    import dashboard as d

    monkeypatch.setattr(d, "_live_quote_for", lambda sym, now=None: (51.59, 3.5))
    monkeypatch.setattr(
        "ai_entry_watch.decision_max_age_sec", lambda cfg=None: 15.0, raising=False)

    payload = {
        "entry_book": [{
            "symbol": "GTLB",
            "entry_low": 50.0,
            "entry_high": 52.0,
            "stop_price": 48.0,
            "last_ask": 51.0,
            "last_ask_src": "stale_tape",
            "last_ask_age_sec": 18.6,
            "last_ask_ts": time.time() - 18.6,
            "block_code": "stale_quote",
            "phase": "watching",
        }],
        "entry_watch": [],
    }
    out = d.overlay_ai_book_live_prices(payload)
    row = out["entry_book"][0]
    assert row["last_ask_src"] == "stream"
    assert float(row["last_ask_age_sec"]) <= 15.0
    assert row.get("block_code") != "stale_quote"
