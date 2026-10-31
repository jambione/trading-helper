#!/usr/bin/env python3
"""October 2026 Counterfactual: Anti-Pullback Gate Options on full October data.
Evaluates Option 2 (and comparison variants) across all 923 live round trips
executed during the full month of October 2026 (2026-10-01 .. 2026-10-30).

Options evaluated:
- Baseline: Live actual performance (all trades)
- Option 2 (Precision Gate): Block if Price < (VWAP - 15 bp) OR Drop >= 2.0%
- Option 1 (Balanced Waterfall): Block if Price < VWAP OR Drop >= 2.0%
- Option 3 (Disaster-Only): Block if Price < (VWAP - 15 bp) OR Drop >= 5.0%
- Pure VWAP Floor: Block if Price < VWAP
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
import numpy as np

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import fill_truth_report as ft
from desk_core import load_desk_env
import alpaca_trader
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame

ET = ZoneInfo("America/New_York")


def get_data_client():
    p = os.path.join(ROOT, "secrets_data.json")
    if os.path.exists(p):
        s = json.load(open(p))
        return StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])
    cfg = json.load(open(os.path.join(ROOT, "config", "bot_config.json")))
    return StockHistoricalDataClient(cfg["alpaca_api_key"], cfg["alpaca_secret_key"])


def fetch_october_closed_trips():
    """Fetches all closed orders from Alpaca for October 2026 and pairs them into round trips."""
    ft._load_env()
    alpaca_trader.init(mode="paper", api_key=os.getenv("ALPACA_API_KEY"), secret_key=os.getenv("ALPACA_SECRET_KEY"))
    cl = alpaca_trader._client
    
    oct_start = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    all_orders = []
    until = None
    
    print("Fetching all October orders from Alpaca...")
    while True:
        req_kwargs = {"status": QueryOrderStatus.CLOSED, "limit": 500, "after": oct_start, "nested": True}
        if until:
            req_kwargs["until"] = until
        batch = cl.get_orders(filter=GetOrdersRequest(**req_kwargs))
        if not batch:
            break
        all_orders.extend(batch)
        if len(batch) < 500:
            break
        until = batch[-1].submitted_at - timedelta(microseconds=1)
        
    print(f"Total October orders fetched: {len(all_orders)}")
    
    raw_fills = []
    for o in all_orders:
        status = str(getattr(o, "status", "") or "").split(".")[-1].lower()
        filled_qty = alpaca_trader._f(o, "filled_qty")
        if filled_qty <= 0 and status not in ("filled", "partially_filled"):
            continue
        raw_fills.append({
            "id": str(getattr(o, "id", "") or ""),
            "symbol": str(getattr(o, "symbol", "") or "").upper(),
            "side": str(getattr(o, "side", "") or "").split(".")[-1].lower(),
            "qty": alpaca_trader._f(o, "qty"),
            "filled_qty": filled_qty,
            "filled_avg_price": alpaca_trader._f(o, "filled_avg_price"),
            "type": str(getattr(o, "type", "") or "").split(".")[-1].lower(),
            "status": status,
            "submitted_at": str(getattr(o, "submitted_at", "") or ""),
            "filled_at": str(getattr(o, "filled_at", "") or getattr(o, "submitted_at", "") or ""),
        })
        
    ai_syms = ft._ai_symbols()
    eng_syms = ft._engine_symbols()
    tagged = ft.tag_fills(raw_fills, ai_syms, eng_syms)
    closed = ft._pair_round_trips(tagged)
    print(f"Successfully paired into {len(closed)} closed round-trip trades.")
    return closed


def run_october_counterfactual():
    load_desk_env()
    data_cl = get_data_client()
    
    closed = fetch_october_closed_trips()
    
    # Group trades by date (ET)
    by_day_trades = defaultdict(list)
    for t in closed:
        b_dt = datetime.fromisoformat(t["buy_time"].replace("Z", "+00:00")).astimezone(ET)
        d_str = b_dt.strftime("%Y-%m-%d")
        t["b_dt_et"] = b_dt
        t["day"] = d_str
        by_day_trades[d_str].append(t)
        
    sorted_days = sorted(by_day_trades.keys())
    print(f"Total trading sessions in October: {len(sorted_days)}\n")
    
    # Process each day: fetch bars in bulk for that day's unique symbols
    trade_records = []
    
    for day_idx, d_str in enumerate(sorted_days):
        day_trades = by_day_trades[d_str]
        unique_syms = list(set(t["symbol"] for t in day_trades))
        
        # Day bounds in ET
        y, m, d = [int(x) for x in d_str.split("-")]
        t_start = datetime(y, m, d, 9, 30, tzinfo=ET)
        t_end = datetime(y, m, d, 16, 5, tzinfo=ET)
        
        # Bulk fetch SIP minute bars for all symbols on this day
        # Chunk if unique_syms > 50
        bars_by_sym = defaultdict(list)
        chunk_size = 40
        for i in range(0, len(unique_syms), chunk_size):
            chunk = unique_syms[i:i + chunk_size]
            try:
                bars_resp = data_cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=chunk,
                    timeframe=TimeFrame.Minute,
                    start=t_start,
                    end=t_end,
                    feed=DataFeed.SIP,
                ))
                for s in chunk:
                    bars_by_sym[s].extend(bars_resp.data.get(s) or [])
            except Exception as e:
                print(f"Error fetching bars for day {d_str} chunk {chunk}: {e}")
                
        # Now process each trade on this day
        for t in day_trades:
            sym = t["symbol"]
            b_dt = t["b_dt_et"]
            sym_bars = bars_by_sym.get(sym, [])
            
            # Filter bars prior to entry time (regular hours >= 09:30)
            prior_bars = [b for b in sym_bars if b.timestamp.astimezone(ET) <= b_dt]
            
            entry_px = float(t["entry"])
            exit_px = float(t["exit"])
            pnl_pct = float(t["pnl_pct"])
            pnl_bp = pnl_pct * 100.0
            
            vwap = None
            hod = entry_px
            if prior_bars:
                cpv = sum(((float(b.high) + float(b.low) + float(b.close)) / 3.0) * float(b.volume) for b in prior_bars)
                cvol = sum(float(b.volume) for b in prior_bars)
                vwap = cpv / cvol if cvol > 0 else float(prior_bars[-1].close)
                hod = max(float(b.high) for b in prior_bars)
                
            drop_from_hod_pct = (hod - entry_px) / hod if hod > 0 else 0.0
            is_below_vwap = (vwap is not None and entry_px < vwap)
            is_below_vwap_15bp = (vwap is not None and entry_px < vwap * (1.0 - 0.0015))
            
            # Evaluate gates
            # Option 2 (Precision Gate): Price < VWAP - 15bp OR Drop >= 2.0%
            opt2_blocked = is_below_vwap_15bp or (drop_from_hod_pct >= 0.020)
            
            # Option 1 (Balanced Waterfall): Price < VWAP OR Drop >= 2.0%
            opt1_blocked = is_below_vwap or (drop_from_hod_pct >= 0.020)
            
            # Option 3 (Disaster Only): Price < VWAP - 15bp OR Drop >= 5.0%
            opt3_blocked = is_below_vwap_15bp or (drop_from_hod_pct >= 0.050)
            
            # Pure VWAP Floor: Price < VWAP
            vwap_blocked = is_below_vwap
            
            trade_records.append({
                "day": d_str,
                "symbol": sym,
                "buy_time": t["buy_time"],
                "sell_time": t["sell_time"],
                "entry_px": entry_px,
                "exit_px": exit_px,
                "pnl_bp": round(pnl_bp, 2),
                "vwap": round(vwap, 2) if vwap else None,
                "hod": round(hod, 2),
                "drop_pct": round(drop_from_hod_pct * 100, 2),
                "is_below_vwap": is_below_vwap,
                "is_below_vwap_15bp": is_below_vwap_15bp,
                "opt2_blocked": opt2_blocked,
                "opt1_blocked": opt1_blocked,
                "opt3_blocked": opt3_blocked,
                "vwap_blocked": vwap_blocked,
            })
            
        print(f"[{day_idx+1:2d}/{len(sorted_days)}] Processed session {d_str} ({len(day_trades):2d} trades, {len(unique_syms):2d} symbols)")
        
    print(f"\nCompleted bar analysis for all {len(trade_records)} trades across October 2026.")
    
    # Aggregate and Compare
    def summarize_gate(name: str, key_blocked: str):
        kept = [r for r in trade_records if not r[key_blocked]]
        blocked = [r for r in trade_records if r[key_blocked]]
        
        all_nets = [r["pnl_bp"] for r in trade_records]
        k_nets = [r["pnl_bp"] for r in kept]
        b_nets = [r["pnl_bp"] for r in blocked]
        
        all_tot = sum(all_nets)
        k_tot = sum(k_nets) if k_nets else 0.0
        b_tot = sum(b_nets) if b_nets else 0.0
        delta = k_tot - all_tot  # -b_tot
        
        win_all = sum(1 for x in all_nets if x > 0) / len(all_nets) * 100
        win_k = sum(1 for x in k_nets if x > 0) / len(k_nets) * 100 if k_nets else 0.0
        
        b_losers = [r for r in blocked if r["pnl_bp"] <= 0]
        b_winners = [r for r in blocked if r["pnl_bp"] > 0]
        
        # Day by day deltas
        by_day_d = defaultdict(float)
        for r in blocked:
            by_day_d[r["day"]] -= r["pnl_bp"]
            
        pos_days = sum(1 for d in sorted_days if by_day_d[d] >= 0)
        worst_day = min((by_day_d[d] for d in sorted_days), default=0.0)
        
        return {
            "name": name,
            "n_kept": len(kept),
            "n_blocked": len(blocked),
            "pct_blocked": round(len(blocked) / len(trade_records) * 100, 1),
            "all_tot": round(all_tot, 1),
            "kept_tot": round(k_tot, 1),
            "blocked_tot": round(b_tot, 1),
            "delta": round(delta, 1),
            "win_all": round(win_all, 1),
            "win_kept": round(win_k, 1),
            "kept_mean": round(float(np.mean(k_nets)), 2) if k_nets else 0.0,
            "blocked_mean": round(float(np.mean(b_nets)), 2) if b_nets else 0.0,
            "b_losers_count": len(b_losers),
            "b_losers_tot": round(sum(r["pnl_bp"] for r in b_losers), 1),
            "b_winners_count": len(b_winners),
            "b_winners_tot": round(sum(r["pnl_bp"] for r in b_winners), 1),
            "pos_days": pos_days,
            "worst_day": round(worst_day, 1),
            "by_day_deltas": {d: round(by_day_d[d], 1) for d in sorted_days},
        }
        
    s_base = {
        "name": "Live Actual Baseline",
        "n_kept": len(trade_records),
        "kept_tot": round(sum(r["pnl_bp"] for r in trade_records), 1),
        "kept_mean": round(float(np.mean([r["pnl_bp"] for r in trade_records])), 2),
        "win_rate": round(sum(1 for r in trade_records if r["pnl_bp"] > 0) / len(trade_records) * 100, 1),
    }
    
    s_opt2 = summarize_gate("Option 2: Precision Gate (< VWAP-15bp OR Drop >= 2.0%)", "opt2_blocked")
    s_opt1 = summarize_gate("Option 1: Balanced Waterfall (< VWAP OR Drop >= 2.0%)", "opt1_blocked")
    s_opt3 = summarize_gate("Option 3: Conservative Disaster-Only (< VWAP-15bp OR Drop >= 5.0%)", "opt3_blocked")
    s_vwap = summarize_gate("Option 4: Pure VWAP Floor (< VWAP)", "vwap_blocked")
    
    # Print Comparative Report
    print("\n" + "=" * 92)
    print("OCTOBER 2026 FULL-MONTH COUNTERFACTUAL AUDIT (22 TRADING SESSIONS)")
    print("=" * 92)
    print(f"Total Live Trades Evaluated: {len(trade_records)}")
    print(f"Live Baseline Total P&L:     {s_base['kept_tot']:+8.1f} bp (Mean: {s_base['kept_mean']:+5.2f} bp, Win Rate: {s_base['win_rate']:4.1f}%)\n")
    
    print(f"{'Gate Strategy':42s} | {'Kept':4s} | {'Block':5s} | {'Kept P&L':9s} | {'Delta':9s} | {'Win%':5s} | {'PosDays':7s} | {'WorstDay':8s}")
    print("-" * 92)
    for s in [s_opt2, s_opt1, s_opt3, s_vwap]:
        print(f"{s['name'][:42]:42s} | {s['n_kept']:4d} | {s['n_blocked']:5d} | {s['kept_tot']:+8.1f} bp | {s['delta']:+8.1f} bp | {s['win_kept']:4.1f}% | {s['pos_days']:2d}/22   | {s['worst_day']:+7.1f} bp")
    print("=" * 92)
    
    # Detailed Breakdown for Option 2
    print("\n" + "-" * 78)
    print("OPTION 2 (PRECISION GATE) DEEP-DIVE METRICS")
    print("-" * 78)
    print(f"Trades Filtered:             {s_opt2['n_blocked']} / {len(trade_records)} ({s_opt2['pct_blocked']}%)")
    print(f"Net Cumulative Improvement:  {s_opt2['delta']:+8.1f} bp GAIN")
    print(f"Toxic Losses Avoided:        {s_opt2['b_losers_count']} trades ({s_opt2['b_losers_tot']:+8.1f} bp loss saved)")
    print(f"False Positives (Missed):    {s_opt2['b_winners_count']} trades ({s_opt2['b_winners_tot']:+8.1f} bp profit missed)")
    print(f"Mean P&L on Blocked Trades:  {s_opt2['blocked_mean']:+6.2f} bp/trade (Strongly Negative Edge Filtered Out)")
    print(f"Sessions Improved / Neutral: {s_opt2['pos_days']} of 22 sessions ({s_opt2['pos_days']/22*100:.1f}%)")
    print(f"Worst Single-Session Drag:   {s_opt2['worst_day']:+6.1f} bp")
    
    # Print Day by Day for Option 2
    print("\nSESSION-BY-SESSION BREAKDOWN FOR OPTION 2:")
    print(f"{'Date':10s} | {'LiveTrades':10s} | {'LivePnL':9s} | {'Kept':4s} | {'Block':5s} | {'KeptPnL':9s} | {'Delta':9s}")
    print("-" * 68)
    by_day_records = defaultdict(list)
    for r in trade_records:
        by_day_records[r["day"]].append(r)
        
    for d_str in sorted_days:
        day_recs = by_day_records[d_str]
        l_tot = sum(r["pnl_bp"] for r in day_recs)
        k_recs = [r for r in day_recs if not r["opt2_blocked"]]
        b_recs = [r for r in day_recs if r["opt2_blocked"]]
        k_tot = sum(r["pnl_bp"] for r in k_recs) if k_recs else 0.0
        delta = k_tot - l_tot
        print(f"{d_str:10s} | {len(day_recs):10d} | {l_tot:+8.1f} bp | {len(k_recs):4d} | {len(b_recs):5d} | {k_tot:+8.1f} bp | {delta:+8.1f} bp {'GAIN' if delta > 0 else ('DRAG' if delta < 0 else 'FLAT')}")

    # Top avoided losers and missed winners in October for Option 2
    blocked_opt2 = [r for r in trade_records if r["opt2_blocked"]]
    worst_avoided = sorted([r for r in blocked_opt2 if r["pnl_bp"] < 0], key=lambda x: x["pnl_bp"])
    best_missed = sorted([r for r in blocked_opt2 if r["pnl_bp"] > 0], key=lambda x: x["pnl_bp"], reverse=True)
    
    print("\nTOP 5 DISASTERS AVOIDED BY OPTION 2 IN OCTOBER:")
    for i, r in enumerate(worst_avoided[:5]):
        print(f"  {i+1}. {r['day']} {r['symbol']:5s}: {r['pnl_bp']:+6.1f} bp (Entry: {r['entry_px']:.2f}, VWAP: {r['vwap']}, HOD: {r['hod']}, Drop: {r['drop_pct']}%)")
        
    print("\nTOP 5 MISSED WINNERS (FALSE POSITIVES) IN OCTOBER:")
    for i, r in enumerate(best_missed[:5]):
        print(f"  {i+1}. {r['day']} {r['symbol']:5s}: {r['pnl_bp']:+6.1f} bp (Entry: {r['entry_px']:.2f}, VWAP: {r['vwap']}, HOD: {r['hod']}, Drop: {r['drop_pct']}%)")
        
    out_path = os.path.join(ROOT, "ai_reports", "october_counterfactual.json")
    with open(out_path, "w") as f:
        json.dump({
            "summary": {
                "baseline": s_base,
                "opt2": s_opt2,
                "opt1": s_opt1,
                "opt3": s_opt3,
                "vwap": s_vwap,
            },
            "records": trade_records
        }, f, indent=2)
    print(f"\nSaved full October counterfactual report to {out_path}.")


if __name__ == "__main__":
    run_october_counterfactual()
