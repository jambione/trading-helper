"""Arm-ready book admissions: soft-seed keep gate, eviction, CHG% band, logging."""
from __future__ import annotations

import time

import pytest

import ai_entry_watch as ew
from config import DEFAULT_CONFIG


def _cfg(**over):
    c = {
        "ai_watch_admit_require_arm_ready": True,
        "ai_watch_admit_arm_ready_rth_only": False,  # force on in tests
        # Isolate arm-ready soft-seed from square-prefer bus (own suite).
        "ai_watch_arm_cm_rsi_max": 75.0,
        "ai_watch_require_exh_rising": True,
        "ai_watch_exhaustion_heat_min_pct": 40.0,
        "ai_max_price": 100.0,
        "ai_watch_decision_max_age_sec": 15.0,
        "ai_watch_admit_max_tape_age_sec": 120.0,
        "ai_watch_admit_chg_prefer_min": 8.0,
        "ai_watch_admit_chg_prefer_max": 40.0,
        "ai_watch_admit_chg_soft_max": 50.0,
        "ai_watch_unarmable_evict_sec": 90.0,
        "ai_watch_scout_ttl_sec": 120.0,
        "ai_watch_warming_exh_min": 15.0,
        "ai_watch_warming_exh_max": 45.0,
        # Isolate from wall-clock morning flood (09:30–11 ET).
        "ai_watch_morning_flood_enabled": False,
    }
    c.update(over)
    return c


def _ready_row(**over):
    row = {
        "symbol": "READY",
        "source": "trending",
        "soft_seed": True,
        "criteria": ["soft_seed", "trending"],
        "price": 12.0,
        "pct_change": 18.0,
        "last_ask_src": "stream",
        "last_ask_age_sec": 2.0,
        "indicator": {
            "cm_rsi": 42.0,
            "cm_rsi_rising": True,
            "pctr": -45.0,  # heat 55
            "pctr_rising": True,
            "pctr_falling": False,
        },
    }
    row.update(over)
    return row


# ── evaluate_arm_ready accept / reject ─────────────────────────────────────

def test_arm_ready_accepts_young_tape_rsi_rising_exh_heat(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda s: (12.0, 1.0))
    ok, why = ew.evaluate_arm_ready(_ready_row(), _cfg())
    assert ok is True
    assert why == "ok"


def test_arm_ready_rejects_stale_tape(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda s: None)
    ok, why = ew.evaluate_arm_ready(
        _ready_row(last_ask_src="stale_tape", last_ask_age_sec=400.0),
        _cfg(),
    )
    assert ok is False
    assert why == "stale"


def test_arm_ready_rejects_rsi_extended(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda s: (12.0, 1.0))
    row = _ready_row()
    row["indicator"]["cm_rsi"] = 80.0
    ok, why = ew.evaluate_arm_ready(row, _cfg())
    assert ok is False
    assert why == "rsi_extended"


def test_arm_ready_rejects_exh_falling(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda s: (12.0, 1.0))
    row = _ready_row()
    row["indicator"]["pctr_rising"] = False
    row["indicator"]["pctr_falling"] = True
    ok, why = ew.evaluate_arm_ready(row, _cfg())
    assert ok is False
    assert why == "exh_falling"


def test_arm_ready_rejects_exh_too_low(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda s: (12.0, 1.0))
    row = _ready_row()
    row["indicator"]["pctr"] = -80.0  # heat 20 < 40
    ok, why = ew.evaluate_arm_ready(row, _cfg())
    assert ok is False
    assert why == "exh_too_low"


def test_arm_ready_rejects_above_max_price(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda s: (150.0, 1.0))
    ok, why = ew.evaluate_arm_ready(
        _ready_row(price=150.0), _cfg(ai_max_price=100.0))
    assert ok is False
    assert why == "above_max_price"


def test_stamp_arm_ready_fields_logging():
    row = _ready_row()
    # Force stale without live_print dependency via block_code.
    row["block_code"] = "stale_quote"
    ready, why = ew.stamp_arm_ready_fields(row, _cfg())
    assert ready is False
    assert row["arm_ready"] is False
    assert row["arm_ready_reason"] == "stale"
    assert row["admit_chg_band"] == "prefer"
    assert row["admit_pct_change"] == 18.0


# ── inclusion gate ─────────────────────────────────────────────────────────

def test_inclusion_rejects_soft_seed_not_arm_ready(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda s: (12.0, 1.0))
    monkeypatch.setattr(ew, "is_levered_etp", lambda s: False)
    monkeypatch.setattr(ew, "_dead_reentry_blocked", lambda *a, **k: False)
    monkeypatch.setattr(ew, "_stale_timeout_blocked", lambda *a, **k: False)
    monkeypatch.setattr(ew, "_no_stream_strike_demoted", lambda *a, **k: False)
    monkeypatch.setattr(ew, "_consume_reseed_stream_clear", lambda *a, **k: False)

    row = _ready_row()
    row["indicator"]["cm_rsi"] = 80.0
    cfg = _cfg(
        ai_watch_require_uptrend=True,
        ai_watch_require_indicators=False,
        ai_watch_min_price=1.0,
        ai_min_dollar_volume=0.0,
        ai_watch_max_float_m=0.0,
        ai_watch_admit_ticks=1,
    )
    ok, met, why = ew.passes_inclusion(row, cfg, indicators={})
    assert ok is False
    assert why.startswith("arm_ready_")
    assert "rsi_extended" in why


def test_inclusion_keeps_arm_ready_soft_seed(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda s: (12.0, 1.0))
    monkeypatch.setattr(ew, "is_levered_etp", lambda s: False)
    monkeypatch.setattr(ew, "_dead_reentry_blocked", lambda *a, **k: False)
    monkeypatch.setattr(ew, "_stale_timeout_blocked", lambda *a, **k: False)
    monkeypatch.setattr(ew, "_no_stream_strike_demoted", lambda *a, **k: False)
    monkeypatch.setattr(ew, "_consume_reseed_stream_clear", lambda *a, **k: False)

    row = _ready_row()
    cfg = _cfg(
        ai_watch_require_uptrend=True,
        ai_watch_require_indicators=False,
        ai_watch_min_price=1.0,
        ai_min_dollar_volume=0.0,
        ai_watch_max_float_m=0.0,
        ai_watch_admit_ticks=1,
        ai_watch_require_look_ext=False,
    )
    ok, met, why = ew.passes_inclusion(row, cfg, indicators={})
    assert ok is True, why
    assert why == ""
    assert row.get("arm_ready") is True
    assert "arm_ready" in met


def test_inclusion_allows_scout_only_without_arm_ready(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda s: (12.0, 1.0))
    monkeypatch.setattr(ew, "is_levered_etp", lambda s: False)
    monkeypatch.setattr(ew, "_dead_reentry_blocked", lambda *a, **k: False)
    monkeypatch.setattr(ew, "_stale_timeout_blocked", lambda *a, **k: False)
    monkeypatch.setattr(ew, "_no_stream_strike_demoted", lambda *a, **k: False)
    monkeypatch.setattr(ew, "_consume_reseed_stream_clear", lambda *a, **k: False)

    row = _ready_row(
        scout_only=True,
        seat_role="warming",
        criteria=["soft_seed", "warming", "scout_only"],
    )
    row["indicator"]["cm_rsi_rising"] = False
    row["indicator"]["pctr"] = -70.0  # warming band heat ~30
    cfg = _cfg(
        ai_watch_require_uptrend=True,
        ai_watch_require_indicators=False,
        ai_watch_min_price=1.0,
        ai_min_dollar_volume=0.0,
        ai_watch_max_float_m=0.0,
        ai_watch_require_look_ext=False,
    )
    ok, met, why = ew.passes_inclusion(row, cfg, indicators={})
    assert ok is True, why


# ── soft-seed pick path ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_soft_seed_clock():
    ew._SOFT_SEED_LAST_TS = 0.0
    yield
    ew._SOFT_SEED_LAST_TS = 0.0


def test_maybe_soft_seed_skips_non_ready_non_warming(monkeypatch):
    monkeypatch.setattr(ew, "trading_hours_active", lambda *a, **k: True)
    monkeypatch.setattr(ew, "live_print", lambda s: (12.0, 1.0))

    def _rows(cfg):
        return [{
            "symbol": "HOTX",
            "source": "movers",
            "price": 10.0,
            "pct_change": 22.0,
            "dollar_volume": 5_000_000,
            "criteria": ["soft_seed", "movers"],
            "last_ask_src": "stream",
            "last_ask_age_sec": 1.0,
            # Hot EXH + RSI falling → not arm-ready, not warming
            "indicator": {
                "cm_rsi": 60.0,
                "cm_rsi_rising": False,
                "pctr": -10.0,  # heat 90
                "pctr_rising": False,
                "pctr_falling": True,
            },
        }]

    monkeypatch.setattr(ew, "_soft_seed_source_rows", _rows)
    picked, fired = ew.maybe_soft_seed_rows(
        _cfg(ai_watch_soft_seed_enabled=True, ai_watch_soft_seed_interval_sec=1.0,
             ai_watch_soft_seed_max=12),
        now=time.time(),
        indicators={},
    )
    assert fired is True
    assert picked == []


def test_maybe_soft_seed_keeps_arm_ready(monkeypatch):
    monkeypatch.setattr(ew, "trading_hours_active", lambda *a, **k: True)
    monkeypatch.setattr(ew, "live_print", lambda s: (12.0, 1.0))

    def _rows(cfg):
        r = _ready_row()
        r["dollar_volume"] = 3_000_000
        return [r]

    monkeypatch.setattr(ew, "_soft_seed_source_rows", _rows)
    picked, fired = ew.maybe_soft_seed_rows(
        _cfg(ai_watch_soft_seed_enabled=True, ai_watch_soft_seed_interval_sec=1.0),
        now=time.time(),
        indicators={},
    )
    assert fired is True
    assert len(picked) == 1
    assert picked[0]["arm_ready"] is True
    assert picked[0].get("scout_only") is False


def test_maybe_soft_seed_scout_only_warming_short_ttl(monkeypatch):
    monkeypatch.setattr(ew, "trading_hours_active", lambda *a, **k: True)
    monkeypatch.setattr(ew, "live_print", lambda s: (12.0, 1.0))
    now = time.time()

    def _rows(cfg):
        return [{
            "symbol": "WARM1",
            "source": "trending",
            "price": 8.0,
            "pct_change": 12.0,
            "dollar_volume": 2_000_000,
            "criteria": ["soft_seed", "trending"],
            "last_ask_src": "stream",
            "last_ask_age_sec": 1.0,
            "indicator": {
                "cm_rsi": 35.0,
                "cm_rsi_rising": False,  # not arm-ready
                "pctr": -70.0,  # heat 30 — warming band
                "pctr_rising": True,
                "pctr_falling": False,
            },
        }]

    monkeypatch.setattr(ew, "_soft_seed_source_rows", _rows)
    picked, fired = ew.maybe_soft_seed_rows(
        _cfg(ai_watch_soft_seed_enabled=True, ai_watch_soft_seed_interval_sec=1.0,
             ai_watch_scout_ttl_sec=120.0),
        now=now,
        indicators={},
    )
    assert fired is True
    assert len(picked) == 1
    assert picked[0]["scout_only"] is True
    assert picked[0]["seat_role"] == "warming"
    assert picked[0]["arm_ready"] is False
    assert picked[0]["scout_until"] == pytest.approx(now + 120.0)


# ── CHG% soft band ─────────────────────────────────────────────────────────

def test_chg_band_classify():
    cfg = _cfg()
    assert ew.classify_admit_chg_band(18.0, cfg) == "prefer"
    assert ew.classify_admit_chg_band(5.0, cfg) == "below_prefer"
    assert ew.classify_admit_chg_band(45.0, cfg) == "mid"
    assert ew.classify_admit_chg_band(55.0, cfg) == "over_soft"
    assert ew.classify_admit_chg_band(None, cfg) == "unknown"


def test_soft_seed_score_prefers_chg_band_and_demotes_over_soft():
    cfg = _cfg()
    prefer = {"pct_change": 20.0, "dollar_volume": 1e6}
    over = {"pct_change": 60.0, "dollar_volume": 1e6}
    s_pref = ew.soft_seed_scout_score(prefer, cfg)
    s_over = ew.soft_seed_scout_score(over, cfg)
    assert prefer["admit_chg_band"] == "prefer"
    assert over["admit_chg_band"] == "over_soft"
    assert s_pref > s_over


def test_soft_seed_over_soft_pullback_less_harsh():
    cfg = _cfg()
    over = {"pct_change": 60.0, "dollar_volume": 1e6, "admit_range_pos": 40.0}
    s = ew.soft_seed_scout_score(over, cfg)
    assert over["admit_chg_band"] == "over_soft_pullback"
    # Still scored, but not the hard -55 demote path alone.
    assert s > -40


def test_maybe_soft_seed_skips_over_soft_without_pullback(monkeypatch):
    monkeypatch.setattr(ew, "trading_hours_active", lambda *a, **k: True)
    monkeypatch.setattr(ew, "live_print", lambda s: (12.0, 1.0))

    def _rows(cfg):
        r = _ready_row(pct_change=62.0, symbol="EXTND")
        r["dollar_volume"] = 4_000_000
        return [r]

    monkeypatch.setattr(ew, "_soft_seed_source_rows", _rows)
    picked, fired = ew.maybe_soft_seed_rows(
        _cfg(ai_watch_soft_seed_enabled=True, ai_watch_soft_seed_interval_sec=1.0),
        now=time.time(),
        indicators={},
    )
    assert fired is True
    assert picked == []


# ── eviction ───────────────────────────────────────────────────────────────

class _FakeCP:
    def log_event(self, kind, **kw):
        return {"kind": kind, **kw}


class _FakeGT:
    def has_open_position(self, sym):
        return False


def test_never_armable_evict_drops_rsi_extended(monkeypatch):
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda syms: dropped.extend(syms))
    now = time.time()
    rec = {
        "symbol": "STUCK",
        "status": "watching",
        "admit_ts": now - 600.0,  # past subscribe grace
        "block_code": "rsi_extended",
        "block_ts": now - 120.0,
        "unarmable_since": now - 120.0,
        "last_ask_src": "stream",
        "last_ask_age_sec": 2.0,
        "indicator": {
            "cm_rsi": 90.0,
            "cm_rsi_rising": False,
            "pctr": -50.0,
            "pctr_rising": True,
        },
    }
    events = []
    got = ew._maybe_never_armable_evict(
        rec, sym="STUCK", cfg=_cfg(), now=now,
        events=events, cp=_FakeCP(), gt=_FakeGT(),
    )
    assert got is True
    assert dropped == ["STUCK"]
    assert events[0]["reason"] == "never_armable"
    assert events[0]["arm_ready"] is False
    assert events[0].get("arm_ready_reason")


def test_never_armable_evict_respects_grace(monkeypatch):
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda syms: dropped.extend(syms))
    now = time.time()
    rec = {
        "symbol": "YOUNG",
        "status": "watching",
        "admit_ts": now - 600.0,
        "block_code": "exh_falling",
        "block_ts": now - 10.0,
        "unarmable_since": now - 10.0,
    }
    events = []
    got = ew._maybe_never_armable_evict(
        rec, sym="YOUNG", cfg=_cfg(ai_watch_unarmable_evict_sec=90.0),
        now=now, events=events, cp=_FakeCP(), gt=_FakeGT(),
    )
    assert got is False
    assert dropped == []


def test_scout_ttl_drop(monkeypatch):
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda syms: dropped.extend(syms))
    monkeypatch.setattr(
        ew, "evaluate_arm_ready", lambda *a, **k: (False, "rsi_not_rising"))
    now = time.time()
    rec = {
        "symbol": "SCOUT",
        "status": "watching",
        "scout_only": True,
        "seat_role": "warming",
        "scout_until": now - 1.0,
        "arm_ready_reason": "rsi_not_rising",
        "admit_pct_change": 15.0,
        "admit_chg_band": "prefer",
    }
    events = []
    got = ew._maybe_scout_ttl_drop(
        rec, sym="SCOUT", cfg=_cfg(), now=now,
        events=events, cp=_FakeCP(), gt=_FakeGT(),
    )
    assert got is True
    assert dropped == ["SCOUT"]
    assert events[0]["reason"] == "scout_ttl"
    assert events[0]["arm_ready"] is False


def test_unarmable_steal_includes_rsi_stuck(monkeypatch):
    """Preferential steal victims include RSI-stuck seats past grace."""
    now = time.time()
    rec = {
        "symbol": "STUCK",
        "status": "watching",
        "admit_ts": now - 600.0,
        "block_code": "rsi_not_rising",
        "block_ts": now - 200.0,
        "unarmable_since": now - 200.0,
        "last_ask_src": "stream",
        "last_ask_age_sec": 1.0,
    }
    assert ew._is_unarmable_stale_watching(rec, _cfg(), now=now) is True


def test_default_config_bakes_mid_session_knobs():
    assert DEFAULT_CONFIG["ai_watch_admit_require_arm_ready"] is True
    assert DEFAULT_CONFIG["ai_watch_soft_seed_interval_sec"] == 120.0
    assert DEFAULT_CONFIG["ai_watch_stale_timeout_sec"] == 180.0
    assert DEFAULT_CONFIG["ai_watch_max_stale_tape_seats"] == 3
    assert DEFAULT_CONFIG["ai_watch_no_trade_reseed_sec"] == 120.0
    assert DEFAULT_CONFIG["ai_max_spread_r"] == 2.0
    assert DEFAULT_CONFIG["ai_fill_abort_r"] == 5.0
    assert DEFAULT_CONFIG["ai_exh_falling_flatten_enabled"] is False
    assert DEFAULT_CONFIG["ai_no_progress_flatten_enabled"] is False
    assert DEFAULT_CONFIG["ai_local_trail_decay_overtake"] is False
    assert DEFAULT_CONFIG["ai_local_trail_give_r"] == 0.35
    assert DEFAULT_CONFIG["ai_local_trail_be_at_r"] == 0.15
    assert DEFAULT_CONFIG["ai_entry_confirm_max_slip_pct"] == 0.5
    # High-but-sane daily loss (not emergency 999, not tight 3.0).
    assert DEFAULT_CONFIG["ai_daily_loss_limit_r"] >= 12.0
