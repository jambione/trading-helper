"""Square-aligned watch admit / soft-seed / far eviction bus."""
from __future__ import annotations

import time

import pytest

import ai_entry_watch as ew
from config import DEFAULT_CONFIG


def _cfg(**over):
    c = {
        "ai_watch_admit_prefer_square": True,
        "ai_watch_exh_square_arm": True,
        "ai_watch_exh_pre_thr": 35.0,
        "ai_watch_max_far_exh_seats": 0,
        "ai_watch_far_exh_evict_sec": 45.0,
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


def test_default_config_bakes_square_bus_and_trail():
    assert DEFAULT_CONFIG["ai_watch_admit_prefer_square"] is True
    assert DEFAULT_CONFIG["ai_watch_exh_pre_thr"] == 35.0
    assert DEFAULT_CONFIG["ai_watch_max_far_exh_seats"] == 0
    assert DEFAULT_CONFIG["ai_watch_far_exh_evict_sec"] == 45.0
    assert DEFAULT_CONFIG["ai_watch_arm_require_cm_rsi"] is False
    assert DEFAULT_CONFIG["ai_local_trail_time_decay_enabled"] is False
    assert DEFAULT_CONFIG["ai_local_trail_arm_r"] == 0.25
    assert DEFAULT_CONFIG["ai_local_trail_ob_hold_mae_r"] == -1.0
    # Square arm math unchanged.
    assert DEFAULT_CONFIG["ai_watch_exh_square_arm"] is True
    # Premarket flood stays off — pre_square farm ≠ include_pre.
    assert DEFAULT_CONFIG.get("ai_watch_morning_flood_include_pre", False) is False


# ── soft-seed prefer ───────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_soft_seed():
    ew._SOFT_SEED_LAST_TS = 0.0
    yield
    ew._SOFT_SEED_LAST_TS = 0.0


def test_soft_seed_prefers_pre_square_over_far(monkeypatch):
    monkeypatch.setattr(ew, "trading_hours_active", lambda *a, **k: True)

    def _rows(cfg):
        return [
            {
                "symbol": "FAR1", "source": "movers", "pct_change": 20.0,
                "dollar_volume": 5e6, "price": 10.0,
                "criteria": ["soft_seed", "movers"],
                "indicator": _ind(fast=-39, slow=-64),
            },
            {
                "symbol": "PRE1", "source": "trending", "pct_change": 12.0,
                "dollar_volume": 3e6, "price": 11.0,
                "criteria": ["soft_seed", "trending"],
                "indicator": _ind(fast=-30, slow=-28),
            },
            {
                "symbol": "SQ1", "source": "momentum", "pct_change": 15.0,
                "dollar_volume": 4e6, "price": 12.0,
                "criteria": ["soft_seed", "momentum"],
                "indicator": _ind(fast=-13, slow=-4),
            },
        ]

    monkeypatch.setattr(ew, "_soft_seed_source_rows", _rows)
    picked, fired = ew.maybe_soft_seed_rows(_cfg(), now=time.time(), indicators={})
    assert fired is True
    syms = [r["symbol"] for r in picked]
    assert "SQ1" in syms and "PRE1" in syms
    assert "FAR1" not in syms  # far does not consume keep seats
    assert picked[0]["exh_seat_class"] == "square"  # ranked first


def test_pre_square_keeps_when_arm_ready_required_but_false(monkeypatch):
    """Prefer-square: approach seats keep without full arm_ready (open still gated)."""
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
        ai_watch_admit_prefer_square=True,
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


def test_soft_seed_score_ranks_square_highest():
    cfg = _cfg()
    far = {"pct_change": 25.0, "dollar_volume": 1e7,
           "indicator": _ind(fast=-39, slow=-64)}
    pre = {"pct_change": 10.0, "dollar_volume": 1e6,
           "indicator": _ind(fast=-30, slow=-28)}
    sq = {"pct_change": 12.0, "dollar_volume": 1e6,
          "indicator": _ind(fast=-13, slow=-4)}
    assert ew.soft_seed_scout_score(sq, cfg) > ew.soft_seed_scout_score(pre, cfg)
    assert ew.soft_seed_scout_score(pre, cfg) > ew.soft_seed_scout_score(far, cfg)


def test_warming_profile_uses_pre_square():
    cfg = _cfg()
    pre = {"indicator": _ind(fast=-30, slow=-28)}
    far = {"indicator": _ind(fast=-39, slow=-64)}
    assert ew.is_warming_exh_profile(
        None, True, cfg, row=pre, ind=pre["indicator"]) is True
    assert ew.is_warming_exh_profile(
        None, True, cfg, row=far, ind=far["indicator"]) is False


# ── far eviction ───────────────────────────────────────────────────────────

class _FakeCP:
    def log_event(self, kind, **kw):
        return {"kind": kind, **kw}


class _FakeGT:
    def has_open_position(self, sym):
        return False


def test_far_exh_evict_after_ttl(monkeypatch):
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda s: dropped.extend(s))
    now = time.time()
    rec = {
        "symbol": "FARX",
        "status": "watching",
        "admit_ts": now - 600.0,
        "far_exh_since": now - 120.0,
        "exh_seat_class": "far",
        "pctr_gap": 25.0,
        "indicator": _ind(fast=-39, slow=-64),
        "seat_role": "pin",
    }
    events = []
    got = ew._maybe_far_exh_evict(
        rec, sym="FARX", cfg=_cfg(), now=now,
        events=events, cp=_FakeCP(), gt=_FakeGT(),
    )
    assert got is True
    assert dropped == ["FARX"]
    assert events[0]["reason"] == "far_exh"


def test_far_exh_evict_respects_grace(monkeypatch):
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda s: dropped.extend(s))
    now = time.time()
    rec = {
        "symbol": "YOUNG",
        "status": "watching",
        "admit_ts": now - 600.0,
        "far_exh_since": now - 10.0,
        "exh_seat_class": "far",
        "indicator": _ind(fast=-50, slow=-70),
    }
    assert ew._maybe_far_exh_evict(
        rec, sym="YOUNG", cfg=_cfg(), now=now,
        events=[], cp=_FakeCP(), gt=_FakeGT(),
    ) is False
    assert dropped == []


def test_unarmable_steal_includes_far_past_grace():
    now = time.time()
    rec = {
        "symbol": "FARPIN",
        "status": "watching",
        "admit_ts": now - 600.0,
        "seat_role": "pin",
        "exh_seat_class": "far",
        "far_exh_since": now - 200.0,
        "indicator": _ind(fast=-39, slow=-64),
        "last_ask_src": "stream",
        "last_ask_age_sec": 1.0,
    }
    assert ew._is_unarmable_stale_watching(rec, _cfg(), now=now) is True


def test_square_arm_unchanged_smci_rklb():
    """Admit bus must not loosen square arm gates."""
    cfg = {
        "ai_watch_exh_square_arm": True,
        "ai_watch_admit_prefer_square": True,
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


def test_far_exh_steal_for_pre_square(monkeypatch):
    """Book full of far + waiting pre_square → steal with distinct reason."""
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda s: dropped.extend(s))
    now = time.time()
    state = {}
    for i in range(6):
        sym = f"FAR{i}"
        state[sym] = {
            "symbol": sym,
            "status": "watching",
            "admit_ts": now - 600.0,
            "far_exh_since": now - 60.0,
            "exh_seat_class": "far",
            "indicator": _ind(fast=-39, slow=-64),
            "seat_role": "pin" if i < 2 else None,
            "admit_dollar_volume": 1e5 + i,
        }
    cands = [
        {
            "symbol": "PRE1",
            "exh_seat_class": "pre_square",
            "indicator": _ind(fast=-32, slow=-30),
            "dollar_volume": 5e6,
            "last_ask_src": "stream",
            "last_ask_age_sec": 1.0,
        },
        {
            "symbol": "SQ1",
            "exh_seat_class": "square",
            "indicator": _ind(fast=-13, slow=-4),
            "dollar_volume": 6e6,
            "last_ask_src": "stream",
            "last_ask_age_sec": 1.0,
        },
    ]
    events = []
    got = ew._preferential_far_exh_steal(
        state, cfg=_cfg(), now=now, events=events,
        cp=_FakeCP(), gt=_FakeGT(), candidates=cands,
    )
    assert len(got) == 2
    reasons = {e.get("reason") for e in events}
    assert "far_exh_steal_for_square" in reasons
    assert "far_exh_steal_for_pre_square" in reasons
    assert set(dropped) == set(got)


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


def test_soft_seed_score_far_never_outranks_pre_on_chg():
    cfg = _cfg()
    far = {"pct_change": 80.0, "dollar_volume": 5e7,
           "indicator": _ind(fast=-39, slow=-64)}
    pre = {"pct_change": 5.0, "dollar_volume": 1e5,
           "indicator": _ind(fast=-32, slow=-30)}
    assert ew.soft_seed_scout_score(pre, cfg) > ew.soft_seed_scout_score(far, cfg)
