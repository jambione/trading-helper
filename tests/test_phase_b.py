"""Phase B Hybrid C plumbing — session, last-price, arm strip, book, dry entry."""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import phase_b as pb  # noqa: E402
import phase_b_ledger as pbl  # noqa: E402

ET = ZoneInfo("America/New_York")

# 2026-09-17 is a Thursday — weekday boundaries for clock tests.
def _ts(h: int, m: int, day: int = 17) -> float:
    return datetime(2026, 9, day, h, m, tzinfo=ET).timestamp()


CFG_ON = {
    # The indicator arm is RTH's as of 2026-09-18 (square + cm_rsi). The cases
    # below assert the legacy 40-70 band and its reason codes, which still ship
    # behind this knob; premarket/RTH parity is covered in
    # tests/test_phase_b_shared_arm.py.
    "ai_phase_b_legacy_arm": True,
    "ai_phase_b_enabled": True,
    "ai_phase_b_dry_run": True,
    "ai_phase_b_start_time": "04:00",
    "ai_phase_b_entry_cutoff": "09:20",
    "ai_phase_b_flatten_start": "09:25",
    "ai_phase_b_flat_deadline": "09:28",
    "ai_phase_b_max_seats": 4,
    "ai_phase_b_max_open": 2,
    "ai_phase_b_min_price": None,
    "ai_watch_min_price": 2.0,
    "ai_phase_b_print_max_age_sec": 15.0,
    "ai_phase_b_entry_limit_pad_pct": 0.15,
    "ai_phase_b_entry_limit_pad_max_px": 0.05,
    "ai_phase_b_entry_limit_ttl_sec": 45.0,
    "ai_phase_b_exh_min": 40.0,
    "ai_phase_b_exh_max": 70.0,
    "ai_phase_b_require_exh_rising": True,
    "ai_phase_b_rsi_block_falling_above": 10.0,
    "ai_phase_b_arm_confirm_ticks": 1,
    "ai_phase_b_ledger_enabled": True,
    "ai_phase_b_no_rth_handoff": True,
    "ai_phase_b_working_sell": True,
    "ai_phase_b_seat_stale_sec": 75.0,
}


@pytest.fixture(autouse=True)
def _clean_book(tmp_path):
    pb.set_book_path_for_tests(tmp_path / "phase_b_book.json")
    pbl.set_ledger_path_for_tests(tmp_path / "phase_b.jsonl")
    yield
    pb.set_book_path_for_tests(None)
    pbl.set_ledger_path_for_tests(None)


# ── Part 1: clock gates ──────────────────────────────────────────────────

@pytest.mark.parametrize("hm,expect_session,expect_entries,expect_flatten", [
    ((3, 59), False, False, False),
    ((4, 0), True, True, False),
    ((9, 19), True, True, False),
    ((9, 20), True, False, False),
    ((9, 25), True, False, True),
    ((9, 28), False, False, False),
    ((9, 30), False, False, False),
])
def test_clock_boundaries(hm, expect_session, expect_entries, expect_flatten):
    t = _ts(*hm)
    assert pb.phase_b_session_active(t, CFG_ON) is expect_session
    assert pb.phase_b_allow_entries(t, CFG_ON) is expect_entries
    assert pb.phase_b_flatten_active(t, CFG_ON) is expect_flatten


def test_disabled_master_blocks_session():
    cfg = dict(CFG_ON, ai_phase_b_enabled=False)
    t = _ts(8, 0)
    assert pb.phase_b_session_active(t, cfg) is False
    assert pb.phase_b_allow_entries(t, cfg) is False


def test_disabled_means_zero_entry_attempts(monkeypatch):
    cfg = dict(CFG_ON, ai_phase_b_enabled=False)
    placed = []
    rec = {
        "symbol": "AAA",
        "source": "momentum",
        "indicator": {
            "pctr": 55.0, "pctr_rising": True,
            "cm_rsi": 30.0, "cm_rsi_rising": True,
        },
    }
    monkeypatch.setattr(pb, "fresh_stream_last", lambda *a, **k: (3.0, None))
    out = pb.try_phase_b_entry(
        rec, equity=10_000, cfg=cfg, now=_ts(8, 0),
        place_fn=lambda *a, **k: placed.append(1) or {"ok": True},
    )
    assert out["ok"] is False
    assert out["error"] == "phase_b_disabled"
    assert placed == []


def test_rth_trading_hours_unchanged_when_phase_b_off():
    """Plan A trading_hours_active still requires market_open=True."""
    import ai_entry_watch as ew
    cfg = {"ai_watch_enabled": True, "ai_watch_start_time": "04:00",
           "ai_sod_liquidate_enabled": False, "ai_phase_b_enabled": False}
    # Unknown / closed → False (unchanged contract).
    assert ew.trading_hours_active(cfg, _ts(10, 0), market_open=False) is False
    assert ew.trading_hours_active(cfg, _ts(10, 0), market_open=None) is False
    assert ew.trading_hours_active(cfg, _ts(10, 0), market_open=True) is True


# ── Part 2: Finnhub-last price path ──────────────────────────────────────

def test_stale_print_refuses(monkeypatch):
    monkeypatch.setattr(
        pb, "stream_last_print",
        lambda *a, **k: {"price": 3.0, "age_sec": 30.0},
    )
    px, why = pb.fresh_stream_last("AAA", now=_ts(8, 0), cfg=CFG_ON)
    assert px is None
    assert why == "phase_b_stale_print"


def test_fresh_last_builds_padded_limit():
    lim = pb.entry_limit_price(10.0, cfg=CFG_ON)
    # 0.15% of 10 = 0.015, under $0.05 cap → 10.015 → 10.02 rounded? 
    # 10 * 1.0015 = 10.015 → round 10.02; min(10.015, 10.05)=10.015 → 10.01/10.02
    assert lim is not None
    assert lim >= 10.01
    assert lim <= 10.05


def test_pad_max_px_caps_limit():
    cfg = dict(CFG_ON, ai_phase_b_entry_limit_pad_pct=5.0,  # 5%
               ai_phase_b_entry_limit_pad_max_px=0.05)
    lim = pb.entry_limit_price(10.0, cfg=cfg)
    assert lim == 10.05


# ── Part 3: dry entry + TTL unfilled ─────────────────────────────────────

def test_dry_run_places_nothing_on_broker(monkeypatch, tmp_path):
    placed = []
    monkeypatch.setattr(pb, "fresh_stream_last", lambda *a, **k: (3.0, None))
    rec = {
        "symbol": "DRY",
        "source": "momentum",
        "indicator": {
            "pctr": 55.0, "pctr_rising": True,
            "cm_rsi": 25.0, "cm_rsi_rising": True,
        },
    }
    out = pb.try_phase_b_entry(
        rec, equity=10_000, cfg=CFG_ON, now=_ts(8, 0),
        place_fn=lambda *a, **k: placed.append(("broker", a)) or {"ok": True},
    )
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out.get("broker") is False
    assert placed == []
    rows = pbl.read_day(path=tmp_path / "phase_b.jsonl")
    kinds = {r["kind"] for r in rows}
    assert "arm" in kinds
    assert "entry_limit" in kinds


def test_ttl_cancel_emits_unfilled(tmp_path):
    t0 = _ts(8, 0)
    ok = pb.cancel_entry_on_ttl(
        "TTL", t0 - 60.0, now=t0, cfg=CFG_ON,
        cancel_fn=lambda s: None,
    )
    assert ok is True
    rows = pbl.read_day(path=tmp_path / "phase_b.jsonl")
    assert any(r.get("kind") == "unfilled" for r in rows)
    assert any(r.get("reason") == "phase_b_unfilled" for r in rows)


def test_phase_b_no_rth_handoff():
    pos = {"phase_b": True, "working_sell_id": "x"}
    assert pb.should_handoff_to_rth(pos, CFG_ON) is False
    pos2 = {"entry_path": "watch"}
    assert pb.should_handoff_to_rth(pos2, CFG_ON) is True


# ── Part 4: universe / book ──────────────────────────────────────────────

def test_momentum_admitted_before_trending_when_seats_tight():
    cands = [
        {"symbol": "TREND", "source": "trending", "score": 99, "price": 3.0},
        {"symbol": "MOM", "source": "momentum", "score": 10, "price": 3.0},
    ]
    ordered = pb.sort_candidates(cands, CFG_ON)
    assert ordered[0]["symbol"] == "MOM"
    assert ordered[1]["symbol"] == "TREND"


def test_sub_3_name_not_rejected_for_price_alone():
    assert pb.price_ok_for_book(2.5, CFG_ON) is True
    assert pb.price_ok_for_book(1.5, CFG_ON) is False  # under ai_watch_min_price 2.0


def test_open_3_refused_when_max_open_2():
    ok, why = pb.can_open_another(2, CFG_ON)
    assert ok is False
    assert why == "phase_b_max_open"


def test_research_source_off_v1():
    book = {}
    ok, why = pb.admit_candidate(
        {"symbol": "RES", "source": "research", "price": 3.0},
        book, cfg=CFG_ON, now=_ts(8, 0),
    )
    assert ok is False
    assert why == "phase_b_source_off"


def test_seat_and_priority_replace():
    t = _ts(8, 0)
    cfg = dict(CFG_ON, ai_phase_b_max_seats=1)
    ok, _ = pb.seat_symbol(
        {"symbol": "TREND", "source": "trending", "price": 3.0, "score": 1},
        cfg=cfg, now=t,
    )
    assert ok
    ok2, why2 = pb.seat_symbol(
        {"symbol": "MOM", "source": "momentum", "price": 3.0, "score": 1},
        cfg=cfg, now=t,
    )
    assert ok2, why2
    book = pb.load_book()
    assert "MOM" in book
    assert "TREND" not in book


def test_wp1_sources_allow_drops_trending():
    cfg = dict(CFG_ON, ai_phase_b_sources_allow="momentum,movers")
    ordered = pb.sort_candidates(
        [
            {"symbol": "TREND", "source": "trending", "score": 99, "price": 3.0},
            {"symbol": "MOM", "source": "momentum", "score": 10, "price": 3.0},
            {"symbol": "MOV", "source": "movers", "score": 5, "price": 3.0},
        ],
        cfg,
    )
    assert [r["symbol"] for r in ordered] == ["MOM", "MOV"]
    ok, why = pb.admit_candidate(
        {"symbol": "TREND", "source": "trending", "price": 3.0},
        {}, cfg=cfg, now=_ts(8, 0),
    )
    assert ok is False
    assert why == "phase_b_source_off"


def test_liquidity_floor_fail_closed_and_pass():
    cfg = dict(CFG_ON, ai_phase_b_min_prior_dollar_vol=2_000_000.0)
    ok, why = pb.liquidity_ok(
        {"symbol": "THIN", "price": 3.0},
        cfg,
        dollar_vol=500_000.0,
    )
    assert ok is False
    assert why == "phase_b_liquidity"
    ok2, why2 = pb.liquidity_ok(
        {"symbol": "THIN", "price": 3.0},
        cfg,
        dollar_vol=None,
    )
    assert ok2 is False
    assert why2 == "phase_b_liquidity_unknown"
    ok3, why3 = pb.liquidity_ok(
        {"symbol": "FAT", "price": 3.0, "prior_dollar_vol": 5_000_000},
        cfg,
    )
    assert ok3 is True
    assert why3 == "ok"
    ok4, why4 = pb.admit_candidate(
        {"symbol": "THIN", "source": "momentum", "price": 3.0, "dollar_volume": 100_000},
        {}, cfg=cfg, now=_ts(8, 0),
    )
    assert ok4 is False
    assert why4 == "phase_b_liquidity"


def test_liquidity_floor_disabled_when_zero():
    cfg = dict(CFG_ON, ai_phase_b_min_prior_dollar_vol=0)
    ok, why = pb.liquidity_ok({"symbol": "X", "price": 3.0}, cfg, dollar_vol=None)
    assert ok is True
    assert why == "ok"


# ── Part 5: arm strip ────────────────────────────────────────────────────

def _rec(exh, exh_rising, rsi, rsi_rising):
    return {
        "symbol": "ARM",
        "source": "momentum",
        "indicator": {
            "pctr": exh,
            "pctr_rising": exh_rising,
            "pctr_falling": (exh_rising is False),
            "cm_rsi": rsi,
            "cm_rsi_rising": rsi_rising,
            "cm_rsi_falling": (rsi_rising is False),
        },
    }


def test_rsi_65_falling_blocks(monkeypatch):
    monkeypatch.setattr(pb, "fresh_stream_last", lambda *a, **k: (3.0, None))
    ok, why = pb.phase_b_arm_allows(
        _rec(55, True, 65, False), cfg=CFG_ON, now=_ts(8, 0), last=3.0,
    )
    assert ok is False
    assert why == "phase_b_rsi_falling"


def test_rsi_65_rising_allows(monkeypatch):
    monkeypatch.setattr(pb, "fresh_stream_last", lambda *a, **k: (3.0, None))
    ok, why = pb.phase_b_arm_allows(
        _rec(55, True, 65, True), cfg=CFG_ON, now=_ts(8, 0), last=3.0,
    )
    assert ok is True
    assert why == "phase_b_arm"


def test_exh_35_blocks(monkeypatch):
    monkeypatch.setattr(pb, "fresh_stream_last", lambda *a, **k: (3.0, None))
    ok, why = pb.phase_b_arm_allows(
        _rec(35, True, 30, True), cfg=CFG_ON, now=_ts(8, 0), last=3.0,
    )
    assert ok is False
    assert why == "phase_b_exh_band"


def test_exh_55_rising_allows(monkeypatch):
    monkeypatch.setattr(pb, "fresh_stream_last", lambda *a, **k: (3.0, None))
    ok, why = pb.phase_b_arm_allows(
        _rec(55, True, 30, True), cfg=CFG_ON, now=_ts(8, 0), last=3.0,
    )
    assert ok is True


# ── Part 6: ledger + defaults ────────────────────────────────────────────

def test_config_defaults_are_off_and_dry():
    from config import DEFAULT_CONFIG, SAFE_CONFIG_KEYS
    assert DEFAULT_CONFIG.get("ai_phase_b_enabled") is False
    assert DEFAULT_CONFIG.get("ai_phase_b_dry_run") is True
    assert DEFAULT_CONFIG.get("ai_phase_b_min_price") is None
    assert "ai_phase_b_enabled" in SAFE_CONFIG_KEYS


def test_hard_stop_hit():
    assert pb.hard_stop_hit(10.0, 9.4, cfg=CFG_ON) is True   # -6%
    assert pb.hard_stop_hit(10.0, 9.6, cfg=CFG_ON) is False  # -4%


def test_get_latest_print_age(monkeypatch):
    import finnhub_stream as fh
    now = time.time()
    with fh.FINNHUB_STATE.lock:
        fh.FINNHUB_STATE.prices["PBX"] = {
            "price": 2.5,
            "trade_ts": now - 3.0,
            "ts_unix": now - 1.0,
        }
    got = fh.get_latest_print("PBX")
    assert got is not None
    assert got["price"] == 2.5
    assert got["age_sec"] is not None
    assert 2.0 <= got["age_sec"] <= 5.0


# ── Pack #6: scoreable path ──────────────────────────────────────────────

def test_williams_pctr_converts_to_exh_band(monkeypatch):
    """Engine Williams %R −45 → exhaustion 55 → inside 40–70 band."""
    monkeypatch.setattr(pb, "fresh_stream_last", lambda *a, **k: (3.0, None))
    rec = {
        "symbol": "WPR",
        "indicator": {
            "pctr": -45.0,  # Williams
            "pctr_rising": True,
            "cm_rsi": 25.0,
            "cm_rsi_rising": True,
        },
    }
    ok, why = pb.phase_b_arm_allows(rec, cfg=CFG_ON, now=_ts(8, 0), last=3.0)
    assert ok is True, why


def test_empty_indicator_still_no_exh(monkeypatch):
    monkeypatch.setattr(pb, "fresh_stream_last", lambda *a, **k: (3.0, None))
    ok, why = pb.phase_b_arm_allows(
        {"symbol": "MT", "indicator": {}}, cfg=CFG_ON, now=_ts(8, 0), last=3.0,
    )
    assert ok is False and why == "phase_b_no_exh"


def test_refresh_seat_indicators_stamps_engine(monkeypatch, tmp_path):
    pb.set_book_path_for_tests(tmp_path / "book.json")
    book = {
        "AAA": {
            "symbol": "AAA", "status": "watching", "indicator": {},
            "admit_ts": _ts(8, 0),
        },
    }
    pb.save_book(book)
    monkeypatch.setattr(
        pb, "fresh_stream_last", lambda *a, **k: (4.0, None),
    )

    class _EW:
        @staticmethod
        def _engine_indicator_map():
            return {
                "AAA": {
                    "pctr": -50.0, "pctr_rising": True, "pctr_falling": False,
                    "cm_rsi": 22.0, "cm_rsi_rising": True,
                },
            }

        @staticmethod
        def load_watch():
            return {}

        @staticmethod
        def live_exhaustion(*a, **k):
            return None

        @staticmethod
        def exhaustion_allows_buy(record, cfg):
            return True, "stub_exh"


    monkeypatch.setitem(__import__("sys").modules, "ai_entry_watch", _EW)
    out = pb.refresh_seat_indicators(cfg=CFG_ON, now=_ts(8, 1))
    ind = out["AAA"]["indicator"]
    assert ind.get("pctr_rising") is True
    # Normalized exhaustion present for arm band.
    assert ind.get("exhaustion") == pytest.approx(50.0)
    ok, why = pb.phase_b_arm_allows(out["AAA"], cfg=CFG_ON, now=_ts(8, 1), last=4.0)
    assert ok is True, why


def test_rearm_statuses_include_dry_armed():
    assert "dry_armed" in pb._REARM_STATUSES
    assert "dry_shadow" in pb._REARM_STATUSES


def test_sync_book_logs_admit_refuse(monkeypatch, tmp_path):
    pb.set_book_path_for_tests(tmp_path / "book.json")
    pbl.set_ledger_path_for_tests(tmp_path / "phase_b.jsonl")
    # Fill seats so next candidate is refused for capacity / source.
    cfg = dict(CFG_ON, ai_phase_b_max_seats=1)
    monkeypatch.setattr(pb, "fresh_stream_last", lambda *a, **k: (5.0, None))
    monkeypatch.setattr(
        pb, "gather_candidates",
        lambda *a, **k: [
            {"symbol": "ONE", "source": "momentum", "price": 5.0},
            {"symbol": "TWO", "source": "trending", "price": 5.0},
        ],
    )
    summary = pb.sync_book(cfg=cfg, now=_ts(8, 0), open_count=0)
    assert summary["admitted"] or summary["refused"]
    rows = pbl.read_day(path=tmp_path / "phase_b.jsonl")
    # At least one admit or refuse row landed.
    kinds = {r.get("kind") for r in rows}
    assert "admit" in kinds or "admit_refuse" in kinds


def test_live_paper_entry_calls_place_when_dry_off(monkeypatch, tmp_path):
    placed = []
    cfg = dict(CFG_ON, ai_phase_b_dry_run=False)
    monkeypatch.setattr(pb, "fresh_stream_last", lambda *a, **k: (3.0, None))
    rec = {
        "symbol": "PAP",
        "source": "momentum",
        "indicator": {
            "pctr": 55.0, "pctr_rising": True,
            "cm_rsi": 25.0, "cm_rsi_rising": True,
        },
    }
    out = pb.try_phase_b_entry(
        rec, equity=10_000, cfg=cfg, now=_ts(8, 0),
        place_fn=lambda *a, **k: placed.append(1) or {
            "ok": True, "buy_order_id": "x",
        },
    )
    assert out["ok"] is True
    assert out.get("broker") is True
    assert placed == [1]


def test_book_rows_for_dashboard_during_premarket(monkeypatch, tmp_path):
    pb.set_book_path_for_tests(tmp_path / "phase_b_book.json")
    try:
        book = {
            "CHOW": {
                "symbol": "CHOW",
                "source": "movers",
                "price": 4.40,
                "score": 106.5,
                "pct_change": 106.5,
                "status": "watching",
                "indicator": {"exhaustion": 50.0, "cm_rsi": 45.0},
            },
            "GDYN": {
                "symbol": "GDYN",
                "source": "movers",
                "price": 13.36,
                "score": 40.8,
                "pct_change": 40.8,
                "status": "dry_armed",
                "indicator": {"exhaustion": 60.0, "cm_rsi": 50.0},
            },
        }
        pb.save_book(book)
        cfg = dict(CFG_ON, ai_phase_b_enabled=True)

        # 1. During premarket hours: returns formatted rows
        rows = pb.book_rows_for_dashboard(cfg=cfg, now=_ts(8, 0))
        assert len(rows) == 2
        sym_map = {r["symbol"]: r for r in rows}
        assert "CHOW" in sym_map
        assert sym_map["CHOW"]["price"] == 4.40
        assert sym_map["CHOW"]["phase_b"] is True
        assert sym_map["GDYN"]["ready"] is True
        assert sym_map["GDYN"]["blocker"] == "dry armed"

        # 2. Outside session (e.g. 09:30 RTH): returns empty list
        rth_rows = pb.book_rows_for_dashboard(cfg=cfg, now=_ts(9, 31))
        assert rth_rows == []
    finally:
        pb.set_book_path_for_tests(None)


def test_phase_b_safety_rails():
    pb.clear_phase_b_entry_tracking()
    cfg = dict(
        CFG_ON,
        ai_phase_b_enabled=True,
        ai_phase_b_max_entries_per_symbol=2,
        ai_phase_b_lockout_on_loss=True,
        ai_phase_b_max_spread_pct=0.75,
    )
    t = _ts(7, 30)

    # 1. Spread gate check
    rec_wide = {"symbol": "TEST", "spread_pct": 1.2, "source": "movers"}
    out = pb.try_phase_b_entry(rec_wide, equity=1000.0, cfg=cfg, now=t)
    assert out["ok"] is False
    assert out["error"] == "phase_b_spread_wide"

    # 2. Max entries per symbol check (cap = 2)
    rec_ok = {"symbol": "TEST", "spread_pct": 0.2, "source": "movers"}
    # Simulate 2 prior entries today
    from phase_b import _ENTRIES_BY_DAY_SYM, _LOCKED_OUT_BY_DAY
    from datetime import datetime
    from zoneinfo import ZoneInfo
    day = datetime.fromtimestamp(t, tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    _ENTRIES_BY_DAY_SYM[(day, "TEST")] = 2

    out_cap = pb.try_phase_b_entry(rec_ok, equity=1000.0, cfg=cfg, now=t)
    assert out_cap["ok"] is False
    assert out_cap["error"] == "phase_b_max_entries"

    # Also admit_candidate rejects on max entries
    ok_admit, why_admit = pb.admit_candidate(rec_ok, {}, cfg=cfg, now=t)
    assert ok_admit is False
    assert why_admit == "phase_b_max_entries"

    # 3. Loss lockout check
    _LOCKED_OUT_BY_DAY.setdefault(day, set()).add("LOCKED")
    rec_locked = {"symbol": "LOCKED", "source": "movers"}
    out_lock = pb.try_phase_b_entry(rec_locked, equity=1000.0, cfg=cfg, now=t)
    assert out_lock["ok"] is False
    assert out_lock["error"] == "phase_b_loss_lockout"

    ok_admit_lock, why_admit_lock = pb.admit_candidate(rec_locked, {}, cfg=cfg, now=t)
    assert ok_admit_lock is False
    assert why_admit_lock == "phase_b_loss_lockout"


def test_phase_b_premarket_only_sizing_rails():
    # 1. Getter defaults and config overrides
    assert pb.phase_b_risk_pct() == 0.5
    assert pb.phase_b_max_position_pct() == 4.0
    assert pb.phase_b_max_position_dollars() == 2000.0

    custom_cfg = {
        "ai_phase_b_risk_pct": 0.75,
        "ai_phase_b_max_position_pct": 3.0,
        "ai_phase_b_max_position_dollars": 1500.0,
    }
    assert pb.phase_b_risk_pct(custom_cfg) == 0.75
    assert pb.phase_b_max_position_pct(custom_cfg) == 3.0
    assert pb.phase_b_max_position_dollars(custom_cfg) == 1500.0

    # 2. Verify try_phase_b_entry passes pb_risk into place_fn
    passed_args = {}
    def mock_place(sym, decision, equity, **kw):
        passed_args.update(kw)
        passed_args["sym"] = sym
        passed_args["decision"] = decision
        passed_args["equity"] = equity
        return {"ok": True, "buy_order_id": "test_pb_order"}

    pb.clear_phase_b_entry_tracking()
    cfg = dict(
        CFG_ON,
        ai_phase_b_enabled=True,
        ai_phase_b_dry_run=False,
        ai_phase_b_risk_pct=0.5,
        ai_phase_b_require_exh_rising=False,
    )
    rec = {
        "symbol": "PREM",
        "spread_pct": 0.1,
        "source": "movers",
        "indicator": {"exhaustion": 55.0, "cm_rsi": 45.0},
    }
    t = _ts(7, 30)
    with patch("phase_b.fresh_stream_last", return_value=(10.0, "ok")):
        out = pb.try_phase_b_entry(rec, equity=50000.0, cfg=cfg, now=t, place_fn=mock_place)

    assert out["ok"] is True
    assert passed_args.get("risk_pct") == 0.5
    assert passed_args.get("decision", {}).get("phase_b") is True

    # 3. Test place_scaled_entry behavior: Premarket vs RTH
    import ai_positions as cp

    cfg_rth = {
        "ai_size_from_free_equity": False,
        "ai_max_position_pct": 8.0,
        "ai_phase_b_max_position_pct": 4.0,
        "ai_phase_b_max_position_dollars": 2000.0,
    }
    rth_decision = {
        "phase_b": False,
        "entry_low": 20.0,
        "entry_high": 20.0,
        "stop_price": 19.0,
        "target_1": 22.0,
        "skip_zone": True,
    }
    pm_decision = {
        "phase_b": True,
        "entry_low": 20.0,
        "entry_high": 20.0,
        "stop_price": 19.0,
        "target_1": 22.0,
        "skip_zone": True,
    }

    with patch("alpaca_trader.market_is_open", return_value=True), \
         patch("ai_positions._buying_power", return_value=100000.0), \
         patch("alpaca_trader.get_open_positions", return_value={}), \
         patch("alpaca_trader.cancel_open_orders", return_value=None), \
         patch("ai_positions._open_book_snapshot", return_value=(0.0, 0)), \
         patch("ai_positions._entry_cfg", return_value=cfg_rth), \
         patch("phase_b.enabled", return_value=True), \
         patch("phase_b.dry_run", return_value=False), \
         patch("phase_b.phase_b_allow_entries", return_value=True), \
         patch("alpaca_trader.size_by_risk", return_value=500):

        captured_orders = []
        def mock_buy_order(ticker, qty, **kw):
            captured_orders.append((ticker, qty))
            return {"ok": True, "buy_order_id": "rth_order", "status": "new", "qty": qty}

        def mock_buy_limit(ticker, lim, **kw):
            qty = kw.get("qty")
            captured_orders.append((ticker, qty))
            return {"ok": True, "order_id": "pm_order", "status": "new", "qty": qty}

        with patch("alpaca_trader.buy_limit_bracket", side_effect=mock_buy_order), \
             patch("alpaca_trader.buy_bracket_exact", side_effect=mock_buy_order), \
             patch("alpaca_trader.buy_limit_at_price", side_effect=mock_buy_limit):

            # RTH: 8% of 50k = $4,000 / $20 = 200 shares
            res_rth = cp.place_scaled_entry("RTHS", rth_decision, account_equity=50000.0, current_ask=20.0, risk_pct=1.0)
            assert res_rth.get("ok") is True
            assert captured_orders[-1] == ("RTHS", 200.0)

            # Pre-market: 4% of 50k = $2,000 / $20 = 100 shares (or $2,000 dollar cap / $20 = 100 shares)
            res_pm = cp.place_scaled_entry("PREM", pm_decision, account_equity=50000.0, current_ask=20.0, risk_pct=0.5)
            assert res_pm.get("ok") is True
            assert captured_orders[-1] == ("PREM", 100.0)



