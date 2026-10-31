#!/usr/bin/env python3
"""Week Counterfactual: Anti-Pullback Gate on 2026-10-26 .. 2026-10-30.
Evaluates the Anti-Pullback Gate across all 134 live round-trip trades executed last week:
- 2026-10-26 (Monday)
- 2026-10-27 (Tuesday)
- 2026-10-28 (Wednesday)
- 2026-10-29 (Thursday)
- 2026-10-30 (Friday)

Anti-Pullback Rule:
Block entries where price is below cash VWAP or pulled back >= 0.8% from high of day.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict
import numpy as np

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import fill_truth_report as ft
from desk_core import load_desk_env
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame

ET = ZoneInfo("America/New_York")
DATES = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]


def get_client():
    p = os.path.join(ROOT, "secrets_data.json")
    if os.path.exists(p):
        s = json.load(open(p))
        return StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])
    cfg = json.load(open(os.path.join(ROOT, "config", "bot_config.json")))
    return StockHistoricalDataClient(cfg["alpaca_api_key"], cfg["alpaca_secret_key"])


def run_week_counterfactual():
    load_desk_env()
    cl = get_client()
    
    print("Loading all round trips from 2026-10-26 through 2026-10-30...")
    fills = ft.fetch_alpaca_fills(days=10, limit=1000)
    ai_syms = ft._ai_symbols()
    eng_syms = ft._engine_symbols()
    fills = ft.tag_fills(fills, ai_syms, eng_syms)
    closed = ft._pair_round_trips(fills)
    
    all_trades = []
    for d_str in DATES:
        d_obj = date.fromisoformat(d_str)
        day_c = ft.closed_on_day(closed, d_obj)
        for t in day_c:
            t["day"] = d_str
            all_trades.append(t)
            
    print(f"Total trades across 5 sessions: {len(all_trades)}\n")
    
    records = []
    
    for idx, t in enumerate(all_trades):
        sym = t["symbol"]
        day_str = t["day"]
        b_time_str = t["buy_time"]
        
        # Robust datetime handling
        b_dt = datetime.fromisoformat(b_time_str.replace("Z", "+00:00"))
        if b_dt.tzinfo is None:
            b_dt = b_dt.replace(tzinfo=timezone.utc)
        b_dt_et = b_dt.astimezone(ET)
        
        # Fetch 1m bars from 09:30 (or 04:00 if premarket) up to entry
        d_day = b_dt_et.date()
        t0 = datetime(d_day.year, d_day.month, d_day.day, 9, 30, tzinfo=ET)
        if b_dt_et < t0:
            t0 = datetime(d_day.year, d_day.month, d_day.day, 4, 0, tzinfo=ET)
            if b_dt_et < t0:
                t0 = b_dt_et - timedelta(minutes=5)
        
        end_dt = max(b_dt_et + timedelta(minutes=1), t0 + timedelta(minutes=1))
        
        try:
            bars = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=sym,
                timeframe=TimeFrame.Minute,
                start=t0,
                end=end_dt,
                feed=DataFeed.SIP,
            )).data.get(sym) or []
        except Exception as e:
            print(f"Error fetching bars for {sym} {b_time_str}: {e}")
            bars = []
        
        entry_px = float(t["entry"])
        exit_px = float(t["exit"])
        pnl_pct = float(t["pnl_pct"])
        pnl_bp = pnl_pct * 100.0
        
        vwap = None
        hod = entry_px
        if bars:
            cpv = sum(((float(b.high) + float(b.low) + float(b.close)) / 3.0) * float(b.volume) for b in bars)
            cvol = sum(float(b.volume) for b in bars)
            vwap = cpv / cvol if cvol > 0 else float(bars[-1].close)
            hod = max(float(b.high) for b in bars)
        
        # Anti-Pullback Gate Condition:
        # Fading pullback if entry is below VWAP OR pulled back >= 0.8% below HOD
        pullback_from_hod_pct = (hod - entry_px) / hod if hod > 0 else 0.0
        is_below_vwap = (vwap is not None and entry_px < vwap)
        is_fading_pullback = is_below_vwap or (pullback_from_hod_pct >= 0.008)
        
        records.append({
            "day": day_str,
            "symbol": sym,
            "buy_time": b_time_str,
            "sell_time": t["sell_time"],
            "entry_px": entry_px,
            "exit_px": exit_px,
            "pnl_bp": round(pnl_bp, 2),
            "vwap": round(vwap, 2) if vwap else None,
            "hod": round(hod, 2),
            "pullback_from_hod_pct": round(pullback_from_hod_pct * 100, 2),
            "is_below_vwap": is_below_vwap,
            "is_fading_pullback": is_fading_pullback,
        })
        
        status_tag = "BLOCK (FADE)" if is_fading_pullback else "KEEP (TREND)"
        reason = []
        if is_below_vwap:
            reason.append(f"<VWAP({vwap:.2f})")
        if pullback_from_hod_pct >= 0.008:
            reason.append(f"-{pullback_from_hod_pct*100:.1f}% HOD")
        reason_str = ", ".join(reason) if reason else "TRENDING"
        
        print(f"[{idx+1:3d}/{len(all_trades)}] {day_str} | {sym:5s} | PnL: {pnl_bp:+6.1f} bp | px:{entry_px:.2f} | {status_tag:12s} | {reason_str}")

    # Output detailed report
    print("\n" + "=" * 78)
    print("ANTI-PULLBACK GATE: FULL-WEEK COUNTERFACTUAL AUDIT (2026-10-26 .. 2026-10-30)")
    print("=" * 78)
    
    by_day = defaultdict(list)
    for r in records:
        by_day[r["day"]].append(r)
        
    day_stats = []
    
    for d_str in DATES:
        day_recs = by_day[d_str]
        all_nets = [r["pnl_bp"] for r in day_recs]
        
        kept = [r for r in day_recs if not r["is_fading_pullback"]]
        blocked = [r for r in day_recs if r["is_fading_pullback"]]
        
        kept_nets = [r["pnl_bp"] for r in kept]
        blocked_nets = [r["pnl_bp"] for r in blocked]
        
        all_tot = sum(all_nets)
        kept_tot = sum(kept_nets) if kept else 0.0
        blocked_tot = sum(blocked_nets) if blocked else 0.0
        delta = kept_tot - all_tot
        
        win_all = sum(1 for x in all_nets if x > 0) / len(all_nets) * 100 if all_nets else 0
        win_kept = sum(1 for x in kept_nets if x > 0) / len(kept_nets) * 100 if kept_nets else 0
        
        blocked_losers = [r for r in blocked if r["pnl_bp"] <= 0]
        blocked_winners = [r for r in blocked if r["pnl_bp"] > 0]
        
        day_stats.append({
            "day": d_str,
            "n_all": len(day_recs),
            "n_kept": len(kept),
            "n_blocked": len(blocked),
            "all_tot": all_tot,
            "kept_tot": kept_tot,
            "blocked_tot": blocked_tot,
            "delta": delta,
            "win_all": win_all,
            "win_kept": win_kept,
            "kept_mean": float(np.mean(kept_nets)) if kept else 0.0,
            "blocked_mean": float(np.mean(blocked_nets)) if blocked else 0.0,
            "blocked_losers_count": len(blocked_losers),
            "blocked_losers_tot": sum(r["pnl_bp"] for r in blocked_losers),
            "blocked_winners_count": len(blocked_winners),
            "blocked_winners_tot": sum(r["pnl_bp"] for r in blocked_winners),
        })
        
        print(f"\nSession {d_str}:")
        print(f"  Live Baseline:  {len(day_recs):2d} trades | Win: {win_all:4.1f}% | Total: {all_tot:+7.2f} bp (Mean: {np.mean(all_nets):+5.2f} bp)")
        print(f"  Kept Trades:    {len(kept):2d} trades | Win: {win_kept:4.1f}% | Total: {kept_tot:+7.2f} bp (Mean: {day_stats[-1]['kept_mean']:+5.2f} bp)")
        print(f"  Blocked Trades: {len(blocked):2d} trades | Total: {blocked_tot:+7.2f} bp (Mean: {day_stats[-1]['blocked_mean']:+5.2f} bp)")
        print(f"    - Blocked Losers (Avoided): {len(blocked_losers):2d} trades, {sum(r['pnl_bp'] for r in blocked_losers):+7.2f} bp")
        print(f"    - Blocked Winners (Missed): {len(blocked_winners):2d} trades, {sum(r['pnl_bp'] for r in blocked_winners):+7.2f} bp")
        print(f"  Session Delta:  {delta:+7.2f} bp {'PROFIT IMPROVEMENT' if delta > 0 else 'DRAG'}")

    # Full Week Totals
    week_all = sum(s["all_tot"] for s in day_stats)
    week_kept = sum(s["kept_tot"] for s in day_stats)
    week_blocked = sum(s["blocked_tot"] for s in day_stats)
    week_delta = week_kept - week_all
    
    total_trades = sum(s["n_all"] for s in day_stats)
    total_kept = sum(s["n_kept"] for s in day_stats)
    total_blocked = sum(s["n_blocked"] for s in day_stats)
    
    all_wins = sum(1 for r in records if r["pnl_bp"] > 0)
    kept_wins = sum(1 for r in records if not r["is_fading_pullback"] and r["pnl_bp"] > 0)
    
    all_blocked_losers = [r for r in records if r["is_fading_pullback"] and r["pnl_bp"] <= 0]
    all_blocked_winners = [r for r in records if r["is_fading_pullback"] and r["pnl_bp"] > 0]
    
    print("\n" + "=" * 78)
    print("FULL-WEEK SUMMARY TOTALS (5 TRADING SESSIONS: 2026-10-26 .. 2026-10-30)")
    print("=" * 78)
    print(f"Live Baseline (All Trades):   {total_trades:3d} trades | Win Rate: {all_wins/total_trades*100:4.1f}% | Total P&L: {week_all:+7.2f} bp (Mean: {week_all/total_trades:+5.2f} bp)")
    print(f"Kept Trades (Anti-Pullback):  {total_kept:3d} trades | Win Rate: {kept_wins/total_kept*100:4.1f}% | Total P&L: {week_kept:+7.2f} bp (Mean: {week_kept/total_kept:+5.2f} bp)")
    print(f"Blocked Trades (Fading Trap): {total_blocked:3d} trades | Total P&L: {week_blocked:+7.2f} bp (Mean: {week_blocked/total_blocked:+5.2f} bp)")
    print(f"  - Toxic Losses Avoided:     {len(all_blocked_losers):3d} trades | Sum: {sum(r['pnl_bp'] for r in all_blocked_losers):+7.2f} bp")
    print(f"  - False Positives (Missed): {len(all_blocked_winners):3d} trades | Sum: {sum(r['pnl_bp'] for r in all_blocked_winners):+7.2f} bp")
    print("-" * 78)
    print(f"CUMULATIVE WEEKLY BENEFIT:    {week_delta:+7.2f} bp {'GAIN' if week_delta > 0 else 'LOSS'}")
    print("=" * 78 + "\n")
    
    # Print top 5 worst avoided trades
    sorted_losers = sorted(all_blocked_losers, key=lambda x: x["pnl_bp"])
    print("TOP 5 TOXIC TRADES AVOIDED BY ANTI-PULLBACK GATE:")
    for i, r in enumerate(sorted_losers[:5]):
        print(f"  {i+1}. {r['day']} {r['symbol']:5s}: {r['pnl_bp']:+6.1f} bp (VWAP: {r['vwap']}, HOD: {r['hod']}, Entry: {r['entry_px']})")
    
    # Print top 5 missed winners
    sorted_winners = sorted(all_blocked_winners, key=lambda x: x["pnl_bp"], reverse=True)
    if sorted_winners:
        print("\nTOP MISSED WINNERS (FALSE POSITIVES):")
        for i, r in enumerate(sorted_winners[:5]):
            print(f"  {i+1}. {r['day']} {r['symbol']:5s}: {r['pnl_bp']:+6.1f} bp (VWAP: {r['vwap']}, HOD: {r['hod']}, Entry: {r['entry_px']})")
    
    out_path = os.path.join(ROOT, "ai_reports", "week_counterfactual_2026-10-26_10-30.json")
    with open(out_path, "w") as f:
        json.dump({"summary": {"week_all": week_all, "week_kept": week_kept, "week_blocked": week_blocked, "week_delta": week_delta}, "day_stats": day_stats, "records": records}, f, indent=2)
    print(f"\nSaved full-week counterfactual records to {out_path}.")


if __name__ == "__main__":
    run_week_counterfactual()
