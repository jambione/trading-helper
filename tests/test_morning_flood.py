"""Morning flood: seat momentum + Trader Bro names 09:30–11:00 ET.

soft_seed_max does not clip square / pre_square momentum during the window.
Far names do not take a keep seat while prefer-square is on. $2 floor still
refuses pennies. Arms unchanged.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import ai_entry_watch as ew
from config import DEFAULT_CONFIG


ET = ZoneInfo("America/New_York")


def _et_ts(hour: int, minute: int = 0, *, day: int = 18) -> float:
    """Weekday ET timestamp (Sep 2026 — Thursday the 18th)."""
    return datetime(2026, 9, day, hour, minute, tzinfo=ET).timestamp()


def _cfg(**over):
    c = {
        "ai_watch_morning_flood_enabled": True,
        "ai_watch_morning_flood_start": "09:30",
        "ai_watch_morning_flood_end": "11:00",
        "ai_watch_morning_flood_include_pre": False,
        "ai_watch_soft_seed_enabled": True,
        "ai_watch_soft_seed_interval_sec": 1.0,
        "ai_watch_soft_seed_max": 12,
        "ai_watch_soft_seed_momentum": True,
        "ai_watch_soft_seed_research": True,
        "ai_watch_soft_seed_movers": False,
        "ai_watch_soft_seed_trending": False,
        "ai_watch_admit_prefer_square": True,
        "ai_watch_admit_require_arm_ready": False,
        "ai_watch_min_price": 2.0,
        "ai_max_price": 100.0,
        "ai_watch_exh_pre_thr": 35.0,
        "rte_threshold": 20,
        "rte_confluence_max": 15.0,
        "ai_watch_scout_ttl_sec": 120.0,
        "ai_watch_far_exh_evict_sec": 45.0,
        "ai_watch_max_far_exh_seats": 0,
        "ai_watch_exh_square_arm": True,
    }
    c.update(over)
    return c


def _far_ind():
    return {
        "pctr": -50.0,
        "pctr_slow": -70.0,
        "pctr_rising": True,
        "pctr_slow_rising": True,
        "pctr_ob": False,
        "pctr_tight": False,
    }


@pytest.fixture(autouse=True)
def _reset_soft_seed_clock():
    ew._SOFT_SEED_LAST_TS = 0.0
    yield
    ew._SOFT_SEED_LAST_TS = 0.0


def test_default_config_bakes_morning_flood_window():
    assert DEFAULT_CONFIG["ai_watch_morning_flood_enabled"] is True
    assert DEFAULT_CONFIG["ai_watch_morning_flood_start"] == "09:30"
    assert DEFAULT_CONFIG["ai_watch_morning_flood_end"] == "11:00"
    assert DEFAULT_CONFIG["ai_watch_morning_flood_include_pre"] is False


def test_morning_flood_active_window():
    cfg = _cfg()
    assert ew.morning_flood_active(cfg, _et_ts(9, 30)) is True
    assert ew.morning_flood_active(cfg, _et_ts(10, 0)) is True
    assert ew.morning_flood_active(cfg, _et_ts(10, 59)) is True
    assert ew.morning_flood_active(cfg, _et_ts(11, 0)) is False
    assert ew.morning_flood_active(cfg, _et_ts(11, 30)) is False
    assert ew.morning_flood_active(cfg, _et_ts(9, 0)) is False
    assert ew.morning_flood_active(
        _cfg(ai_watch_morning_flood_enabled=False), _et_ts(10, 0)
    ) is False
    assert ew.morning_flood_active(
        _cfg(ai_watch_morning_flood_include_pre=True), _et_ts(9, 0)
    ) is True


def test_is_morning_flood_source():
    assert ew.is_morning_flood_source("momentum") is True
    assert ew.is_morning_flood_source("xai") is True
    assert ew.is_morning_flood_source("agy") is True
    assert ew.is_morning_flood_source("movers") is False
    assert ew.is_morning_flood_source(
        {"source": "momentum", "criteria": ["soft_seed", "momentum"]}
    ) is True
    assert ew.is_morning_flood_source(
        {"source": "trending", "criteria": ["soft_seed", "trending"]}
    ) is False


def _pre_ind():
    return {
        "pctr": -28.0,
        "pctr_slow": -30.0,
        "pctr_rising": True,
        "pctr_slow_rising": True,
        "pctr_ob": False,
        "pctr_tight": True,
    }


def test_flood_seats_pre_square_ahead_of_far(monkeypatch):
    """Far momentum does not take a keep seat. Pre-square does, past max."""
    monkeypatch.setattr(ew, "trading_hours_active", lambda *a, **k: True)
    now = _et_ts(10, 0)
    far = [
        {
            "symbol": f"F{i:02d}",
            "source": "momentum",
            "price": 5.0 + i,
            "pct_change": 30.0,
            "dollar_volume": 5e6,
            "criteria": ["soft_seed", "momentum"],
            "indicator": _far_ind(),
        }
        for i in range(20)
    ]
    pre = [
        {
            "symbol": f"P{i:02d}",
            "source": "momentum",
            "price": 8.0,
            "pct_change": 6.0,
            "dollar_volume": 2e6,
            "criteria": ["soft_seed", "momentum"],
            "indicator": _pre_ind(),
        }
        for i in range(14)
    ]
    monkeypatch.setattr(ew, "_soft_seed_source_rows", lambda cfg, now=None: far + pre)
    picked, fired = ew.maybe_soft_seed_rows(
        _cfg(ai_watch_soft_seed_max=12), now=now, seen=set())
    assert fired is True
    syms = {r["symbol"] for r in picked if not r.get("scout_only")}
    assert syms == {f"P{i:02d}" for i in range(14)}
    assert all(r.get("morning_flood") == 1 for r in picked if not r.get("scout_only"))


def test_flood_soft_seed_refuses_below_min_price(monkeypatch):
    monkeypatch.setattr(ew, "trading_hours_active", lambda *a, **k: True)
    now = _et_ts(10, 0)
    monkeypatch.setattr(ew, "_soft_seed_source_rows", lambda cfg, now=None: [
        {
            "symbol": "PENNY",
            "source": "momentum",
            "price": 1.50,
            "pct_change": 40.0,
            "dollar_volume": 2e6,
            "criteria": ["soft_seed", "momentum"],
            "indicator": _far_ind(),
        },
        {
            "symbol": "OK1",
            "source": "momentum",
            "price": 3.0,
            "pct_change": 15.0,
            "dollar_volume": 2e6,
            "criteria": ["soft_seed", "momentum"],
            "indicator": _pre_ind(),
        },
        {
            "symbol": "NOPX",
            "source": "xai",
            "price": None,
            "pct_change": 5.0,
            "criteria": ["soft_seed", "research"],
            "indicator": _far_ind(),
        },
    ])
    picked, fired = ew.maybe_soft_seed_rows(_cfg(), now=now, seen=set())
    assert fired is True
    syms = {r["symbol"] for r in picked}
    assert "OK1" in syms
    assert "PENNY" not in syms
    assert "NOPX" not in syms


def test_after_flood_soft_seed_max_and_prefer_square_apply(monkeypatch):
    """11:30 ET → soft_seed_max and prefer-square far refuse return."""
    monkeypatch.setattr(ew, "trading_hours_active", lambda *a, **k: True)
    now = _et_ts(11, 30)
    rows = [
        {
            "symbol": f"M{i:02d}",
            "source": "momentum",
            "price": 5.0,
            "pct_change": 12.0,
            "dollar_volume": 5e6,
            "criteria": ["soft_seed", "momentum"],
            "indicator": _far_ind(),
        }
        for i in range(20)
    ]
    monkeypatch.setattr(ew, "_soft_seed_source_rows", lambda cfg, now=None: rows)
    picked, fired = ew.maybe_soft_seed_rows(
        _cfg(ai_watch_soft_seed_max=12), now=now, seen=set())
    assert fired is True
    # Far + prefer_square → no keep seats from this far-only batch.
    assert picked == [] or all(
        r.get("scout_only") is True or r.get("exh_seat_class") != "far"
        for r in picked
    )
    assert all(r.get("morning_flood") != 1 for r in picked)
    assert len(picked) <= 12


def test_flood_off_identical_to_prefer_square(monkeypatch):
    monkeypatch.setattr(ew, "trading_hours_active", lambda *a, **k: True)
    now = _et_ts(10, 0)
    rows = [
        {
            "symbol": "FAR1",
            "source": "momentum",
            "price": 10.0,
            "pct_change": 20.0,
            "dollar_volume": 5e6,
            "criteria": ["soft_seed", "momentum"],
            "indicator": _far_ind(),
        },
        {
            "symbol": "PRE1",
            "source": "momentum",
            "price": 11.0,
            "pct_change": 12.0,
            "dollar_volume": 3e6,
            "criteria": ["soft_seed", "momentum"],
            "indicator": {
                "pctr": -30.0, "pctr_slow": -28.0,
                "pctr_rising": True, "pctr_slow_rising": True,
            },
        },
    ]
    monkeypatch.setattr(ew, "_soft_seed_source_rows", lambda cfg, now=None: rows)
    picked, fired = ew.maybe_soft_seed_rows(
        _cfg(ai_watch_morning_flood_enabled=False, ai_watch_soft_seed_max=12),
        now=now, seen=set())
    assert fired is True
    syms = {r["symbol"] for r in picked}
    assert "PRE1" in syms
    assert "FAR1" not in syms


def test_far_exh_evict_skipped_for_flood_momentum(monkeypatch):
    """Blind far_exh drop still skips flood; steal path yields to pre_square."""
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda s: dropped.extend(s))
    now = _et_ts(10, 0)
    rec = {
        "symbol": "MOMFAR",
        "source": "momentum",
        "status": "watching",
        "admit_ts": now - 600.0,
        "far_exh_since": now - 200.0,
        "exh_seat_class": "far",
        "indicator": _far_ind(),
        "morning_flood": 1,
    }

    class _CP:
        def log_event(self, kind, **kw):
            return {"kind": kind, **kw}

    class _GT:
        def has_open_position(self, sym):
            return False

    assert ew._maybe_far_exh_evict(
        rec, sym="MOMFAR", cfg=_cfg(), now=now,
        events=[], cp=_CP(), gt=_GT(),
    ) is False
    assert dropped == []

    # After 11:00, far eviction resumes.
    later = _et_ts(11, 30)
    rec["far_exh_since"] = later - 200.0
    assert ew._maybe_far_exh_evict(
        rec, sym="MOMFAR", cfg=_cfg(), now=later,
        events=[], cp=_CP(), gt=_GT(),
    ) is True
    assert dropped == ["MOMFAR"]


def test_flood_far_stealable_for_pre_square(monkeypatch):
    """Flood still seats far names, but they yield ASAP to pre_square/square."""
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda s: dropped.extend(s))
    now = _et_ts(10, 0)
    state = {
        "MOMFAR": {
            "symbol": "MOMFAR",
            "source": "momentum",
            "status": "watching",
            "admit_ts": now - 600.0,
            "far_exh_since": now - 20.0,  # past min(45,15)=15 flood steal gate
            "exh_seat_class": "far",
            "indicator": _far_ind(),
            "morning_flood": 1,
            "admit_dollar_volume": 1e5,
        }
    }
    cands = [{
        "symbol": "PRE1",
        "source": "momentum",
        "exh_seat_class": "pre_square",
        "indicator": {
            "pctr": -32.0, "pctr_slow": -30.0,
            "pctr_rising": True, "pctr_slow_rising": True,
        },
        "dollar_volume": 5e6,
        "last_ask_src": "stream",
        "last_ask_age_sec": 1.0,
    }]

    class _CP:
        def log_event(self, kind, **kw):
            return {"kind": kind, **kw}

    class _GT:
        def has_open_position(self, sym):
            return False

    events = []
    got = ew._preferential_far_exh_steal(
        state, cfg=_cfg(), now=now, events=events,
        cp=_CP(), gt=_GT(), candidates=cands,
    )
    assert got == ["MOMFAR"]
    assert events[0]["reason"] == "far_exh_steal_for_pre_square"
    # Far flood seat is stealable (unarmable path) past TTL too.
    assert ew._is_unarmable_stale_watching(state["MOMFAR"], _cfg(), now=now) is True


def test_square_arm_still_refuses_non_ob_tight_morning_seats():
    """Flood seats the book; arms still require dual OB+tight square."""
    cfg = _cfg()
    far = {"indicator": _far_ind()}
    ok, why = ew.exhaustion_allows_buy(far, cfg)
    assert ok is False
    assert why in ("exh_not_tight", "wait_exh", "exh_not_ob", "not_overbought")

    square = {"indicator": {
        "pctr": -13.0, "pctr_slow": -4.0,
        "pctr_rising": True, "pctr_slow_rising": True,
        "pctr_ob": True, "pctr_tight": True,
        "pctr_falling": False,
    }}
    ok, why = ew.exhaustion_allows_buy(square, cfg)
    assert ok is True and why == "overbought"


def test_inclusion_below_min_price_unchanged():
    cfg = _cfg(
        ai_watch_require_uptrend=False,
        ai_watch_require_indicators=False,
        ai_min_dollar_volume=0.0,
        ai_watch_max_float_m=0,
        ai_watch_min_rvol=0.0,
        ai_watch_admit_max_tape_age_sec=0,
    )
    ok, _met, why = ew.passes_inclusion(
        {"symbol": "PENNY", "source": "momentum", "price": 1.50,
         "pct_change": 30.0, "criteria": [], "morning_flood": 1},
        cfg,
    )
    assert ok is False
    assert why == "below_min_price"


def test_desk_seed_n_caps_lifted_during_flood(monkeypatch):
    now = _et_ts(10, 0)
    monkeypatch.setattr(ew, "morning_flood_active", lambda cfg, now=None: True)
    monkeypatch.setattr(ew, "_live_quote_map", lambda: ({}, {}))
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [])
    monkeypatch.setattr(ew, "_momentum_flagged_from_dashboard", lambda max_price: [
        (10.0, {
            "symbol": f"F{i:02d}", "source": "momentum", "price": 5.0 + i,
            "pct_change": 20.0, "criteria": ["flag"], "score": 10.0,
            "reason": "flag", "agreement": True,
        })
        for i in range(18)
    ])
    monkeypatch.setattr(ew, "_big_mover_from_dashboard", lambda *a, **k: [])
    monkeypatch.setattr(ew, "research_candidate_rows", lambda: [
        {"symbol": f"X{i:02d}", "source": "xai", "score": 8.0, "reason": "t"}
        for i in range(15)
    ])
    # Enrich research prices via live quote map override.
    quotes = {
        f"X{i:02d}": {"price": 6.0 + i, "pct_change": 3.0, "rvol": 2.0,
                      "day_vol": 1e6}
        for i in range(15)
    }
    monkeypatch.setattr(ew, "_live_quote_map", lambda: (quotes, {}))

    rows = ew.desk_candidate_rows({
        "ai_watch_seed_momentum": True,
        "ai_watch_seed_momentum_n": 12,
        "ai_watch_seed_momentum_open": False,
        "ai_watch_seed_trending": False,
        "ai_watch_seed_movers": False,
        "ai_watch_seed_research": True,
        "ai_watch_seed_research_n": 12,
        "ai_watch_seed_bb_live": False,
        "ai_watch_min_price": 2.0,
        "ai_max_price": 100.0,
        "ai_watch_morning_flood_enabled": True,
    })
    syms = {r["symbol"] for r in rows}
    assert len([s for s in syms if s.startswith("F")]) == 18
    assert len([s for s in syms if s.startswith("X")]) == 15
    assert all(r.get("morning_flood") == 1 for r in rows)
    _ = now  # silence lint; flood monkeypatched
