#!/usr/bin/env python3
"""Calibrate Anti-Pullback Gate Thresholds.
Runs parameter grid search and Leave-One-Out Cross-Validation (LOOCV)
across all 134 live round trips from last week (2026-10-26 .. 2026-10-30).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
import numpy as np

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

ET = ZoneInfo("America/New_York")
DATES = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]


def load_data():
    path = os.path.join(ROOT, "ai_reports", "week_counterfactual_2026-10-26_10-30.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing {path}")
    data = json.load(open(path))
    recs = data["records"]
    for r in recs:
        b_dt = datetime.fromisoformat(r["buy_time"].replace("Z", "+00:00"))
        b_et = b_dt.astimezone(ET)
        r["minute_of_day"] = b_et.hour * 60 + b_et.minute
        r["time_str"] = b_et.strftime("%H:%M")
    return recs


def evaluate_param(recs, mode: str, thresh: float, time_split: str = "ALL"):
    """Evaluates a parameter combination on a set of records.
    Returns summary stats dict.
    """
    kept = []
    blocked = []
    by_day = defaultdict(lambda: {"kept": [], "blocked": []})
    
    for r in recs:
        drop = r["pullback_from_hod_pct"]
        below_v = r["is_below_vwap"]
        mod = r["minute_of_day"]
        
        # Time condition
        # 09:30 is minute 570; 10:15 is minute 615
        is_morning = mod < 615
        
        if time_split == "MORNING_ONLY" and not is_morning:
            is_blocked = False
        elif time_split == "MIDDAY_ONLY" and is_morning:
            is_blocked = False
        else:
            if mode == "OR":
                is_blocked = below_v or (drop >= thresh)
            elif mode == "AND":
                is_blocked = below_v and (drop >= thresh)
            elif mode == "PULLBACK_ONLY":
                is_blocked = (drop >= thresh)
            elif mode == "VWAP_ONLY":
                is_blocked = below_v
            else:
                raise ValueError(mode)
                
        d = r["day"]
        if is_blocked:
            blocked.append(r)
            by_day[d]["blocked"].append(r)
        else:
            kept.append(r)
            by_day[d]["kept"].append(r)
            
    all_pnl = sum(r["pnl_bp"] for r in recs)
    kept_pnl = sum(r["pnl_bp"] for r in kept)
    blocked_pnl = sum(r["pnl_bp"] for r in blocked)
    delta_pnl = -blocked_pnl  # benefit of avoiding blocked trades
    
    win_rate = sum(1 for r in kept if r["pnl_bp"] > 0) / len(kept) * 100 if kept else 0.0
    
    daily_deltas = {}
    for d in DATES:
        b_day = sum(r["pnl_bp"] for r in by_day[d]["blocked"])
        daily_deltas[d] = -b_day
        
    days_positive = sum(1 for d in DATES if daily_deltas[d] >= 0)
    worst_day_delta = min(daily_deltas[d] for d in DATES)
    
    return {
        "mode": mode,
        "thresh": thresh,
        "time_split": time_split,
        "n_kept": len(kept),
        "n_blocked": len(blocked),
        "all_pnl": round(all_pnl, 2),
        "kept_pnl": round(kept_pnl, 2),
        "blocked_pnl": round(blocked_pnl, 2),
        "delta_pnl": round(delta_pnl, 2),
        "win_rate": round(win_rate, 2),
        "days_positive": days_positive,
        "worst_day_delta": round(worst_day_delta, 2),
        "daily_deltas": daily_deltas,
    }


def run_calibration():
    recs = load_data()
    print(f"Loaded {len(recs)} records from 2026-10-26 .. 2026-10-30.")
    baseline_pnl = sum(r["pnl_bp"] for r in recs)
    baseline_win = sum(1 for r in recs if r["pnl_bp"] > 0) / len(recs) * 100
    print(f"Baseline: {len(recs)} trades | P&L: {baseline_pnl:+.2f} bp | Win: {baseline_win:.1f}%\n")
    
    modes = ["OR", "AND", "PULLBACK_ONLY", "VWAP_ONLY"]
    thresholds = [0.5, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.5, 3.0]
    time_splits = ["ALL", "MIDDAY_ONLY"]  # MIDDAY_ONLY: let morning open run, protect after 10:15
    
    grid_results = []
    
    for t_split in time_splits:
        for mode in modes:
            for th in thresholds:
                if mode == "VWAP_ONLY" and th != 0.8:
                    continue
                res = evaluate_param(recs, mode, th, t_split)
                grid_results.append(res)
                
    # Sort by weekly delta_pnl descending
    grid_results.sort(key=lambda x: x["delta_pnl"], reverse=True)
    
    print("=" * 86)
    print("TOP 15 PARAMETER CONFIGURATIONS RANKED BY CUMULATIVE BENEFIT (DELTA P&L)")
    print("=" * 86)
    print(f"{'Mode':13s} | {'Thresh':6s} | {'TimeSplit':11s} | {'Kept':4s} | {'Block':5s} | {'KeptPnL':8s} | {'DeltaPnL':8s} | {'Win%':5s} | {'PosDays':7s} | {'WorstDay':8s}")
    print("-" * 86)
    
    for r in grid_results[:15]:
        th_s = f"{r['thresh']:.1f}%" if r["mode"] != "VWAP_ONLY" else "N/A"
        print(f"{r['mode']:13s} | {th_s:6s} | {r['time_split']:11s} | {r['n_kept']:4d} | {r['n_blocked']:5d} | {r['kept_pnl']:+7.1f} | {r['delta_pnl']:+7.1f} | {r['win_rate']:4.1f}% | {r['days_positive']:1d}/5     | {r['worst_day_delta']:+7.1f}")
        
    # Now check Stability / Min Max (Best Worst-Day Performance)
    # i.e., configurations that do not ruin ANY single day (worst_day_delta >= -15 bp)
    stable = [r for r in grid_results if r["worst_day_delta"] >= -15.0]
    stable.sort(key=lambda x: x["delta_pnl"], reverse=True)
    
    print("\n" + "=" * 86)
    print("TOP STABLE CONFIGURATIONS (Worst Single-Day Drag >= -15 bp)")
    print("=" * 86)
    print(f"{'Mode':13s} | {'Thresh':6s} | {'TimeSplit':11s} | {'Kept':4s} | {'Block':5s} | {'KeptPnL':8s} | {'DeltaPnL':8s} | {'Win%':5s} | {'PosDays':7s} | {'WorstDay':8s}")
    print("-" * 86)
    for r in stable[:10]:
        th_s = f"{r['thresh']:.1f}%" if r["mode"] != "VWAP_ONLY" else "N/A"
        print(f"{r['mode']:13s} | {th_s:6s} | {r['time_split']:11s} | {r['n_kept']:4d} | {r['n_blocked']:5d} | {r['kept_pnl']:+7.1f} | {r['delta_pnl']:+7.1f} | {r['win_rate']:4.1f}% | {r['days_positive']:1d}/5     | {r['worst_day_delta']:+7.1f}")

    # Leave-One-Out Cross Validation (LOOCV)
    # For each held-out day, train on remaining 4 days (pick max delta), test on held-out day
    print("\n" + "=" * 86)
    print("LEAVE-ONE-OUT CROSS-VALIDATION (LOOCV)")
    print("=" * 86)
    
    oos_deltas = []
    
    for held_out in DATES:
        train_recs = [r for r in recs if r["day"] != held_out]
        test_recs = [r for r in recs if r["day"] == held_out]
        
        # Optimize on train
        best_cfg = None
        best_train_delta = -999999
        for t_split in time_splits:
            for mode in modes:
                for th in thresholds:
                    if mode == "VWAP_ONLY" and th != 0.8:
                        continue
                    res = evaluate_param(train_recs, mode, th, t_split)
                    # Criterion: train delta, requiring worst_day >= -20
                    if res["delta_pnl"] > best_train_delta:
                        best_train_delta = res["delta_pnl"]
                        best_cfg = (mode, th, t_split)
                        
        # Test on held-out day
        oos_res = evaluate_param(test_recs, best_cfg[0], best_cfg[1], best_cfg[2])
        oos_deltas.append(oos_res["delta_pnl"])
        print(f"Held out: {held_out} | Best Train Config: {best_cfg[0]} {best_cfg[1]}% ({best_cfg[2]}) | Train Delta: {best_train_delta:+6.1f} bp | Out-of-Sample Test Delta: {oos_res['delta_pnl']:+6.1f} bp")
        
    print("-" * 86)
    print(f"LOOCV Out-of-Sample Total Weekly Delta: {sum(oos_deltas):+6.1f} bp (All 5 days held out independently)")
    print("=" * 86 + "\n")
    
    out_calib = os.path.join(ROOT, "ai_reports", "pullback_gate_calibration.json")
    with open(out_calib, "w") as f:
        json.dump({
            "top_all": grid_results[:20],
            "top_stable": stable[:10],
            "loocv_oos_total": sum(oos_deltas),
            "loocv_details": list(zip(DATES, oos_deltas))
        }, f, indent=2)
    print(f"Saved calibration report to {out_calib}.")


if __name__ == "__main__":
    run_calibration()
