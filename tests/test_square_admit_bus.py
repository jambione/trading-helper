"""Square-aligned watch admit / soft-seed / far eviction bus."""
from __future__ import annotations

import time

import pytest

import ai_entry_watch as ew
from config import DEFAULT_CONFIG


def _cfg(**over):
    c = {
        "ai_watch_exh_square_arm": True,
        "ai_watch_exh_pre_thr": 35.0,
        "ai_watch_max_far_exh_seats": 0,
        "rte_threshold": 20,
        "rte_confluence_max": 15.0,
        "ai_watch_soft_seed_enabled": True,
        "ai_watch_soft_seed_interval_sec": 1.0,
        "ai_watch_soft_seed_max": 12,
        "ai_watch_admit_require_arm_ready": False,  # isolate seat-class prefer
        "ai_max_price": 100.0,
        "ai_watch_scout_ttl_sec": 120.0,
        # Isolate from wall-clock morning flood (09:30–11 ET).
        "ai_watch_morning_flood_enabled": False,
    }
    c.update(over)
    return c


def _ind(*, fast, slow, rising=True, slow_rising=True):
    return {
        "pctr": float(fast),
        "pctr_slow": float(slow),
        "pctr_rising": rising,
        "pctr_slow_rising": slow_rising,
        "pctr_falling": not rising,
        "pctr_ob": float(fast) >= -20 and float(slow) >= -20,
    }


# ── classify ───────────────────────────────────────────────────────────────

def test_classify_square_pre_far_unknown():
    cfg = _cfg()
    assert ew.classify_exh_seat(
        {"indicator": _ind(fast=-13, slow=-4)}, cfg)[0] == "square"
    # Brief fixture: both −32, gap 8, rising → pre_square (pre_thr=35).
    cls, gap = ew.classify_exh_seat(
        {"indicator": _ind(fast=-32, slow=-32)}, cfg)
    assert cls == "pre_square"
    assert gap == pytest.approx(0.0)
    cls, gap = ew.classify_exh_seat(
        {"indicator": _ind(fast=-30, slow=-28)}, cfg)
    assert cls == "pre_square"
    assert gap == pytest.approx(2.0)
    # Gap 25 → far even if both in approach band.
    assert ew.classify_exh_seat(
        {"indicator": _ind(fast=-20, slow=-45)}, cfg)[0] == "far"
    assert ew.classify_exh_seat(
        {"indicator": _ind(fast=-39, slow=-64)}, cfg)[0] == "far"
    # Missing slow → unknown (not pre_square).
    assert ew.classify_exh_seat(
        {"indicator": {"pctr": -30.0}}, cfg)[0] == "unknown"
    assert ew.is_warming_exh_profile(
        None, True, cfg, allow_unknown=False,
        row={"indicator": {"pctr": -30.0}},
        ind={"pctr": -30.0},
    ) is False


def test_stamp_exh_seat_fields():
    row = {"symbol": "X", "indicator": _ind(fast=-30, slow=-28)}
    assert ew.stamp_exh_seat_fields(row, _cfg()) == "pre_square"
    assert row["exh_seat_class"] == "pre_square"
    assert row["pctr_gap"] == pytest.approx(2.0)
    assert row["exh_seat_class_admit"] == "pre_square"


def test_admit_class_does_not_freeze_unknown():
    """APLD 2026-09-21: seat before dual-%R must not lock admit=unknown forever."""
    rec = {
        "symbol": "APLD",
        "exh_seat_class": "unknown",
        "exh_seat_class_admit": "unknown",
        "indicator": {"pctr": -18.6},  # slow missing
    }
    assert ew.maybe_freeze_exh_seat_class_admit(rec) is None
    assert rec.get("exh_seat_class_admit") in (None, "")
    # Dual lines arrive → square; admit upgrades from unset/unknown.
    rec["indicator"] = _ind(fast=-18.6, slow=-9.4)
    assert ew.stamp_exh_seat_fields(rec, _cfg()) == "square"
    assert rec["exh_seat_class_admit"] == "square"


def test_admit_class_freezes_first_known_class():
    """Once far/pre/square is stamped, later square does not rewrite admit."""
    rec = {"symbol": "Y", "indicator": _ind(fast=-39, slow=-64)}
    assert ew.stamp_exh_seat_fields(rec, _cfg()) == "far"
    assert rec["exh_seat_class_admit"] == "far"
    rec["indicator"] = _ind(fast=-13, slow=-4)
    assert ew.stamp_exh_seat_fields(rec, _cfg()) == "square"
    assert rec["exh_seat_class"] == "square"
    assert rec["exh_seat_class_admit"] == "far"  # frozen at first known


def test_admission_fields_skip_unknown_admit():
    prev = {"exh_seat_class_admit": "unknown", "exh_seat_class": "unknown"}
    row = {"exh_seat_class": "square", "pct_change": 12.0}
    fields = ew._admission_fields(row, prev, time.time())
    assert fields.get("exh_seat_class_admit") == "square"


def test_default_config_bakes_square_bus_and_trail():
    assert DEFAULT_CONFIG["ai_watch_exh_pre_thr"] == 35.0
    assert DEFAULT_CONFIG["ai_watch_max_far_exh_seats"] == 0
    assert DEFAULT_CONFIG["ai_local_trail_time_decay_enabled"] is True
    assert DEFAULT_CONFIG["ai_local_trail_arm_r"] == 0.25
    assert DEFAULT_CONFIG["ai_local_trail_ob_hold_mae_r"] == -1.0
    # Square arm math unchanged.
    assert DEFAULT_CONFIG["ai_watch_exh_square_arm"] is True
    # Premarket flood stays off — pre_square farm ≠ include_pre.
    assert DEFAULT_CONFIG.get("ai_watch_morning_flood_include_pre", False) is False


# ── soft-seed ───────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_soft_seed():
    ew._SOFT_SEED_LAST_TS = 0.0
    yield
    ew._SOFT_SEED_LAST_TS = 0.0


def test_pre_square_keeps_when_arm_ready_required_but_false(monkeypatch):
    """Approach seats keep without full arm_ready (open still gated)."""
    monkeypatch.setattr(ew, "trading_hours_active", lambda *a, **k: True)
    monkeypatch.setattr(
        ew, "evaluate_arm_ready", lambda *a, **k: (False, "rsi_not_rising"))

    def _rows(cfg):
        return [
            {
                "symbol": "PREK", "source": "trending", "pct_change": 12.0,
                "dollar_volume": 3e6, "price": 11.0,
                "criteria": ["soft_seed", "trending"],
                "last_ask_src": "stream", "last_ask_age_sec": 1.0,
                "indicator": {
                    **_ind(fast=-30, slow=-28),
                    "cm_rsi": 55.0, "cm_rsi_rising": False,
                },
            },
            {
                "symbol": "FARX", "source": "movers", "pct_change": 22.0,
                "dollar_volume": 5e6, "price": 10.0,
                "criteria": ["soft_seed", "movers"],
                "last_ask_src": "stream", "last_ask_age_sec": 1.0,
                "indicator": {
                    **_ind(fast=-39, slow=-64),
                    "cm_rsi": 40.0, "cm_rsi_rising": True,
                },
            },
        ]

    monkeypatch.setattr(ew, "_soft_seed_source_rows", _rows)
    cfg = _cfg(
        ai_watch_admit_require_arm_ready=True,
        ai_watch_admit_arm_ready_rth_only=False,
    )
    picked, fired = ew.maybe_soft_seed_rows(cfg, now=time.time(), indicators={})
    assert fired is True
    syms = [r["symbol"] for r in picked]
    assert "PREK" in syms
    pre = next(r for r in picked if r["symbol"] == "PREK")
    assert pre.get("scout_only") is False
    assert pre.get("exh_seat_class") == "pre_square"
    assert pre.get("arm_ready") is False
    assert "FARX" not in syms


# ── far eviction ───────────────────────────────────────────────────────────

class _FakeCP:
    def log_event(self, kind, **kw):
        return {"kind": kind, **kw}


class _FakeGT:
    def has_open_position(self, sym):
        return False


def test_dead_unknown_evicts_missing_pctr_inside_subscribe_grace(monkeypatch):
    """WHLR-class: no %R, admitted 40s ago, still inside the 90s subscribe grace."""
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda s: dropped.extend(s))
    now = time.time()
    rec = {
        "symbol": "WHLR",
        "status": "watching",
        "admit_ts": now - 40.0,
        "exh_seat_class": "unknown",
        "indicator": {},
        "source": "momentum",
        "morning_flood": 1,
    }
    cfg = _cfg(
        ai_watch_dead_seat_evict_sec=30.0,
        ai_watch_stream_subscribe_grace_sec=90.0,
        ai_watch_morning_flood_enabled=True,
    )
    events = []
    got = ew._maybe_dead_unknown_evict(
        rec, sym="WHLR", cfg=cfg, now=now,
        events=events, cp=_FakeCP(), gt=_FakeGT(),
    )
    assert got is True
    assert dropped == ["WHLR"]
    assert events[0]["reason"] == "dead_unknown"


def test_dead_unknown_keeps_a_young_seat_and_a_rising_heater(monkeypatch):
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda s: dropped.extend(s))
    now = time.time()
    cfg = _cfg(ai_watch_dead_seat_evict_sec=30.0)
    young = {
        "symbol": "NEW",
        "status": "watching",
        "admit_ts": now - 10.0,
        "exh_seat_class": "unknown",
        "indicator": {},
    }
    assert ew._maybe_dead_unknown_evict(
        young, sym="NEW", cfg=cfg, now=now,
        events=[], cp=_FakeCP(), gt=_FakeGT(),
    ) is False
    assert dropped == []

    heat = {
        "symbol": "BENF",
        "status": "watching",
        "admit_ts": now - 400.0,
        "exh_seat_class": "far",
        "indicator": _ind(fast=-44.0, slow=-40.0),
        "block_code": None,
    }
    assert ew._maybe_dead_unknown_evict(
        heat, sym="BENF", cfg=cfg, now=now,
        events=[], cp=_FakeCP(), gt=_FakeGT(),
    ) is False

    square = {
        "symbol": "SQ",
        "status": "watching",
        "admit_ts": now - 400.0,
        "indicator": _ind(fast=-13.0, slow=-4.0),
    }
    assert ew._maybe_dead_unknown_evict(
        square, sym="SQ", cfg=cfg, now=now,
        events=[], cp=_FakeCP(), gt=_FakeGT(),
    ) is False
    assert dropped == []


def test_dead_unknown_evicts_stale_quote_after_its_own_clock(monkeypatch):
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda s: dropped.extend(s))
    now = time.time()
    cfg = _cfg(ai_watch_dead_seat_evict_sec=30.0)
    rec = {
        "symbol": "IPDN",
        "status": "watching",
        "admit_ts": now - 600.0,
        "dead_unknown_since": now - 40.0,
        "block_code": "stale_quote",
        "block_ts": now - 1.0,  # poll restamps this every cycle
        "exh_seat_class": "unknown",
        "indicator": {},
        "source": "momentum",
        "morning_flood": 1,
    }
    events = []
    got = ew._maybe_dead_unknown_evict(
        rec, sym="IPDN", cfg=cfg, now=now,
        events=events, cp=_FakeCP(), gt=_FakeGT(),
    )
    assert got is True
    assert events[0]["reason"] == "dead_unknown"
    assert events[0]["block"] == "stale_quote"

    fresh = dict(rec)
    fresh["symbol"] = "DCOY"
    fresh["dead_unknown_since"] = now - 5.0
    assert ew._maybe_dead_unknown_evict(
        fresh, sym="DCOY", cfg=cfg, now=now,
        events=[], cp=_FakeCP(), gt=_FakeGT(),
    ) is False


def test_square_arm_unchanged_smci_rklb():
    """Admit bus must not loosen square arm gates."""
    cfg = {
        "ai_watch_exh_square_arm": True,
        "ai_watch_require_exh_rising": True,
        "ai_watch_require_live_pctr": False,
        "ai_watch_tv_exh_rsi": False,
        "rte_threshold": 20,
        "rte_confluence_max": 15,
        "ai_watch_ob_allow_hot": False,
    }
    smci = {"indicator": {
        **_ind(fast=-13, slow=-4), "pctr_ob": True, "pctr_tight": True,
    }}
    rklb = {"indicator": _ind(fast=-39, slow=-64)}
    ok, why = ew.exhaustion_allows_buy(smci, cfg)
    assert ok is True and why == "overbought"
    ok, why = ew.exhaustion_allows_buy(rklb, cfg)
    assert ok is False and why == "exh_not_tight"


def test_exh_seat_class_counts():
    state = {
        "A": {"symbol": "A", "status": "watching", "exh_seat_class": "square"},
        "B": {"symbol": "B", "status": "watching", "exh_seat_class": "pre_square"},
        "C": {"symbol": "C", "status": "watching", "exh_seat_class": "far"},
        "D": {"symbol": "D", "status": "watching", "exh_seat_class": "far"},
        "_meta": "skip",
    }
    c = ew.exh_seat_class_counts(state)
    assert c["n_square"] == 1
    assert c["n_pre_square"] == 1
    assert c["n_far"] == 2
    assert c["n_seats"] == 4
