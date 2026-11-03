"""Tests for Phase B pre-market parity with RTH package:
1. presquare_only gate
2. 4m post-square cooldown gate (after_square_refusal)
3. 5m Slow %R pitch veto gate (wr_slow_5m_refusal)
4. 60s re-entry cooldown gate (ai_reentry_cooldown_sec)
5. 5m smart zombie cut working sell routing
6. First-leg profit capture (+0.20%) working sell routing
7. Dashboard live gate badges
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest

import ai_entry_watch as ew
import ai_positions as cp
import phase_b as pb
import alpaca_trader as tr
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

ET = ZoneInfo("America/New_York")


def _cfg(**over):
    cfg = {
        "ai_phase_b_enabled": True,
        "ai_phase_b_working_sell": True,
        "ai_phase_b_dry_run": False,
        "ai_phase_b_start_time": "04:00",
        "ai_phase_b_entry_cutoff": "09:20",
        "ai_phase_b_print_max_age_sec": 15.0,
        "ai_watch_exh_square_arm": True,
        "ai_watch_exh_presquare_arm": True,
        "ai_watch_presquare_only": True,
        "ai_watch_presquare_after_square_min": 4.0,
        "ai_watch_wr_slow_5m_veto": True,
        "ai_reentry_cooldown_sec": 60.0,
        "rte_threshold": 20,
        "rte_confluence_max": 15.0,
        "rte_require_tight": True,
        "ai_watch_exhaustion_rules": True,
        "ai_watch_require_exh_rising": True,
        "ai_watch_square_min_count": 1,
    }
    cfg.update(over)
    return cfg


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(pb, "fresh_stream_last", lambda sym, now=None, cfg=None: (10.0, None))
    monkeypatch.setattr(ew, "_recent_exit_ts", lambda sym: None)
    ew._SQUARE_STREAK.clear()
    ew._LAST_SQUARE.clear()
    yield
    ew._SQUARE_STREAK.clear()
    ew._LAST_SQUARE.clear()


def test_phase_b_presquare_only_refuses_full_square():
    """Candidates in a full square (both >= -20) must be rejected with not_presquare when presquare_only is set."""
    t0 = dt.datetime(2026, 10, 20, 6, 0, tzinfo=ET).timestamp()
    rec = {
        "symbol": "AAA",
        "indicator": {
            "pctr": -5.0,
            "pctr_slow": -8.0,
            "pctr_rising": True,
            "pctr_falling": False,
            "cm_rsi": 30.0,
        },
    }
    ok, why = pb.phase_b_arm_allows(rec, cfg=_cfg(ai_watch_presquare_only=True), now=t0)
    assert ok is False
    assert why == "not_presquare"


def test_phase_b_presquare_only_allows_presquare():
    """Candidates approaching overbought in pre-square band (both >= -35, tight, rising) must be allowed."""
    t0 = dt.datetime(2026, 10, 20, 6, 0, tzinfo=ET).timestamp()
    rec = {
        "symbol": "AAA",
        "indicator": {
            "pctr": -25.0,
            "pctr_slow": -28.0,
            "pctr_rising": True,
            "pctr_falling": False,
            "pctr_slow_rising": True,
            "cm_rsi": 30.0,
        },
    }
    cfg = _cfg(ai_watch_presquare_only=True, ai_watch_wr_slow_5m_veto=False)
    ok, why = pb.phase_b_arm_allows(rec, cfg=cfg, now=t0)
    assert ok is True
    assert why == "phase_b_arm"


def test_phase_b_after_square_refusal_gates():
    """Candidate in pre-square that recently exited a full square within 4m must be blocked by after_square_4m."""
    t0 = dt.datetime(2026, 10, 20, 6, 0, tzinfo=ET).timestamp()
    # Record a square 60s ago
    ew._LAST_SQUARE["AAA"] = [t0 - 60.0, t0 - 60.0]

    rec = {
        "symbol": "AAA",
        "indicator": {
            "pctr": -25.0,
            "pctr_slow": -28.0,
            "pctr_rising": True,
            "pctr_falling": False,
            "pctr_slow_rising": True,
            "cm_rsi": 30.0,
        },
    }
    cfg = _cfg(ai_watch_presquare_only=True, ai_watch_presquare_after_square_min=4.0)
    ok, why = pb.phase_b_arm_allows(rec, cfg=cfg, now=t0)
    assert ok is False
    assert why == "presquare_after_square"


def test_phase_b_slow_5m_veto_gates(monkeypatch):
    """When 5m Slow %R pitch veto is active and slow line is pitched down, entry must be refused."""
    t0 = dt.datetime(2026, 10, 20, 6, 0, tzinfo=ET).timestamp()
    monkeypatch.setattr(ew, "wr_slow_5m_refusal", lambda sym, rec, now, cfg: "wr_slow_5m_falling")

    rec = {
        "symbol": "AAA",
        "indicator": {
            "pctr": -25.0,
            "pctr_slow": -28.0,
            "pctr_rising": True,
            "pctr_falling": False,
            "pctr_slow_rising": True,
            "cm_rsi": 30.0,
        },
    }
    cfg = _cfg(ai_watch_presquare_only=True, ai_watch_wr_slow_5m_veto=True)
    ok, why = pb.phase_b_arm_allows(rec, cfg=cfg, now=t0)
    assert ok is False
    assert why == "wr_slow_5m_falling"


def test_phase_b_reentry_cooldown_gates(monkeypatch):
    """Re-entry within cooldown window (60s) must be refused."""
    t0 = dt.datetime(2026, 10, 20, 6, 0, tzinfo=ET).timestamp()
    monkeypatch.setattr(ew, "_recent_exit_ts", lambda sym: t0 - 30.0)  # exited 30 seconds ago

    rec = {
        "symbol": "AAA",
        "indicator": {
            "pctr": -25.0,
            "pctr_slow": -28.0,
            "pctr_rising": True,
            "pctr_falling": False,
            "pctr_slow_rising": True,
            "cm_rsi": 30.0,
        },
    }
    cfg = _cfg(ai_watch_presquare_only=True, ai_reentry_cooldown_sec=60.0)
    ok, why = pb.phase_b_arm_allows(rec, cfg=cfg, now=t0)
    assert ok is False
    assert why == "reentry_cooldown"


def test_phase_b_smart_dead_cut_working_sell(monkeypatch):
    """5m zombie loser in Phase B must be cut via working limit sell at the bid, not market-dumped."""
    t0 = dt.datetime(2026, 10, 20, 6, 0, tzinfo=ET).timestamp()
    orders_submitted = []

    fake_client = MagicMock()
    fake_client.get_orders.return_value = []
    fake_client.submit_order.side_effect = lambda req: (
        orders_submitted.append(req) or SimpleNamespace(id="ws-cut-1", status="accepted")
    )
    fake_client.get_open_position.return_value = SimpleNamespace(qty="100.0", current_price="9.90")

    monkeypatch.setattr(tr, "_host_allowed", lambda: True)
    monkeypatch.setattr(tr, "_client", fake_client)
    monkeypatch.setattr(tr, "_mode", "paper")
    monkeypatch.setattr(tr, "cancel_open_orders", lambda sym: {"canceled": 1})
    monkeypatch.setattr(cp, "_premarket_bid", lambda sym: 9.88)
    monkeypatch.setattr(cp, "_rth_now", lambda ts: False)
    monkeypatch.setattr(tr, "market_is_open", lambda: False)
    monkeypatch.setattr(pb, "fresh_stream_last", lambda sym, now=None, cfg=None: (9.90, "stream"))

    pos = {
        "symbol": "ZOM",
        "phase_b": True,
        "entry_confirmed": True,
        "entry_confirmed_at": t0 - 350.0,  # 350s age > 300s
        "entry_price": 10.00,
        "last_seen_price": 9.90,            # -1.0% loss <= -0.15%
        "peak_price": 10.001,               # +0.01% MFE <= +0.02%
        "local_stop_price": 9.50,
        "indicator": {"pctr": -85.0},       # dead %R <= -70
    }

    cfg = _cfg(ai_smart_dead_cut_sec=300.0)
    events = pb.tick(cfg=cfg, now=t0, open_positions={"ZOM": pos})

    cut_events = [e for e in events if e.get("kind") == "smart_dead_cut"]
    assert len(cut_events) == 1
    assert pos.get("working_sell_state") == "flatten"
    assert pos.get("working_sell_px") == 9.88
    assert len(orders_submitted) == 1
    ord1 = orders_submitted[0]
    assert ord1.symbol == "ZOM"
    assert int(ord1.qty) == 100
    assert float(ord1.limit_price) == 9.88
    assert ord1.extended_hours is True
    assert ord1.time_in_force == TimeInForce.DAY


def test_phase_b_profit_capture_working_sell(monkeypatch):
    """First-leg profit capture (+0.20%) in Phase B must place a working sell at bid."""
    orders_submitted = []
    fake_client = MagicMock()
    fake_client.get_orders.return_value = []
    fake_client.submit_order.side_effect = lambda req: (
        orders_submitted.append(req) or SimpleNamespace(id="ws-prof-1", status="accepted")
    )
    fake_client.get_open_position.return_value = SimpleNamespace(qty="50.0", current_price="10.03")

    monkeypatch.setattr(tr, "_host_allowed", lambda: True)
    monkeypatch.setattr(tr, "_client", fake_client)
    monkeypatch.setattr(tr, "_mode", "paper")
    monkeypatch.setattr(tr, "cancel_open_orders", lambda sym: {"canceled": 1})
    monkeypatch.setattr(cp, "_premarket_bid", lambda sym: 10.03)
    monkeypatch.setattr(cp, "_rth_now", lambda ts: False)
    monkeypatch.setattr(tr, "market_is_open", lambda: False)

    pos = {
        "symbol": "WIN",
        "phase_b": True,
        "entry_confirmed": True,
        "entry_price": 10.00,
        "local_stop_price": 9.60,
        "peak_price": 10.03,
    }

    events = []
    exit_why = {}
    changed, closed = cp.apply_local_trail("WIN", pos, 10.03, events, exit_why)

    assert closed is False
    assert pos.get("working_sell_state") == "flatten"
    assert pos.get("working_sell_px") == 10.03
    assert len(orders_submitted) == 1
    ord1 = orders_submitted[0]
    assert ord1.symbol == "WIN"
    assert int(ord1.qty) == 50
    assert float(ord1.limit_price) == 10.03
    assert ord1.extended_hours is True


def test_phase_b_dashboard_gate_badges(monkeypatch):
    """Unified dashboard rows during Phase B must populate gate badges (g_spread, g_sq_wait, g_cd_wait, g_s5_veto)."""
    t0 = dt.datetime(2026, 10, 20, 6, 0, tzinfo=ET).timestamp()
    pb.set_book_path_for_tests(None)
    book = {
        "AAA": {
            "symbol": "AAA",
            "price": 10.00,
            "status": "watching",
            "source": "movers",
            "pct_change": 5.0,
            "score": 10.0,
            "indicator": {"cm_rsi": 25.0, "pctr": -20.0, "pctr_slow": -25.0},
        }
    }
    pb.save_book(book)
    rows = pb.book_rows_for_dashboard(cfg=_cfg(), now=t0)
    assert len(rows) == 1
    r = rows[0]
    assert "g_spread" in r
    assert "g_spread_max" in r
    assert "g_sq_wait" in r
    assert "g_cd_wait" in r
    assert "g_s5_veto" in r
    assert "g_cm_rsi" in r
