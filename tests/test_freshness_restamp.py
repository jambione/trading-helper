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
    # Isolate from live signal_state / dash (mini RTH has young eng for ALMS).
    monkeypatch.setattr(ew, "live_print", lambda sym: None)
    monkeypatch.setattr(ew, "_engine_rt_print", lambda sym: None)
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
            "block_code": "stale_quote",
            "block_reason": "stale quote",
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
    # Underlying rec must also drop the sticky tape-data refuse.
    assert state["GTLB"].get("block_code") != "stale_quote"


def test_clear_tape_data_block_when_stream_fresh():
    """BIAF/SNDG: stream+young must clear sticky stale_quote on the record."""
    now = time.time()
    rec = {
        "symbol": "BIAF",
        "last_ask_src": "stream",
        "price_src": "stream",
        "last_ask_age_sec": 2.0,
        "last_ask_ts": now - 2.0,
        "block_code": "stale_quote",
        "block_reason": "stale quote",
        "block_detail": "tape age unknown or old",
    }
    assert ew.clear_tape_data_block_if_stream_fresh(rec) is True
    assert rec.get("block_code") is None
    assert rec.get("block_reason") is None


def test_clear_tape_data_block_promotes_false_stale_young_age(monkeypatch):
    """Class C CAPR: stale_tape label + age≤ceiling must promote and clear.

    clear_tape_data_block used to require src=stream first, so a false
    stale_tape label beside a young dated age could never recover.
    """
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "live_print", lambda sym: None)
    rec = {
        "symbol": "CAPR",
        "last_ask": 8.29,
        "last_ask_src": "stale_tape",
        "last_ask_age_sec": 2.0,
        "last_ask_ts": time.time() - 2.0,
        "block_code": "stale_quote",
        "block_reason": "stale quote",
    }
    assert ew.clear_tape_data_block_if_stream_fresh(rec) is True
    assert rec["last_ask_src"] == "stream"
    assert rec.get("block_code") is None


def test_clear_tape_data_block_keeps_true_thin_stale_tape(monkeypatch):
    """True thin: old stale_tape must keep refusing (no promote)."""
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "live_print", lambda sym: None)
    rec = {
        "symbol": "SNDG",
        "last_ask": 10.5,
        "last_ask_src": "stale_tape",
        "last_ask_age_sec": 120.0,
        "last_ask_ts": time.time() - 120.0,
        "block_code": "stale_quote",
        "block_reason": "stale quote",
    }
    assert ew.clear_tape_data_block_if_stream_fresh(rec) is False
    assert rec["last_ask_src"] == "stale_tape"
    assert rec["block_code"] == "stale_quote"


def test_apply_decision_price_clears_stale_quote_on_stream(monkeypatch):
    monkeypatch.setattr(ew, "decision_price", lambda *a, **k: (10.5, "stream", 1.5))
    rec = {
        "symbol": "SMCI",
        "block_code": "stale_quote",
        "block_reason": "stale quote",
        "last_ask_src": "stale_tape",
    }
    px, src, age = ew.apply_decision_price(
        rec, {"ai_watch_decision_max_age_sec": 15.0}, time.time())
    assert src == "stream" and px == 10.5 and age == 1.5
    assert rec["last_ask_src"] == "stream"
    assert rec.get("block_code") != "stale_quote"


def test_should_arm_buy_refuses_stale_tape_src():
    rec = {
        "symbol": "BIAF",
        "status": "watching",
        "last_ask_src": "stale_tape",
        "structure": {
            "decision": "WAIT",
            "wait_kind": "wait_for_zone",
            "entry_low": 10.0,
            "entry_high": 10.2,
            "stop_price": 9.5,
            "target_1": 11.0,
            "reward_risk": 1.0,
        },
    }
    ok, why = ew.should_arm_buy(
        rec, ask=10.1, bid=10.0, cfg={"desk_product": "scalp_legacy"})
    assert ok is False
    assert why == "stale_quote"


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


def test_align_stream_clock_fixes_lagging_map_before_blocker(monkeypatch):
    """Class C / APLD-SMCI: young field age must beat lagging _LAST_QUOTE_TS.

    Paint stamped last_ask_age_sec=2.9 with src=stream while the map still
    held ~40s; apply_tape_blocker then restamped stale_quote and honesty
    demoted stream — desk showed stale_quote beside eng age ≤5s.
    """
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "_push_cfg", lambda: {
        "ai_watch_decision_max_age_sec": 15.0,
        "ai_watch_arm_mode": "last",
    })
    monkeypatch.setattr(ew, "_desk_rvol", lambda _s: None)
    monkeypatch.setattr(ew, "_row_arm_refuse", lambda *_a, **_k: None)
    ew._LAST_QUOTE_TS["APLD"] = time.time() - 40.0
    row = {
        "symbol": "APLD", "source": "xai",
        "entry_low": 25.0, "entry_high": 27.0, "stop_price": 24.0,
        "zone_kind": "at_last",
        "last_ask_src": "stream",
        "last_ask_age_sec": 2.9,
        # Deliberately omit last_ask_ts — map is the only clock (the bug).
        "block_code": "stale_quote",
        "blocker": "stale quote",
        "block_reason": "stale quote",
    }
    assert ew.row_quote_age_sec(row) is not None
    assert ew.row_quote_age_sec(row) > 15.0  # map lies
    assert ew.align_stream_clock_if_field_young(row) is True
    assert ew.row_quote_age_sec(row) <= 15.0
    ew.apply_tape_blocker(row, 26.36)
    assert row["last_ask_src"] == "stream"
    assert row.get("block_code") != "stale_quote"


def test_apply_tape_blocker_trusts_young_field_over_old_row_ts(monkeypatch):
    """Paint: stream + young field must clear stale_quote even if row ts is old."""
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "_push_cfg", lambda: {
        "ai_watch_decision_max_age_sec": 15.0,
        "ai_watch_arm_mode": "last",
    })
    monkeypatch.setattr(ew, "_desk_rvol", lambda _s: None)
    monkeypatch.setattr(ew, "_row_arm_refuse", lambda *_a, **_k: None)
    ew._LAST_QUOTE_TS["SMCI"] = time.time() - 55.0
    row = {
        "symbol": "SMCI", "source": "trending",
        "entry_low": 38.0, "entry_high": 41.0, "stop_price": 37.0,
        "zone_kind": "at_last",
        "last_ask_src": "stream",
        "last_ask_age_sec": 3.0,
        "last_ask_ts": time.time() - 55.0,  # lagging poller ts
        "block_code": "stale_quote",
        "block_reason": "stale quote",
        "blocker": "stale quote",
    }
    ew.apply_tape_blocker(row, 39.4)
    assert row["last_ask_src"] == "stream"
    assert row.get("block_code") != "stale_quote"
    assert str(row.get("block_reason") or "").lower() != "stale quote"
    assert ew.row_quote_age_sec(row) <= 15.0


def test_true_thin_stale_still_refuses(monkeypatch):
    """Do not clear stale_quote when the print is genuinely old."""
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "live_print", lambda sym: None)
    monkeypatch.setattr(ew, "_push_cfg", lambda: {
        "ai_watch_decision_max_age_sec": 15.0,
        "ai_watch_arm_mode": "last",
    })
    row = {
        "symbol": "SNDG", "source": "momentum",
        "entry_low": 10.0, "entry_high": 11.0, "stop_price": 9.0,
        "zone_kind": "at_last",
        "last_ask_src": "stale_tape",
        "last_ask_age_sec": 120.0,
        "last_ask_ts": time.time() - 120.0,
        "block_code": "stale_quote",
        "block_reason": "stale quote",
    }
    ew.apply_tape_blocker(row, 10.5)
    assert row["block_code"] == "stale_quote"
    assert row["last_ask_src"] == "stale_tape"


def test_promote_false_stale_from_young_field_age(monkeypatch):
    """stale_tape + age≤ceiling → stream; should_arm must not return stale_quote."""
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "live_print", lambda sym: None)
    now = time.time()
    rec = {
        "symbol": "CAPR",
        "status": "watching",
        "last_ask": 8.29,
        "last_ask_src": "stale_tape",
        "price_src": "stale_tape",
        "last_ask_age_sec": 10.1,
        "last_ask_ts": now - 10.1,
        "block_code": "stale_quote",
        "structure": {
            "decision": "BUY",
            "entry_low": 8.0,
            "entry_high": 8.5,
            "stop_price": 7.5,
            "target_1": 9.5,
            "wait_kind": "wait_for_zone",
        },
    }
    assert ew.promote_stream_src_if_print_fresh(rec) is True
    assert rec["last_ask_src"] == "stream"
    ok, why = ew.should_arm_buy(
        rec, ask=8.29, bid=8.28,
        cfg={
            "ai_watch_decision_max_age_sec": 15.0,
            "ai_watch_arm_mode": "last",
            "desk_product": "scalp_legacy",
            "ai_watch_arm_require_macd": False,
            "ai_watch_require_exh_rising": False,
            "ai_watch_arm_require_cm_rsi": False,
            "ai_watch_soft_ob_enabled": False,
            "ai_watch_mistimed_heat_enabled": False,
            "ai_watch_macd_block_narrowing": False,
        },
        now=now,
    )
    assert why != "stale_quote"
    assert why != "tape_only"


def test_promote_from_young_live_print_not_field(monkeypatch):
    """live_print young recovers empty/stale label even without field age."""
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "live_print", lambda sym: (12.5, 3.0))
    rec = {
        "symbol": "SDOT",
        "last_ask": 13.0,
        "last_ask_src": "stale_tape",
        "last_ask_age_sec": 90.0,
        "last_ask_ts": time.time() - 90.0,
        "block_code": "stale_quote",
    }
    assert ew.promote_stream_src_if_print_fresh(rec) is True
    assert rec["last_ask_src"] == "stream"
    assert rec["last_ask"] == 12.5
    assert float(rec["last_ask_age_sec"]) == 3.0
    assert ew.clear_tape_data_block_if_stream_fresh(rec) is True
    assert rec.get("block_code") is None


def test_no_promote_without_prints_keeps_thin_refusal(monkeypatch):
    """No young live_print and no young field → true thin stays refused."""
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg=None: 15.0)
    monkeypatch.setattr(ew, "live_print", lambda sym: None)
    rec = {
        "symbol": "LGCL",
        "status": "watching",
        "last_ask": 2.1,
        "last_ask_src": "stale_tape",
        "last_ask_age_sec": 67.4,
        "last_ask_ts": time.time() - 67.4,
        "block_code": "stale_quote",
        "structure": {
            "decision": "BUY",
            "entry_low": 2.0,
            "entry_high": 2.2,
            "stop_price": 1.8,
            "target_1": 2.5,
            "wait_kind": "wait_for_zone",
        },
    }
    assert ew.promote_stream_src_if_print_fresh(rec) is False
    assert rec["last_ask_src"] == "stale_tape"
    ok, why = ew.should_arm_buy(
        rec, ask=2.1, bid=2.0,
        cfg={"ai_watch_decision_max_age_sec": 15.0, "desk_product": "scalp_legacy"},
        now=time.time(),
    )
    assert ok is False
    assert why == "stale_quote"


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


def test_live_quote_for_prefers_finnhub_trade_ts_not_learn_time(monkeypatch):
    """Finnhub age must use trade_ts, not ts_unix (learn time).

    Preferring ts_unix made every remembered print look age≈0 — desk stream
    while poller STATE (trade_ts age) correctly said stale_tape.
    """
    import dashboard as d

    monkeypatch.setattr(d, "_load_signal_state", lambda: {"tickers": {}})
    now = time.time()
    trade_ts = now - 40.0
    with d.STATE.lock:
        d.STATE.tickers.pop("THIN", None)
    with d.FINNHUB_STATE.lock:
        d.FINNHUB_STATE.prices["THIN"] = {
            "price": 3.5,
            "ts_unix": now,          # just learned / still in memory
            "trade_ts": trade_ts,    # print itself is 40s old
        }
    with d._alpaca_cache_lock:
        d._alpaca_price_cache.pop("THIN", None)
    px, age = d._live_quote_for("THIN", now=now)
    assert px == 3.5
    assert age is not None and age >= 39.0
    # Undated Finnhub (REST) must not claim a young age via ts_unix alone.
    with d.FINNHUB_STATE.lock:
        d.FINNHUB_STATE.prices["THIN"] = {
            "price": 3.5,
            "ts_unix": now,
            "trade_ts": None,
        }
    px2, age2 = d._live_quote_for("THIN", now=now)
    assert px2 is None and age2 is None


def test_overlay_wall_stamp_keeps_near_ceiling_young_print(monkeypatch):
    """Class C: early *now* must not self-invalidate a ≤ceiling overlay print.

    Overlay measures age against an early now, then apply_tape_blocker
    recomputes via row_quote_age_sec(wall). Stamping last_ask_ts = now - age
    lets paint latency push a 14s eng print over 15s → honesty→stale_tape.
    Wall stamp (time.time() - age) keeps the decided age.
    """
    import dashboard as d

    monkeypatch.setattr(d, "_live_quote_for", lambda sym, now=None: (51.59, 14.0))
    monkeypatch.setattr(
        "ai_entry_watch.decision_max_age_sec", lambda cfg=None: 15.0, raising=False)

    early = time.time() - 2.5  # simulate ~2.5s of work before overlay finishes
    payload = {
        "entry_book": [{
            "symbol": "GTLB",
            "entry_low": 50.0,
            "entry_high": 52.0,
            "stop_price": 48.0,
            "last_ask": 51.0,
            "last_ask_src": "rest",
            "last_ask_age_sec": 19.0,
            "last_ask_ts": time.time() - 19.0,
            "block_code": "stale_quote",
            "phase": "watching",
        }],
        "entry_watch": [],
    }
    out = d.overlay_ai_book_live_prices(payload, now=early)
    row = out["entry_book"][0]
    assert row["last_ask_src"] == "stream"
    assert float(row["last_ask_age_sec"]) == 14.0
    assert row.get("block_code") != "stale_quote"
    # Recomputed wall age must stay ≤ ceiling (the Class C failure mode).
    import ai_entry_watch as ew
    age = ew.row_quote_age_sec(row)
    assert age is not None and age <= 15.0
