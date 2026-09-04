"""Levered ETP filter on non-movers seeds + stale_timeout watch drop.

2026-09-04: MST sat on the AI watch book from 09:30 via the momentum seed
(movers already filtered levered ETPs). Same class of names can also stick
forever on stale_tape / need-stream when Finnhub never delivers a live trade.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402
import ticker_filters as tf  # noqa: E402
from config import DEFAULT_CONFIG  # noqa: E402


# ── shared helper ─────────────────────────────────────────────────────────

def test_ticker_filters_matches_movers_reexport():
    import movers_screener as ms

    for sym in ("MST", "MSTX", "TSLL", "CONL", "NVDL", "CIFX"):
        assert tf.is_levered_etp(sym) and ms.is_levered_etp(sym)
    for sym in ("MSTR", "CIFR", "BULL", "AAPL", "TSLA"):
        assert not tf.is_levered_etp(sym) and not ms.is_levered_etp(sym)


# ── A: ETP on non-movers seed / admit ─────────────────────────────────────

def _seed_cfg(**over):
    c = {
        "ai_watch_seed_momentum": False,
        "ai_watch_seed_momentum_open": False,
        "ai_watch_seed_trending": False,
        "ai_watch_seed_movers": False,
        "ai_watch_seed_research": False,
        "ai_watch_seed_bb_live": False,
        "ai_max_price": 100.0,
        "ai_watch_min_rvol": 0.0,
        "ai_watch_open_seed_min_pct": 0.0,
        "ai_watch_min_pct_change": 0.0,
        "ai_watch_trending_min_score": 0.0,
        "ai_watch_trending_min_pct_change": 0.0,
        "ai_watch_trending_min_rvol": 0.0,
        "ai_watch_require_look_ext": False,
    }
    c.update(over)
    return c


def test_momentum_seed_skips_levered_etp(monkeypatch):
    """MST-class must not reach the shortlist via momentum flags."""
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": "MST", "price": 8.0, "pct_change": 40.0, "rvol": 5.0,
         "find_it_first": True, "mention_window": 1},
        {"ticker": "AAPL", "price": 180.0, "pct_change": 3.0, "rvol": 2.0,
         "find_it_first": True, "mention_window": 1},
    ])
    rows = ew.desk_candidate_rows(_seed_cfg(
        ai_watch_seed_momentum=True, ai_watch_seed_momentum_n=12,
        ai_max_price=500.0,
    ))
    syms = {r["symbol"] for r in rows}
    assert "MST" not in syms
    assert "AAPL" in syms


def test_momentum_open_seed_skips_levered_etp(monkeypatch, tmp_path):
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": "TSLL", "price": 12.0, "pct_change": 8.0, "rvol": 4.0},
        {"ticker": "SOFI", "price": 10.0, "pct_change": 6.0, "rvol": 3.0},
    ])
    (tmp_path / "trending_stocks.json").write_text(
        json.dumps({"rows": []}), encoding="utf-8")
    rows = ew.desk_candidate_rows(_seed_cfg(
        ai_watch_seed_momentum_open=True, ai_watch_seed_momentum_open_n=10,
    ))
    syms = {r["symbol"] for r in rows}
    assert "TSLL" not in syms
    assert "SOFI" in syms


def test_trending_seed_skips_levered_etp(monkeypatch, tmp_path):
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    (tmp_path / "trending_stocks.json").write_text(json.dumps({
        "rows": [
            {"symbol": "MSTX", "trending_score": 20.0, "pct_change": 25.0,
             "rvol": 5.0, "price": 15.0, "is_equity": True},
            {"symbol": "NVDA", "trending_score": 18.0, "pct_change": 4.0,
             "rvol": 3.0, "price": 120.0, "is_equity": True},
        ],
    }), encoding="utf-8")
    rows = ew.desk_candidate_rows(_seed_cfg(
        ai_watch_seed_trending=True, ai_watch_seed_trending_n=20,
        ai_max_price=500.0,
    ))
    syms = {r["symbol"] for r in rows}
    assert "MSTX" not in syms
    assert "NVDA" in syms


def test_research_seed_skips_levered_etp(monkeypatch):
    monkeypatch.setattr(ew, "research_candidate_rows", lambda: [
        {"symbol": "CONL", "source": "xai", "reason": "levered thesis"},
        {"symbol": "PLTR", "source": "xai", "reason": "real thesis"},
    ])
    monkeypatch.setattr(ew, "_live_quote_map", lambda: (
        {"CONL": {"price": 40.0, "pct_change": 5.0, "rvol": 3.0},
         "PLTR": {"price": 30.0, "pct_change": 2.0, "rvol": 2.0}},
        {},
    ))
    rows = ew.desk_candidate_rows(_seed_cfg(
        ai_watch_seed_research=True, ai_watch_seed_research_n=12,
        ai_max_price=100.0,
    ))
    syms = {r["symbol"] for r in rows}
    assert "CONL" not in syms
    assert "PLTR" in syms


def test_passes_inclusion_refuses_levered_etp(monkeypatch):
    monkeypatch.setattr(
        "float_feed.float_shares", lambda s: 5.0, raising=False)
    ok, _met, why = ew.passes_inclusion(
        {"symbol": "MST", "price": 8.0, "pct_change": 20.0, "rvol": 5.0,
         "criteria": ["mom_open"], "dollar_volume": 5e6},
        {"ai_watch_require_uptrend": False, "ai_watch_min_price": 1.0,
         "ai_min_dollar_volume": 0.0, "ai_watch_max_float_m": 0,
         "ai_watch_admit_max_tape_age_sec": 0},
    )
    assert ok is False
    assert why == "levered_etp"


# ── B: stale_timeout drop ────────────────────────────────────────────────

def test_stale_timeout_knobs_default_around_six_minutes():
    assert DEFAULT_CONFIG["ai_watch_stale_timeout_sec"] == 360.0
    assert DEFAULT_CONFIG["ai_watch_stale_timeout_grace_sec"] == 90.0
    assert DEFAULT_CONFIG["ai_watch_stale_timeout_reseed_sec"] == 300.0
    assert DEFAULT_CONFIG["ai_watch_stale_timeout_include_need_stream"] is False
    assert DEFAULT_CONFIG["ai_watch_stale_timeout_quiet_max_sec"] == 180.0
    assert DEFAULT_CONFIG["ai_watch_no_trade_after_subscribe_sec"] == 300.0
    assert DEFAULT_CONFIG["ai_watch_admit_max_tape_age_sec"] == 120.0
    assert ew.stale_timeout_sec({}) == 360.0
    assert ew.stale_timeout_reseed_sec({}) == 300.0
    assert ew.stale_timeout_grace_sec({}) == 90.0
    assert ew.stale_timeout_quiet_max_sec({}) == 180.0
    assert ew.stale_timeout_sec({"ai_watch_stale_timeout_sec": 0}) == 0.0


def _drop_cfg(**over):
    c = {
        "ai_watch_stale_timeout_sec": 360.0,
        "ai_watch_stale_timeout_grace_sec": 0.0,  # tests control the clock
        "ai_watch_stale_timeout_reseed_sec": 300.0,
        "ai_watch_stale_timeout_include_need_stream": False,
        # 0 = any dated tape is still "quiet"; tests that want a true dead
        # drop set age high or leave this 0 and use src=none.
        "ai_watch_stale_timeout_quiet_max_sec": 0.0,
    }
    c.update(over)
    return c


def test_stale_timeout_drop_after_n_minutes(tmp_path, monkeypatch):
    """A name stuck on stale_tape with no young trade_ts is dropped."""
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._STALE_TIMEOUT_UNTIL.clear()
    t0 = 1_000_000.0
    ew.save_watch({
        "DEAD": {
            "symbol": "DEAD",
            "status": "watching",
            "last_ask": 5.0,
            "last_ask_src": "stale_tape",
            "price_src": "stale_tape",
            "admit_ts": t0 - 1000.0,
            "stale_feed_since": t0 - 400.0,
        },
    })
    monkeypatch.setattr(ew, "row_quote_age_sec", lambda *_a, **_k: 400.0)

    class _CP:
        @staticmethod
        def log_event(kind, **kw):
            return {"kind": kind, **kw}

    class _GT:
        @staticmethod
        def has_open_position(_sym):
            return False

    rec = ew.load_watch()["DEAD"]
    events: list = []
    dropped = ew._maybe_stale_timeout_drop(
        rec, sym="DEAD", px_src="stale_tape",
        cfg=_drop_cfg(),
        now=t0, events=events, cp=_CP, gt=_GT,
    )
    assert dropped is True
    assert any(e.get("reason") == "stale_timeout" for e in events)
    assert "DEAD" not in ew.load_watch()
    assert ew._stale_timeout_blocked("DEAD", t0 + 10.0)


def test_stale_timeout_does_not_drop_open_position(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._STALE_TIMEOUT_UNTIL.clear()
    t0 = 2_000_000.0
    rec = {
        "symbol": "HELD",
        "status": "watching",
        "last_ask_src": "stale_tape",
        "admit_ts": t0 - 1000.0,
        "stale_feed_since": t0 - 900.0,
    }
    ew.save_watch({"HELD": dict(rec)})
    monkeypatch.setattr(ew, "row_quote_age_sec", lambda *_a, **_k: 900.0)

    class _CP:
        @staticmethod
        def log_event(kind, **kw):
            return {"kind": kind, **kw}

    class _GT:
        @staticmethod
        def has_open_position(sym):
            return sym == "HELD"

    events: list = []
    dropped = ew._maybe_stale_timeout_drop(
        rec, sym="HELD", px_src="stale_tape",
        cfg=_drop_cfg(),
        now=t0, events=events, cp=_CP, gt=_GT,
    )
    assert dropped is False
    assert not events
    assert "HELD" in ew.load_watch()


def test_need_stream_does_not_count_by_default(monkeypatch):
    """Brief need-stream after admit must not start the dead-tape clock."""
    monkeypatch.setattr(ew, "row_quote_age_sec", lambda *_a, **_k: 120.0)
    rec = {"symbol": "QUIET", "block_code": "stream_required",
           "admit_ts": 1_000_000.0 - 200.0}
    cfg = {"ai_watch_arm_require_stream_price": True,
           "ai_watch_stream_max_age_sec": 10.0,
           "ai_watch_stale_timeout_include_need_stream": False}
    assert not ew._stale_feed_condition(rec, "rest", cfg, now=1_000_000.0)
    # Opt-in still available for desks that want it.
    cfg["ai_watch_stale_timeout_include_need_stream"] = True
    assert ew._stale_feed_condition(rec, "rest", cfg, now=1_000_000.0)


def test_stale_timeout_grace_blocks_instant_clock(tmp_path, monkeypatch):
    """Newly admitted names get subscribe grace before the clock starts."""
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._STALE_TIMEOUT_UNTIL.clear()
    t0 = 3_000_000.0
    rec = {
        "symbol": "NEW",
        "status": "watching",
        "last_ask_src": "stale_tape",
        "admit_ts": t0 - 30.0,  # only 30s on book
        "stale_feed_since": t0 - 30.0,
    }
    monkeypatch.setattr(ew, "row_quote_age_sec", lambda *_a, **_k: 400.0)

    class _CP:
        @staticmethod
        def log_event(kind, **kw):
            return {"kind": kind, **kw}

    class _GT:
        @staticmethod
        def has_open_position(_sym):
            return False

    events: list = []
    dropped = ew._maybe_stale_timeout_drop(
        rec, sym="NEW", px_src="stale_tape",
        cfg=_drop_cfg(ai_watch_stale_timeout_grace_sec=90.0),
        now=t0, events=events, cp=_CP, gt=_GT,
    )
    assert dropped is False
    assert "stale_feed_since" not in rec  # clock cleared during grace


def test_stale_timeout_refuses_reseed_while_tape_dead(monkeypatch):
    import time as _time
    ew._STALE_TIMEOUT_UNTIL.clear()
    ew._RESEED_STREAM_CLEARED.clear()
    now = _time.time()
    ew._mark_stale_timeout_block("ZZZ", now, {
        "ai_watch_stale_timeout_reseed_sec": 600.0,
    })
    monkeypatch.setattr(ew, "live_print", lambda *_a, **_k: None)
    ok, _met, why = ew.passes_inclusion(
        {"symbol": "ZZZ", "price": 5.0, "pct_change": 10.0, "rvol": 4.0,
         "criteria": ["mom_open"], "dollar_volume": 2e6},
        {"ai_watch_require_uptrend": False, "ai_watch_min_price": 1.0,
         "ai_min_dollar_volume": 0.0, "ai_watch_max_float_m": 0,
         "ai_watch_admit_max_tape_age_sec": 0},
    )
    assert ok is False and why == "stale_timeout_reseed_block"
    ew._STALE_TIMEOUT_UNTIL.clear()


def test_reseed_allowed_when_young_stream_appears(monkeypatch):
    """30m cool must not starve a name that now has a live Finnhub print."""
    import time as _time
    ew._STALE_TIMEOUT_UNTIL.clear()
    ew._RESEED_STREAM_CLEARED.clear()
    now = _time.time()
    ew._mark_stale_timeout_block("BACK", now, {
        "ai_watch_stale_timeout_reseed_sec": 1800.0,  # old hungry cool
    })
    assert "BACK" in ew._STALE_TIMEOUT_UNTIL
    monkeypatch.setattr(ew, "live_print", lambda *_a, **_k: (12.5, 1.5))
    row = {"symbol": "BACK", "price": 12.5, "pct_change": 8.0, "rvol": 3.0,
           "criteria": ["mom_open"], "dollar_volume": 3e6,
           "last_ask_ts": now - 1.5, "last_ask_src": "stream"}
    assert ew._stale_timeout_blocked(
        "BACK", now + 10.0, cfg={"ai_watch_stream_max_age_sec": 10.0}, row=row
    ) is False
    assert "BACK" not in ew._STALE_TIMEOUT_UNTIL
    assert ew._consume_reseed_stream_clear("BACK") is True

    ok, met, why = ew.passes_inclusion(
        row,
        {"ai_watch_require_uptrend": False, "ai_watch_min_price": 1.0,
         "ai_min_dollar_volume": 0.0, "ai_watch_max_float_m": 0,
         "ai_watch_admit_max_tape_age_sec": 0},
    )
    # Cool already cleared; admit proceeds (may still fail other gates —
    # at least not reseed_block).
    assert why != "stale_timeout_reseed_block"
    assert why != "stale_timeout"


def test_young_trade_ts_prevents_stale_timeout_condition():
    rec = {"symbol": "LIVE", "last_ask_ts": 1_000_000.0 - 2.0}
    assert ew._young_trade_ts(rec, 1_000_000.0, {"ai_watch_stream_max_age_sec": 10})
    assert not ew._stale_feed_condition(
        rec, "stale_tape", {}, now=1_000_000.0)


def test_sync_drops_levered_etp_already_on_book(tmp_path, monkeypatch):
    """MST left on the book from a prior momentum seed must leave on sync."""
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew.save_watch({
        "MST": {
            "symbol": "MST",
            "status": "watching",
            "source": "momentum",
            "last_ask": 7.0,
            "structure": {"entry_low": 6.0, "entry_high": 7.5},
            "structure_ts": 1.0,
            "last_candidate_ts": 1.0,
        },
        "AAPL": {
            "symbol": "AAPL",
            "status": "watching",
            "source": "momentum",
            "last_ask": 180.0,
            "structure": {"entry_low": 175.0, "entry_high": 182.0},
            "structure_ts": 1.0,
            "last_candidate_ts": 1.0,
        },
    })
    monkeypatch.setattr(ew, "desk_candidate_rows", lambda cfg=None: [
        {"symbol": "AAPL", "source": "momentum", "price": 180.0,
         "pct_change": 2.0, "rvol": 2.0, "criteria": ["mom_open"],
         "agreement": True, "score": 2.0, "reason": "desk"},
    ])
    monkeypatch.setattr(ew, "apply_inclusion_gate",
                        lambda rows, cfg, indicators=None: (rows, []))
    monkeypatch.setattr(ew, "push_candidates_to_engine", lambda *_a, **_k: None)
    monkeypatch.setattr(ew, "_engine_indicator_map", lambda: {})
    monkeypatch.setattr(ew, "_log_rejects", lambda *_a, **_k: None)

    state = ew.sync_watch_from_source_panels(
        cfg={"ai_watch_admit_grace_sec": 0}, now=10_000.0)
    assert "MST" not in state
    assert "AAPL" in state


def test_no_trade_after_subscribe_drops_aehg_class(tmp_path, monkeypatch):
    """Finnhub-book name with only a 10+ min-old print is dropped after N min."""
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._STALE_TIMEOUT_UNTIL.clear()
    t0 = 5_000_000.0
    ew.save_watch({
        "AEHG": {
            "symbol": "AEHG",
            "status": "watching",
            "last_ask": 6.85,
            "last_ask_src": "stale_tape",
            "admit_ts": t0 - 600.0,  # 10 min on book
            "block_code": "stale_quote",
        },
    })
    monkeypatch.setattr(ew, "live_print", lambda *_a, **_k: (6.85, 850.0))
    monkeypatch.setattr(ew, "row_quote_age_sec", lambda *_a, **_k: 850.0)

    class _CP:
        @staticmethod
        def log_event(kind, **kw):
            return {"kind": kind, **kw}

    class _GT:
        @staticmethod
        def has_open_position(_sym):
            return False

    rec = ew.load_watch()["AEHG"]
    events: list = []
    dropped = ew._maybe_no_trade_after_subscribe_drop(
        rec, sym="AEHG",
        cfg={
            "ai_watch_no_trade_after_subscribe_sec": 300.0,
            "ai_watch_stale_timeout_grace_sec": 90.0,
            "ai_watch_stream_subscribe_grace_sec": 90.0,
            "ai_watch_decision_max_age_sec": 15.0,
            "ai_watch_stale_timeout_reseed_sec": 300.0,
        },
        now=t0, events=events, cp=_CP, gt=_GT,
    )
    assert dropped is True
    assert any(e.get("reason") == "no_stream_trade" for e in events)
    assert "AEHG" not in ew.load_watch()


def test_no_trade_drop_keeps_young_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._STALE_TIMEOUT_UNTIL.clear()
    t0 = 6_000_000.0
    rec = {
        "symbol": "SMCI",
        "status": "watching",
        "last_ask_src": "stream",
        "last_ask_age_sec": 3.0,
        "admit_ts": t0 - 600.0,
    }
    ew.save_watch({"SMCI": dict(rec)})
    monkeypatch.setattr(ew, "live_print", lambda *_a, **_k: (40.0, 3.0))

    class _CP:
        @staticmethod
        def log_event(kind, **kw):
            return {"kind": kind, **kw}

    class _GT:
        @staticmethod
        def has_open_position(_sym):
            return False

    events: list = []
    dropped = ew._maybe_no_trade_after_subscribe_drop(
        rec, sym="SMCI",
        cfg={
            "ai_watch_no_trade_after_subscribe_sec": 300.0,
            "ai_watch_stale_timeout_grace_sec": 90.0,
            "ai_watch_decision_max_age_sec": 15.0,
        },
        now=t0, events=events, cp=_CP, gt=_GT,
    )
    assert dropped is False
    assert "SMCI" in ew.load_watch()


def test_passes_inclusion_refuses_stale_tape_admit(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda *_a, **_k: (12.0, 400.0))
    monkeypatch.setattr(
        "float_feed.float_shares", lambda s: 5.0, raising=False)
    ok, _met, why = ew.passes_inclusion(
        {"symbol": "LABX", "price": 12.0, "pct_change": 18.0, "rvol": 3.0,
         "criteria": ["mom_open"], "dollar_volume": 2e6},
        {"ai_watch_require_uptrend": False, "ai_watch_min_price": 1.0,
         "ai_min_dollar_volume": 0.0, "ai_watch_max_float_m": 0,
         "ai_watch_admit_max_tape_age_sec": 120.0},
    )
    assert ok is False and why == "stale_tape_admit"


def test_passes_inclusion_refuses_no_tape(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "float_feed.float_shares", lambda s: 5.0, raising=False)
    ok, _met, why = ew.passes_inclusion(
        {"symbol": "DARK", "price": 8.0, "pct_change": 20.0, "rvol": 4.0,
         "criteria": ["mom_open"], "dollar_volume": 2e6},
        {"ai_watch_require_uptrend": False, "ai_watch_min_price": 1.0,
         "ai_min_dollar_volume": 0.0, "ai_watch_max_float_m": 0,
         "ai_watch_admit_max_tape_age_sec": 120.0},
    )
    assert ok is False and why == "no_tape"


def test_clear_stale_quote_on_stream_age_le_15():
    now = __import__("time").time()
    rec = {
        "symbol": "SNDG",
        "last_ask_src": "stream",
        "last_ask_age_sec": 8.0,
        "last_ask_ts": now - 8.0,
        "block_code": "stale_quote",
        "block_reason": "stale quote",
    }
    assert ew.clear_tape_data_block_if_stream_fresh(
        rec, {"ai_watch_decision_max_age_sec": 15.0}) is True
    assert rec.get("block_code") is None


def test_no_trade_reseed_longer_than_generic():
    assert DEFAULT_CONFIG["ai_watch_no_trade_reseed_sec"] == 900.0
    assert ew.no_trade_reseed_sec({}) == 900.0
    assert ew.no_trade_reseed_sec({
        "ai_watch_no_trade_reseed_sec": 0,
        "ai_watch_stale_timeout_reseed_sec": 300.0,
    }) == 300.0


def test_no_stream_trade_uses_long_reseed(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._STALE_TIMEOUT_UNTIL.clear()
    t0 = 7_000_000.0
    ew.save_watch({
        "THIN": {
            "symbol": "THIN", "status": "watching",
            "last_ask_src": "stale_tape", "admit_ts": t0 - 600.0,
        },
    })
    monkeypatch.setattr(ew, "live_print", lambda *_a, **_k: (3.0, 400.0))

    class _CP:
        @staticmethod
        def log_event(kind, **kw):
            return {"kind": kind, **kw}

    class _GT:
        @staticmethod
        def has_open_position(_sym):
            return False

    rec = ew.load_watch()["THIN"]
    events: list = []
    assert ew._maybe_no_trade_after_subscribe_drop(
        rec, sym="THIN",
        cfg={
            "ai_watch_no_trade_after_subscribe_sec": 300.0,
            "ai_watch_no_trade_reseed_sec": 900.0,
            "ai_watch_stale_timeout_grace_sec": 90.0,
            "ai_watch_decision_max_age_sec": 15.0,
            "ai_watch_stale_timeout_reseed_sec": 300.0,
        },
        now=t0, events=events, cp=_CP, gt=_GT,
    ) is True
    until = ew._STALE_TIMEOUT_UNTIL.get("THIN", 0)
    assert until >= t0 + 890.0  # long cool, not 300


def test_enforce_stale_tape_seat_cap_drops_worst(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._STALE_TIMEOUT_UNTIL.clear()
    t0 = 8_000_000.0
    state = {
        "KEEP_HI": {
            "symbol": "KEEP_HI", "status": "watching",
            "last_ask_src": "stale_tape", "admit_dollar_volume": 50e6,
            "last_ask_age_sec": 40.0,
        },
        "KEEP_MID": {
            "symbol": "KEEP_MID", "status": "watching",
            "last_ask_src": "stale_tape", "admit_dollar_volume": 10e6,
            "last_ask_age_sec": 50.0,
        },
        "DROP_LO": {
            "symbol": "DROP_LO", "status": "watching",
            "last_ask_src": "stale_tape", "admit_dollar_volume": 100e3,
            "last_ask_age_sec": 200.0,
        },
        "STREAM": {
            "symbol": "STREAM", "status": "watching",
            "last_ask_src": "stream", "last_ask_age_sec": 3.0,
            "admit_dollar_volume": 1e3,
        },
    }
    ew.save_watch(state)
    monkeypatch.setattr(ew, "row_quote_age_sec",
                        lambda rec, now=None: rec.get("last_ask_age_sec"))

    class _CP:
        @staticmethod
        def log_event(kind, **kw):
            return {"kind": kind, **kw}

    class _GT:
        @staticmethod
        def has_open_position(_sym):
            return False

    events: list = []
    dropped = ew._enforce_stale_tape_seat_cap(
        state, cfg={"ai_watch_max_stale_tape_seats": 2,
                    "ai_watch_no_trade_reseed_sec": 900.0},
        now=t0, events=events, cp=_CP, gt=_GT)
    assert dropped == ["DROP_LO"]
    assert "DROP_LO" not in ew.load_watch()
    assert "KEEP_HI" in ew.load_watch() and "STREAM" in ew.load_watch()


def test_passes_inclusion_movers_min_price_and_dvol(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda *_a, **_k: (4.0, 5.0))
    monkeypatch.setattr(
        "float_feed.float_shares", lambda s: 5.0, raising=False)
    cfg = {
        "ai_watch_require_uptrend": False, "ai_watch_min_price": 2.0,
        "ai_min_dollar_volume": 0.0, "ai_watch_max_float_m": 0,
        "ai_watch_movers_min_price": 5.0,
        "ai_watch_movers_min_dollar_volume": 2e6,
        "ai_watch_movers_admit_max_tape_age_sec": 60.0,
        "ai_watch_admit_max_tape_age_sec": 120.0,
    }
    ok, _m, why = ew.passes_inclusion(
        {"symbol": "CBRG", "source": "movers", "price": 4.0,
         "pct_change": 25.0, "rvol": 3.0, "dollar_volume": 5e6,
         "criteria": []},
        cfg)
    assert ok is False and why == "below_min_price"

    monkeypatch.setattr(ew, "live_print", lambda *_a, **_k: (8.0, 5.0))
    ok, _m, why = ew.passes_inclusion(
        {"symbol": "THIN", "source": "movers", "price": 8.0,
         "pct_change": 25.0, "rvol": 3.0, "dollar_volume": 500e3,
         "criteria": []},
        cfg)
    assert ok is False and why == "thin_dollar_volume"
