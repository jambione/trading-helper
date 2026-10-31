#!/usr/bin/env python3
"""Friday 2026-10-30 Counterfactual Simulation:
Evaluates the impact of:
  1.A - Spread-Compression Gate (Block trades with quoted spread > $0.01 or > 3 bp)
  1.B - Passive Maker Exit (Exit at midpoint / ask touch rather than crossing spread at bid)
  1.C - Anti-Pullback Gate (Block entries buying into fading pullbacks near/below VWAP)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import numpy as np

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import fill_truth_report as ft
from desk_core import load_desk_env
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockQuotesRequest, StockBarsRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame

ET = ZoneInfo("America/New_York")


def get_client():
    p = os.path.join(ROOT, "secrets_data.json")
    if os.path.exists(p):
        s = json.load(open(p))
        return StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])
    cfg = json.load(open(os.path.join(ROOT, "config", "bot_config.json")))
    return StockHistoricalDataClient(cfg["alpaca_api_key"], cfg["alpaca_secret_key"])


def run_counterfactual():
    load_desk_env()
    cl = get_client()
    
    print("Fetching Friday 2026-10-30 round-trip trades from Alpaca fill logs...")
    fills = ft.fetch_alpaca_fills(days=5, limit=300)
    ai_syms = ft._ai_symbols()
    eng_syms = ft._engine_symbols()
    fills = ft.tag_fills(fills, ai_syms, eng_syms)
    closed = ft._pair_round_trips(fills)
    day_closed = ft.closed_on_day(closed, date(2026, 10, 30))
    print(f"Loaded {len(day_closed)} closed round trips on 2026-10-30.\n")
    
    records = []
    
    for idx, t in enumerate(day_closed):
        sym = t["symbol"]
        b_time_str = t["buy_time"]
        s_time_str = t["sell_time"]
        b_dt = datetime.fromisoformat(b_time_str)
        s_dt = datetime.fromisoformat(s_time_str)
        
        # 1. Fetch SIP quote at entry
        q_entry = cl.get_stock_quotes(StockQuotesRequest(
            symbol_or_symbols=sym,
            start=b_dt - timedelta(seconds=5),
            end=b_dt + timedelta(seconds=2),
            feed=DataFeed.SIP,
        )).data.get(sym) or []
        
        # 2. Fetch SIP quote at exit
        q_exit = cl.get_stock_quotes(StockQuotesRequest(
            symbol_or_symbols=sym,
            start=s_dt - timedelta(seconds=5),
            end=s_dt + timedelta(seconds=2),
            feed=DataFeed.SIP,
        )).data.get(sym) or []
        
        # 3. Fetch 1m bars for intraday VWAP up to entry
        d_day = b_dt.date()
        t0 = datetime(d_day.year, d_day.month, d_day.day, 9, 30, tzinfo=ET)
        bars = cl.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Minute,
            start=t0,
            end=b_dt + timedelta(minutes=1),
            feed=DataFeed.SIP,
        )).data.get(sym) or []
        
        # Calculate entry spread
        valid_b_qs = [q for q in q_entry if q.bid_price and q.ask_price and q.ask_price > q.bid_price]
        if valid_b_qs:
            last_bq = valid_b_qs[-1]
            entry_bid = float(last_bq.bid_price)
            entry_ask = float(last_bq.ask_price)
            entry_spread = entry_ask - entry_bid
            entry_spread_bp = entry_spread / entry_ask * 10_000
        else:
            entry_bid = t["entry"] - 0.01
            entry_ask = t["entry"]
            entry_spread = 0.01
            entry_spread_bp = entry_spread / t["entry"] * 10_000
        
        # Calculate exit spread
        valid_s_qs = [q for q in q_exit if q.bid_price and q.ask_price and q.ask_price > q.bid_price]
        if valid_s_qs:
            last_sq = valid_s_qs[-1]
            exit_bid = float(last_sq.bid_price)
            exit_ask = float(last_sq.ask_price)
            exit_spread = exit_ask - exit_bid
            exit_spread_bp = exit_spread / exit_ask * 10_000
            exit_mid = (exit_bid + exit_ask) / 2.0
        else:
            exit_bid = t["exit"]
            exit_ask = t["exit"] + 0.01
            exit_spread = 0.01
            exit_spread_bp = exit_spread / t["exit"] * 10_000
            exit_mid = t["exit"] + 0.005
        
        # Calculate VWAP
        vwap = None
        if bars:
            cpv = sum(((float(b.high) + float(b.low) + float(b.close)) / 3.0) * float(b.volume) for b in bars)
            cvol = sum(float(b.volume) for b in bars)
            vwap = cpv / cvol if cvol > 0 else float(bars[-1].close)
        
        # Realized live trade metrics
        actual_entry = float(t["entry"])
        actual_exit = float(t["exit"])
        live_pnl_pct = float(t["pnl_pct"])  # in %
        live_pnl_bp = live_pnl_pct * 100.0   # in bp
        
        # Counterfactual 1.A: Spread Compression Check
        # Compressed if spread <= $0.01 and spread_bp <= 3.0 bp
        is_compressed = (entry_spread <= 0.0101) and (entry_spread_bp <= 3.0)
        
        # Counterfactual 1.B: Passive Maker Exit
        # Midpoint exit price vs actual fill
        cf_maker_mid_pnl_bp = (exit_mid - actual_entry) / actual_entry * 10_000
        # Ask touch exit price (limit at ask)
        cf_maker_ask_pnl_bp = (exit_ask - actual_entry) / actual_entry * 10_000
        
        # Counterfactual 1.C: Anti-Pullback Gate
        # If entry price is below VWAP or pulled back > 0.5% below high of day
        hod = max(float(b.high) for b in bars) if bars else actual_entry
        pullback_from_hod_pct = (hod - actual_entry) / hod if hod > 0 else 0.0
        is_fading_pullback = (vwap is not None and actual_entry < vwap) or (pullback_from_hod_pct >= 0.008)
        
        records.append({
            "symbol": sym,
            "buy_time": b_time_str,
            "sell_time": s_time_str,
            "entry_px": actual_entry,
            "exit_px": actual_exit,
            "live_pnl_bp": round(live_pnl_bp, 2),
            "entry_spread_cents": round(entry_spread, 2),
            "entry_spread_bp": round(entry_spread_bp, 2),
            "exit_spread_cents": round(exit_spread, 2),
            "exit_spread_bp": round(exit_spread_bp, 2),
            "is_compressed": is_compressed,
            "cf_maker_mid_bp": round(cf_maker_mid_pnl_bp, 2),
            "cf_maker_ask_bp": round(cf_maker_ask_pnl_bp, 2),
            "vwap": round(vwap, 2) if vwap else None,
            "hod": round(hod, 2),
            "is_fading_pullback": is_fading_pullback,
        })
        
        print(f"[{idx+1}/{len(day_closed)}] {sym:5s} | Live: {live_pnl_bp:+5.1f} bp | Spread: {entry_spread:0.2f}c ({entry_spread_bp:4.1f} bp) | Comp: {is_compressed} | MidExit: {cf_maker_mid_pnl_bp:+5.1f} bp | Fade: {is_fading_pullback}")

    # Summary Statistics
    live_nets = [r["live_pnl_bp"] for r in records]
    
    # 1.A: Kept vs Blocked by Spread Compression Gate
    kept_comp = [r for r in records if r["is_compressed"]]
    blocked_comp = [r for r in records if not r["is_compressed"]]
    
    # 1.B: Maker Exits
    mid_nets = [r["cf_maker_mid_bp"] for r in records]
    ask_nets = [r["cf_maker_ask_bp"] for r in records]
    
    # 1.C: Anti-Pullback Filter
    kept_pullback = [r for r in records if not r["is_fading_pullback"]]
    blocked_pullback = [r for r in records if r["is_fading_pullback"]]
    
    # Combined (1.A + 1.B + 1.C): Compressed entries only, Maker midpoint exit, No fading pullbacks
    combined = [r for r in records if r["is_compressed"] and not r["is_fading_pullback"]]
    combined_nets = [r["cf_maker_mid_bp"] for r in combined]
    
    print("\n" + "=" * 70)
    print("FRIDAY 2026-10-30 COUNTERFACTUAL RESULTS SUMMARY")
    print("=" * 70)
    print(f"Actual Live Session Performance (33 trades):")
    print(f"  Mean Net P&L:      {np.mean(live_nets):+6.2f} bp/trade")
    print(f"  Total Session P&L: {np.sum(live_nets):+6.2f} bp")
    print(f"  Win Rate:          {sum(1 for x in live_nets if x > 0)/len(live_nets)*100:5.1f}%")
    print("-" * 70)
    print("Counterfactual 1.A (Spread Compression Gate - Block spread > $0.01 / > 3 bp):")
    print(f"  Trades Kept:       {len(kept_comp)} / {len(records)}")
    if kept_comp:
        k_nets = [r["live_pnl_bp"] for r in kept_comp]
        print(f"  Kept Net P&L:      {np.mean(k_nets):+6.2f} bp/trade (Total: {np.sum(k_nets):+6.2f} bp)")
    if blocked_comp:
        b_nets = [r["live_pnl_bp"] for r in blocked_comp]
        print(f"  Blocked Net P&L:   {np.mean(b_nets):+6.2f} bp/trade (Total: {np.sum(b_nets):+6.2f} bp)")
    print("-" * 70)
    print("Counterfactual 1.B (Passive Maker Exits across all 33 trades):")
    print(f"  Midpoint Limit:    {np.mean(mid_nets):+6.2f} bp/trade (Total: {np.sum(mid_nets):+6.2f} bp, Win: {sum(1 for x in mid_nets if x > 0)/len(mid_nets)*100:5.1f}%)")
    print(f"  Ask Touch Limit:   {np.mean(ask_nets):+6.2f} bp/trade (Total: {np.sum(ask_nets):+6.2f} bp, Win: {sum(1 for x in ask_nets if x > 0)/len(ask_nets)*100:5.1f}%)")
    print("-" * 70)
    print("Counterfactual 1.C (Anti-Pullback Gate - Block entries below VWAP / fading HOD):")
    print(f"  Trades Kept:       {len(kept_pullback)} / {len(records)}")
    if kept_pullback:
        kp_nets = [r["live_pnl_bp"] for r in kept_pullback]
        print(f"  Kept Net P&L:      {np.mean(kp_nets):+6.2f} bp/trade (Total: {np.sum(kp_nets):+6.2f} bp)")
    if blocked_pullback:
        bp_nets = [r["live_pnl_bp"] for r in blocked_pullback]
        print(f"  Blocked Net P&L:   {np.mean(bp_nets):+6.2f} bp/trade (Total: {np.sum(bp_nets):+6.2f} bp)")
    print("-" * 70)
    print("COMBINED STACK (1.A Spread Compression + 1.B Midpoint Exit + 1.C Anti-Pullback):")
    print(f"  Qualified Trades:  {len(combined)} / {len(records)}")
    if combined:
        print(f"  Combined Net P&L:  {np.mean(combined_nets):+6.2f} bp/trade")
        print(f"  Combined Total:    {np.sum(combined_nets):+6.2f} bp")
        print(f"  Combined Win Rate: {sum(1 for x in combined_nets if x > 0)/len(combined)*100:5.1f}%")
    print("=" * 70 + "\n")
    
    out_path = os.path.join(ROOT, "ai_reports", "friday_counterfactual_2026-10-30.json")
    with open(out_path, "w") as f:
        json.dump(records, f, indent=2)
    print(f"Saved detailed trade counterfactual records to {out_path}.")


if __name__ == "__main__":
    run_counterfactual()
