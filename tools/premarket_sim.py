#!/usr/bin/env python3
"""Interactive Pre-Market Simulation & Verification Tool.

Simulates a complete pre-market trading session (04:00 - 09:30 ET) through:
- Data feed resolution (SIP vs IEX, delay zeroing)
- Multi-tier quote resolution ladder
- Candidate screening & Phase B admission
- Dynamic trailing stops & resting DAY limit sells
- Flatten window execution (09:25 - 09:28 ET)
- Fractional share safeguarding outside RTH
- 09:30 ET Opening Bell RTH handoff & isolation

Usage:
    .venv/bin/python tools/premarket_sim.py
"""
from __future__ import annotations

import sys
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock
from types import SimpleNamespace

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import phase_b as pb
import ai_positions as cp
import alpaca_trader as tr
import alpaca_api as aa
import tight_screener as ts
import ai_entry_watch as ew
import trade_bridge.providers.alpaca as bridge_alpaca
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

ET = ZoneInfo("America/New_York")

GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_step(title: str):
    print(f"\n{BOLD}{CYAN}════════════════════════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{BLUE}▶ {title}{RESET}")
    print(f"{BOLD}{CYAN}════════════════════════════════════════════════════════════════════{RESET}")


def run_simulation():
    print(f"{BOLD}PRE-MARKET & SIP READINESS LIFECYCLE SIMULATION{RESET}")
    print(f"Time zone: Eastern (America/New_York)\n")

    # ──────────────────────────────────────────────────────────────────────────
    # STAGE 1: Config & SIP Auto-Switch
    # ──────────────────────────────────────────────────────────────────────────
    print_step("STAGE 1: Market Data Feed Configuration & Delay Zeroing")
    cfg_iex = {"alpaca_bar_feed": "iex"}
    cfg_sip = {"alpaca_bar_feed": "sip"}

    feed_iex = bridge_alpaca.feed_kw(cfg_iex)["feed"]
    feed_sip = bridge_alpaca.feed_kw(cfg_sip)["feed"]
    print(f"  • IEX Config  -> Provider feed: {feed_iex.value:<4} | Realtime: {aa.sip_is_realtime(cfg_iex)}")
    print(f"  • SIP Config  -> Provider feed: {feed_sip.value:<4} | Realtime: {aa.sip_is_realtime(cfg_sip)}")

    t_0932 = datetime(2026, 10, 20, 9, 32, tzinfo=ET)
    in_scan_iex = ts.in_scan_window(t_0932, cfg_iex)
    in_scan_sip = ts.in_scan_window(t_0932, cfg_sip)
    print(f"  • Screener gate at 09:32 ET on IEX (15m delay): allowed={in_scan_iex} (must be False)")
    print(f"  • Screener gate at 09:32 ET on SIP (0m delay):  allowed={in_scan_sip} (must be True)")
    assert not in_scan_iex and in_scan_sip
    print(f"  {GREEN}✓ Data feed switching and zero-delay gates verified.{RESET}")

    # ──────────────────────────────────────────────────────────────────────────
    # STAGE 2: 04:30 ET - Session Initialization & Sparse Tape Fallback
    # ──────────────────────────────────────────────────────────────────────────
    print_step("STAGE 2: 04:30 ET - Phase B Active & 4-Tier Tape Resolution")
    t_0430 = datetime(2026, 10, 20, 4, 30, tzinfo=ET).timestamp()
    cfg = {
        "ai_phase_b_enabled": True,
        "ai_phase_b_working_sell": True,
        "ai_phase_b_dry_run": False,
        "alpaca_bar_feed": "sip",
    }
    print(f"  • Phase B active at 04:30 ET: {pb.phase_b_session_active(t_0430, cfg)}")
    print(f"  • Allow entries at 04:30 ET:  {pb.phase_b_allow_entries(t_0430, cfg)}")
    print(f"  • Flatten active at 04:30 ET: {pb.phase_b_flatten_active(t_0430, cfg)}")

    # Test multi-tier fallback: Finnhub empty, live_print active
    orig_lp = getattr(ew, "live_print", None)
    ew.live_print = lambda sym: (14.25, 2.1)
    resolved = pb.stream_last_print("NVDA", now=t_0430, cfg=cfg)
    print(f"  • Stream last print for NVDA resolved via Tier 2 (live_print):")
    print(f"    price=${resolved['price']:.2f}, age={resolved['age_sec']}s, src={resolved['src']}")
    assert resolved["src"] == "live_print" and resolved["price"] == 14.25
    if orig_lp: ew.live_print = orig_lp
    print(f"  {GREEN}✓ Pre-market stream fallback resolution verified.{RESET}")

    # ──────────────────────────────────────────────────────────────────────────
    # STAGE 3: 07:30 ET - Stop Ratchet & Pre-Market Working Sell Execution
    # ──────────────────────────────────────────────────────────────────────────
    print_step("STAGE 3: 07:30 ET - Stop Ratchet & Premarket Working Sell Placement")
    orders = []
    fake_client = MagicMock()
    fake_client.get_orders.return_value = []
    fake_client.submit_order.side_effect = lambda req: orders.append(req) or SimpleNamespace(id="ws-1", status="accepted")
    tr._host_allowed = lambda: True
    tr._client = fake_client
    tr._mode = "paper"
    tr.market_is_open = lambda: False
    cp._rth_now = lambda ts: False
    cp._premarket_bid = lambda sym: 14.80

    pos = {
        "symbol": "NVDA",
        "phase_b": True,
        "entry_confirmed": True,
        "entry_price": 14.00,
        "local_stop_price": 14.85,
        "peak_price": 15.50,
        "risk_per_share": 0.50,
        "mfe_r": 3.0,
    }
    fake_client.get_open_position.return_value = SimpleNamespace(qty="200", current_price="14.80")

    print(f"  • Position: NVDA (Phase B lot) | Entry: $14.00 | Shelf Stop: $14.85 | Peak: $15.50")
    print(f"  • Tape tick prints down to $14.75 (violates $14.85 shelf stop)...")
    changed, closed = cp.apply_local_trail("NVDA", pos, 14.75, [], {})

    print(f"  • apply_local_trail result: closed={closed} (False = not dumped at market)")
    print(f"  • Position state updated to: working_sell_state='{pos.get('working_sell_state')}'")
    assert len(orders) == 1
    req = orders[0]
    print(f"  • Broker order placed:")
    print(f"    - Type:            LimitOrderRequest")
    print(f"    - Side:            {req.side.value}")
    print(f"    - Symbol:          {req.symbol}")
    print(f"    - Limit Price:     ${float(req.limit_price):.2f} (placed at premarket bid $14.80)")
    print(f"    - Qty:             {req.qty}")
    print(f"    - Extended Hours:  {req.extended_hours}")
    print(f"    - Time in Force:   {req.time_in_force.value}")
    assert req.extended_hours is True and req.time_in_force == TimeInForce.DAY
    print(f"  {GREEN}✓ Premarket working sell safely placed as extended-hours DAY limit order.{RESET}")

    # ──────────────────────────────────────────────────────────────────────────
    # STAGE 4: 09:26 ET - Pre-Bell Flatten Window Execution
    # ──────────────────────────────────────────────────────────────────────────
    print_step("STAGE 4: 09:26 ET - Pre-Bell Flatten Window Execution (09:25 - 09:28 ET)")
    t_0926 = datetime(2026, 10, 20, 9, 26, tzinfo=ET).timestamp()
    pos_open = {
        "symbol": "AMD",
        "phase_b": True,
        "entry_confirmed": True,
        "entry_price": 80.00,
        "last_seen_price": 81.50,
    }
    fake_client.get_open_position.return_value = SimpleNamespace(qty="150", current_price="81.50")
    pb.fresh_stream_last = lambda sym, now=None, cfg=None: (81.50, "stream")

    print(f"  • Clock reaches 09:26 ET (Phase B flatten active: {pb.phase_b_flatten_active(t_0926, cfg)})")
    print(f"  • Open Phase B position 'AMD' detected in book. Triggering Phase B maintenance tick...")
    pb.tick(cfg=cfg, now=t_0926, open_positions={"AMD": pos_open})

    assert len(orders) == 2
    flatten_req = orders[1]
    print(f"  • Flatten order placed:")
    print(f"    - Symbol:          {flatten_req.symbol}")
    print(f"    - Limit Price:     ${float(flatten_req.limit_price):.2f}")
    print(f"    - Qty:             {flatten_req.qty}")
    print(f"    - Extended Hours:  {flatten_req.extended_hours}")
    print(f"  {GREEN}✓ Flatten window successfully executed working sell before opening bell.{RESET}")

    # ──────────────────────────────────────────────────────────────────────────
    # STAGE 5: Fractional Share Guard Outside RTH
    # ──────────────────────────────────────────────────────────────────────────
    print_step("STAGE 5: Fractional Share Guarding Outside RTH")
    print("  • Attempting off-hours sell of 45.85 shares...")
    fake_client.get_open_position.return_value = SimpleNamespace(qty="45.85", current_price="10.0")
    res_frac = tr.close_out("TEST", price=10.0)
    ord_frac = orders[2]
    print(f"    Submitted Qty: {ord_frac.qty} (automatically clamped from 45.85 to whole shares)")
    assert int(ord_frac.qty) == 45

    print("  • Attempting off-hours sell of 0.65 shares (under 1 whole share)...")
    fake_client.get_open_position.return_value = SimpleNamespace(qty="0.65", current_price="10.0")
    res_under = tr.close_out("UNDER", price=10.0)
    print(f"    Status: {res_under['status']} | Note: {res_under['note']}")
    assert res_under["status"] == "qty_under_1"
    print(f"  {GREEN}✓ Off-hours fractional share protection verified.{RESET}")

    # ──────────────────────────────────────────────────────────────────────────
    # STAGE 6: 09:30 ET - Opening Bell Handoff & Plan A RTH Isolation
    # ──────────────────────────────────────────────────────────────────────────
    print_step("STAGE 6: 09:30 ET - Opening Bell Handoff & Plan A Isolation")
    t_0930 = datetime(2026, 10, 20, 9, 30, tzinfo=ET).timestamp()
    cp._rth_now = lambda ts: True
    tr.market_is_open = lambda: True

    canceled = []
    tr.cancel_open_orders = lambda sym: canceled.append(sym) or {"canceled": 1}

    # Handoff rests
    pos_lingering = {"working_sell_id": "ws-1", "working_sell_state": "flatten"}
    handoff_ok = cp.handoff_working_sell_to_rth("NVDA", pos_lingering)
    print(f"  • Handoff lingering pre-market sell order to RTH: {handoff_ok}")
    print(f"  • Cancel open orders called for: {canceled}")
    print(f"  • Lingering working_sell_id removed from state: {'working_sell_id' not in pos_lingering}")
    assert handoff_ok and "NVDA" in canceled and "working_sell_id" not in pos_lingering

    # Verify RTH exit isolation
    pos_rth = {"entry_confirmed": True, "phase_b": True, "ticker": "SPY"}
    rth_active = cp._premarket_working_sell_on(pos_rth)
    print(f"  • Is premarket working sell active during RTH: {rth_active} (MUST be False)")
    assert rth_active is False
    print(f"  {GREEN}✓ Opening bell RTH handoff and strict exit isolation verified.{RESET}")

    print(f"\n{BOLD}{GREEN}════════════════════════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{GREEN}ALL PRE-MARKET LEVELS TESTED AND FULLY VERIFIED PASSING!{RESET}")
    print(f"{BOLD}{GREEN}════════════════════════════════════════════════════════════════════{RESET}\n")


if __name__ == "__main__":
    run_simulation()
