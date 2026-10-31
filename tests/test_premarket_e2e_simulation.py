"""End-to-End Pre-market Simulation Harness.

Tests the entire pre-market lifecycle across all layers:
1. Candidate Admission (04:00 - 09:00 ET)
2. Multi-tier tape resolution during sparse prints
3. Stop ratchet & Local Trail trigger outside RTH
4. Resting DAY limit sell placement at the bid (extended_hours=True)
5. Flatten window activation (09:25 - 09:28 ET)
6. Alpaca order schema compliance (strictly limit, DAY, integer qty, extended_hours)
7. Opening bell isolation at 09:30 ET (RTH handoff cancels resting sells, Plan A exits untouched)
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest
import phase_b as pb
import ai_positions as cp
import alpaca_trader as tr
import alpaca_api as aa
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

ET = ZoneInfo("America/New_York")


def test_e2e_premarket_full_lifecycle(monkeypatch):
    orders_submitted = []
    canceled_orders = []

    # Mock broker
    fake_client = MagicMock()
    fake_client.get_orders.return_value = []

    def mock_submit_order(req):
        orders_submitted.append(req)
        return SimpleNamespace(id=f"order-{len(orders_submitted)}", status="accepted")

    fake_client.submit_order.side_effect = mock_submit_order
    monkeypatch.setattr(tr, "_host_allowed", lambda: True)
    monkeypatch.setattr(tr, "_client", fake_client)
    monkeypatch.setattr(tr, "_mode", "paper")
    monkeypatch.setattr(tr, "cancel_open_orders", lambda sym: canceled_orders.append(sym) or {"canceled": 1})

    # ──────────────────────────────────────────────────────────────────────────
    # LEVEL 1: 04:30 ET - Pre-Market Session & Admission
    # ──────────────────────────────────────────────────────────────────────────
    t_0430 = datetime(2026, 10, 20, 4, 30, tzinfo=ET).timestamp()
    cfg = {
        "ai_phase_b_enabled": True,
        "ai_phase_b_working_sell": True,
        "ai_phase_b_dry_run": False,
        "alpaca_bar_feed": "sip",
    }
    assert pb.phase_b_session_active(t_0430, cfg) is True
    assert pb.phase_b_allow_entries(t_0430, cfg) is True
    assert pb.phase_b_flatten_active(t_0430, cfg) is False

    # ──────────────────────────────────────────────────────────────────────────
    # LEVEL 2: Multi-Tier Quote Resolution (Finnhub empty -> Live Print fallback)
    # ──────────────────────────────────────────────────────────────────────────
    import finnhub_stream as fh
    with fh.FINNHUB_STATE.lock:
        fh.FINNHUB_STATE.prices.clear()

    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "live_print", lambda sym: (10.50, 1.5))

    tape_quote = pb.stream_last_print("XYZ", now=t_0430, cfg=cfg)
    assert tape_quote is not None
    assert tape_quote["price"] == 10.50
    assert tape_quote["src"] == "live_print"

    # ──────────────────────────────────────────────────────────────────────────
    # LEVEL 3: 07:30 ET - Position Trail Hit & Premarket Working Sell Placement
    # ──────────────────────────────────────────────────────────────────────────
    t_0730 = datetime(2026, 10, 20, 7, 30, tzinfo=ET).timestamp()
    fake_client.get_open_position.return_value = SimpleNamespace(qty="50.0", current_price="10.20")

    pos = {
        "symbol": "XYZ",
        "phase_b": True,
        "entry_confirmed": True,
        "entry_price": 10.00,
        "local_stop_price": 10.20,
        "peak_price": 10.60,
        "risk_per_share": 0.40,
        "mfe_r": 1.5,
    }

    # Ensure premarket bid exists
    monkeypatch.setattr(cp, "_premarket_bid", lambda _sym: 10.15)
    monkeypatch.setattr(cp, "_rth_now", lambda _ts: False)
    monkeypatch.setattr(tr, "market_is_open", lambda: False)

    # Price drops to 10.10 (breaking 10.20 stop)
    events = []
    exit_why = {}
    changed, closed = cp.apply_local_trail("XYZ", pos, 10.10, events, exit_why)

    # Assertions:
    # 1. Closed is False because it's resting a working limit sell, not market-dumped
    assert closed is False
    assert pos.get("working_sell_state") == "flatten"
    assert pos.get("working_sell_px") == 10.15
    assert len(orders_submitted) == 1

    ord1 = orders_submitted[0]
    assert isinstance(ord1, LimitOrderRequest)
    assert ord1.symbol == "XYZ"
    assert int(ord1.qty) == 50
    assert float(ord1.limit_price) == 10.15
    assert ord1.side == OrderSide.SELL
    assert ord1.time_in_force == TimeInForce.DAY
    assert ord1.extended_hours is True

    # ──────────────────────────────────────────────────────────────────────────
    # LEVEL 4: 09:26 ET - Flatten Window Forced Working Sell
    # ──────────────────────────────────────────────────────────────────────────
    t_0926 = datetime(2026, 10, 20, 9, 26, tzinfo=ET).timestamp()
    assert pb.phase_b_flatten_active(t_0926, cfg) is True

    # Position 2 was still held approaching the bell
    pos2 = {
        "symbol": "ABC",
        "phase_b": True,
        "entry_confirmed": True,
        "entry_price": 25.00,
        "last_seen_price": 25.40,
    }
    fake_client.get_open_position.return_value = SimpleNamespace(qty="100.0", current_price="25.40")
    monkeypatch.setattr(pb, "fresh_stream_last", lambda sym, now=None, cfg=None: (25.40, "stream"))

    pb.tick(
        cfg=cfg,
        now=t_0926,
        open_positions={"ABC": pos2},
    )

    assert len(orders_submitted) == 2
    ord2 = orders_submitted[1]
    assert ord2.symbol == "ABC"
    assert int(ord2.qty) == 100
    assert float(ord2.limit_price) == 25.40
    assert ord2.extended_hours is True
    assert ord2.time_in_force == TimeInForce.DAY

    # ──────────────────────────────────────────────────────────────────────────
    # LEVEL 5: Fractional Share Guard Outside RTH
    # ──────────────────────────────────────────────────────────────────────────
    fake_client.get_open_position.return_value = SimpleNamespace(qty="12.75", current_price="50.0")
    res_frac = tr.close_out("FRAC", price=50.0)
    assert res_frac["ok"] is True
    ord3 = orders_submitted[2]
    assert ord3.qty == 12  # Clamped to integer 12!

    fake_client.get_open_position.return_value = SimpleNamespace(qty="0.45", current_price="50.0")
    res_under = tr.close_out("UNDER", price=50.0)
    assert res_under["ok"] is False
    assert res_under["status"] == "qty_under_1"

    # ──────────────────────────────────────────────────────────────────────────
    # LEVEL 6: 09:30 ET - Opening Bell Handoff & RTH Protection
    # ──────────────────────────────────────────────────────────────────────────
    t_0930 = datetime(2026, 10, 20, 9, 30, tzinfo=ET).timestamp()
    monkeypatch.setattr(cp, "_rth_now", lambda _ts: True)
    monkeypatch.setattr(tr, "market_is_open", lambda: True)

    # Premarket working sell must cancel resting order upon RTH handoff
    pos_working = {"working_sell_id": "order-1", "working_sell_state": "flatten"}
    handoff_ok = cp.handoff_working_sell_to_rth("XYZ", pos_working)
    assert handoff_ok is True
    assert "XYZ" in canceled_orders
    assert "working_sell_id" not in pos_working

    # During RTH, premarket working sell adapter NEVER activates
    assert cp._premarket_working_sell_on(pos) is False
