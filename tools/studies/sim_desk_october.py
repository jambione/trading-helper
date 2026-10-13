#!/usr/bin/env python3
"""Simulation and Performance Evaluation of the Desk against Logs Since October 1, 2026.

Analyzes:
1. Actual Baseline Performance across all sessions (2026-10-01 to 2026-10-13):
   - Win/Loss/Scratch counts, Win Rate, Realized P&L ($ and bp), $/trade, Median hold
   - Performance breakdown by session date
   - Performance breakdown by watchlist source (tight, movers, momentum, etc.)
   - Performance breakdown by entry arm (dual_square, presquare, mid_rise, etc.)
   - Performance breakdown by exit reason (local_trail, no_progress, eod_liquidate, etc.)

2. Peak MFE / Zenith Run-up Distribution:
   - What share of October trades went green vs never went green
   - Distribution of peak gains before rollover (25th, Median, Mean, 75th, 90th percentile in cents and %)
   - How many green trades came all the way back to red or flat under the previous trailing stop

3. Counterfactual Simulation of Shipped / Current Desk Rules:
   - Shipped Exit Parameters:
     * Breakeven Lock at +0.15% (locks stop to Entry + $0.01 once peaked >= +0.15%)
     * Profit Capture at +0.30% (immediate banking upon reaching +0.30%)
     * Quick Decay Idle at 4.0s
   - Alternative Exit Parameter Grid:
     * Current Shipped: BE=0.15%, Cap=0.30%
     * Conservative: BE=0.25%, Cap=0.50%
     * High Win-Rate: BE=0.15%, Cap=0.25%
     * Quick Scalp: BE=0.18%, Cap=0.35%
     * Pure BE Lock only (no cap): BE=0.15%, Cap=None
     * Pure Profit Capture only: BE=None, Cap=0.30%
   - Evaluated on:
     * All 531 October trades
     * Active Dual Pre-Square & Full Square setups specifically
     * Tight vs Movers sources

4. Sizing Simulation:
   - Actual sizing vs Option B Sizing ($1,000 notional per position, 2 positions max)
"""
from __future__ import annotations

import datetime
import json
import math
import os
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OUTCOMES_PATH = Path("ai_reports/outcomes.jsonl")


def load_october_trades():
    trades = []
    if not OUTCOMES_PATH.exists():
        print(f"Error: {OUTCOMES_PATH} not found.", file=sys.stderr)
        return trades

    with OUTCOMES_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                ts = d.get("entry_time") or d.get("ts")
                if not ts:
                    continue
                dt = datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).astimezone(ET)
                day_str = dt.strftime("%Y-%m-%d")
                if day_str >= "2026-10-01":
                    ent = d.get("entry_price")
                    ext = d.get("exit_price") or d.get("exit_price_approx")
                    pk = d.get("peak_price") or ext
                    qty = d.get("total_qty") or 1.0
                    pnl = d.get("realized_pl_usd")
                    if ent and ext and ent > 0 and ext > 0:
                        orig_pnl = pnl if pnl is not None else (ext - ent) * qty
                        bp = (ext / ent - 1.0) * 1e4
                        zenith_cents = (pk - ent) if pk else 0.0
                        zenith_pct = ((pk - ent) / ent * 100.0) if pk else 0.0
                        arm = str(d.get("arm_why") or (d.get("features") or {}).get("arm_why") or "unknown")
                        seat = str(d.get("exh_seat_class_fill") or (d.get("features") or {}).get("exh_seat_class") or "")
                        src = str(d.get("duel_source") or (d.get("features") or {}).get("source") or d.get("source") or "unknown").lower()
                        why = str(d.get("closing_reason") or d.get("close_reason") or "other")
                        hold = float(d.get("hold_sec") or 0.0)

                        trades.append({
                            "raw": d,
                            "symbol": d.get("symbol"),
                            "day": day_str,
                            "time": dt.strftime("%H:%M:%S"),
                            "hour": dt.hour,
                            "entry": ent,
                            "exit": ext,
                            "peak": pk,
                            "qty": qty,
                            "orig_pnl": orig_pnl,
                            "bp": bp,
                            "zenith_cents": zenith_cents,
                            "zenith_pct": zenith_pct,
                            "arm": arm,
                            "seat": seat,
                            "source": src,
                            "why": why,
                            "hold_sec": hold,
                            "is_square": "square" in arm.lower() or "square" in seat.lower(),
                        })
            except Exception:
                pass
    return trades


def run_simulation(trades, be_pct: float | None, cap_pct: float | None, fixed_notional: float | None = None):
    """Simulate trade outcomes with breakeven lock and profit capture."""
    sim_trades = []
    for t in trades:
        ent = t["entry"]
        ext = t["exit"]
        qty = t["qty"]
        orig_pnl = t["orig_pnl"]
        z_pct = t["zenith_pct"]
        z_cents = t["zenith_cents"]

        if fixed_notional is not None:
            # calculate quantity for fixed notional (e.g. $1000)
            sim_qty = max(1.0, round(fixed_notional / ent))
        else:
            sim_qty = qty

        sim_exit_px = ext
        exit_rule = "actual_trail"

        # Check profit capture first
        if cap_pct is not None and z_pct >= cap_pct:
            sim_exit_px = ent * (1.0 + cap_pct / 100.0)
            exit_rule = f"profit_cap_{cap_pct:.2f}%"
        # Check breakeven floor
        elif be_pct is not None and z_pct >= be_pct:
            be_px = ent + 0.01  # entry + 1 cent
            if ext < be_px:
                sim_exit_px = be_px
                exit_rule = f"be_lock_{be_pct:.2f}%"

        sim_pl = (sim_exit_px - ent) * sim_qty
        sim_bp = (sim_exit_px / ent - 1.0) * 1e4
        rescued = (orig_pnl < -0.05 and sim_pl > -0.05)

        sim_trades.append({
            "trade": t,
            "sim_exit_px": sim_exit_px,
            "sim_pl": sim_pl,
            "sim_bp": sim_bp,
            "exit_rule": exit_rule,
            "rescued": rescued,
            "is_win": sim_pl > 0.05,
            "is_loss": sim_pl < -0.05,
            "is_scratch": -0.05 <= sim_pl <= 0.05,
        })
    return sim_trades


def summarize_results(sim_trades):
    n = len(sim_trades)
    if n == 0:
        return {}
    wins = [x for x in sim_trades if x["is_win"]]
    losses = [x for x in sim_trades if x["is_loss"]]
    scratches = [x for x in sim_trades if x["is_scratch"]]
    rescued = [x for x in sim_trades if x["rescued"]]
    pls = [x["sim_pl"] for x in sim_trades]
    bps = [x["sim_bp"] for x in sim_trades]

    win_rate = len(wins) / n * 100.0
    loss_rate = len(losses) / n * 100.0
    scratch_rate = len(scratches) / n * 100.0

    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "scratches": len(scratches),
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        "scratch_rate": scratch_rate,
        "total_pl": sum(pls),
        "mean_pl": st.mean(pls),
        "mean_bp": st.mean(bps),
        "median_bp": st.median(bps),
        "rescued_count": len(rescued),
    }


def main():
    trades = load_october_trades()
    print("=" * 90)
    print(f"DESK PERFORMANCE & SIMULATION REPORT SINCE OCTOBER 1, 2026")
    print(f"Total October trades recorded in logs: {len(trades)}")
    print("=" * 90)

    if not trades:
        print("No trades found since October 1.")
        return

    # Section 1: Actual Baseline Performance by Day
    print("\n" + "-" * 90)
    print("SECTION 1: ACTUAL RECORDED BASELINE PERFORMANCE (OCT 1 - OCT 13)")
    print("-" * 90)

    by_day = defaultdict(list)
    for t in trades:
        by_day[t["day"]].append(t)

    print(f"{'Date':<12} | {'Trades':<7} | {'Win Rate':<10} | {'Loss Rate':<10} | {'Mean bp':<9} | {'Median bp':<10} | {'Total P&L':<10}")
    print("-" * 90)
    for day in sorted(by_day.keys()):
        d_trades = by_day[day]
        pls = [t["orig_pnl"] for t in d_trades]
        bps = [t["bp"] for t in d_trades]
        wins = sum(1 for p in pls if p > 0.05)
        losses = sum(1 for p in pls if p < -0.05)
        wr = wins / len(d_trades) * 100.0
        lr = losses / len(d_trades) * 100.0
        print(f"{day:<12} | {len(d_trades):<7} | {wr:5.1f}%     | {lr:5.1f}%     | {st.mean(bps):+7.1f}   | {st.median(bps):+8.1f}   | ${sum(pls):+8.2f}")

    all_pls = [t["orig_pnl"] for t in trades]
    all_bps = [t["bp"] for t in trades]
    all_wins = sum(1 for p in all_pls if p > 0.05)
    all_losses = sum(1 for p in all_pls if p < -0.05)
    all_scratches = sum(1 for p in all_pls if -0.05 <= p <= 0.05)
    print("-" * 90)
    print(f"{'OVERALL ACTUAL':<12} | {len(trades):<7} | {all_wins/len(trades)*100:5.1f}%     | {all_losses/len(trades)*100:5.1f}%     | {st.mean(all_bps):+7.1f}   | {st.median(all_bps):+8.1f}   | ${sum(all_pls):+8.2f}")
    print(f"Wins: {all_wins} | Losses: {all_losses} | Scratches: {all_scratches}")

    # Baseline by Source
    print("\n" + "-" * 90)
    print("ACTUAL BASELINE BY WATCHLIST SOURCE")
    print("-" * 90)
    by_source = defaultdict(list)
    for t in trades:
        by_source[t["source"]].append(t)
    print(f"{'Source':<15} | {'Trades':<7} | {'Win Rate':<10} | {'Loss Rate':<10} | {'Mean bp':<9} | {'Total P&L':<10}")
    print("-" * 90)
    for src, s_trades in sorted(by_source.items(), key=lambda x: -len(x[1])):
        pls = [t["orig_pnl"] for t in s_trades]
        bps = [t["bp"] for t in s_trades]
        w = sum(1 for p in pls if p > 0.05)
        l = sum(1 for p in pls if p < -0.05)
        print(f"{src:<15} | {len(s_trades):<7} | {w/len(s_trades)*100:5.1f}%     | {l/len(s_trades)*100:5.1f}%     | {st.mean(bps):+7.1f}   | ${sum(pls):+8.2f}")

    # Baseline by Arm / Setup
    print("\n" + "-" * 90)
    print("ACTUAL BASELINE BY ENTRY ARM / SETUP")
    print("-" * 90)
    by_arm = defaultdict(list)
    for t in trades:
        by_arm[t["arm"]].append(t)
    print(f"{'Arm / Why':<35} | {'Trades':<7} | {'Win Rate':<10} | {'Loss Rate':<10} | {'Mean bp':<9} | {'Total P&L':<10}")
    print("-" * 90)
    for arm, a_trades in sorted(by_arm.items(), key=lambda x: -len(x[1])):
        pls = [t["orig_pnl"] for t in a_trades]
        bps = [t["bp"] for t in a_trades]
        w = sum(1 for p in pls if p > 0.05)
        l = sum(1 for p in pls if p < -0.05)
        print(f"{arm[:34]:<35} | {len(a_trades):<7} | {w/len(a_trades)*100:5.1f}%     | {l/len(a_trades)*100:5.1f}%     | {st.mean(bps):+7.1f}   | ${sum(pls):+8.2f}")

    # Baseline by Exit Reason
    print("\n" + "-" * 90)
    print("ACTUAL BASELINE BY EXIT REASON")
    print("-" * 90)
    by_why = defaultdict(list)
    for t in trades:
        by_why[t["why"]].append(t)
    print(f"{'Exit Reason':<25} | {'Trades':<7} | {'Mean Hold':<10} | {'Mean bp':<9} | {'Total P&L':<10}")
    print("-" * 90)
    for why, w_trades in sorted(by_why.items(), key=lambda x: -len(x[1])):
        pls = [t["orig_pnl"] for t in w_trades]
        bps = [t["bp"] for t in w_trades]
        holds = [t["hold_sec"] for t in w_trades]
        print(f"{why:<25} | {len(w_trades):<7} | {st.mean(holds):6.0f}s    | {st.mean(bps):+7.1f}   | ${sum(pls):+8.2f}")

    # Section 2: Zenith (Peak Run-up) Analysis
    print("\n" + "=" * 90)
    print("SECTION 2: PEAK RUN-UP (ZENITH / MFE) ANALYSIS")
    print("=" * 90)

    ever_green = [t for t in trades if t["zenith_cents"] > 0.005]
    never_green = [t for t in trades if t["zenith_cents"] <= 0.005]
    green_came_red = [t for t in trades if t["zenith_cents"] > 0.005 and t["orig_pnl"] < -0.05]
    green_came_flat = [t for t in trades if t["zenith_cents"] > 0.005 and -0.05 <= t["orig_pnl"] <= 0.05]

    print(f"Trades that ever went green (> +$0.005) : {len(ever_green)} ({len(ever_green)/len(trades)*100:.1f}%)")
    print(f"Trades that never went green           : {len(never_green)} ({len(never_green)/len(trades)*100:.1f}%)")
    print(f"Went green, but ended RED (lost)       : {len(green_came_red)} ({len(green_came_red)/len(trades)*100:.1f}%)")
    print(f"Went green, but ended SCRATCH (flat)   : {len(green_came_flat)} ({len(green_came_flat)/len(trades)*100:.1f}%)")
    print(f"Total trades reversing after green     : {len(green_came_red)+len(green_came_flat)} ({(len(green_came_red)+len(green_came_flat))/len(trades)*100:.1f}%)")

    # Zenith distribution for trades that ever went green
    z_cents = sorted(t["zenith_cents"] for t in ever_green)
    z_pcts = sorted(t["zenith_pct"] for t in ever_green)
    n_g = len(z_cents)
    p25_c, med_c, p75_c, p90_c = z_cents[int(0.25*n_g)], st.median(z_cents), z_cents[int(0.75*n_g)], z_cents[int(0.90*n_g)]
    p25_p, med_p, p75_p, p90_p = z_pcts[int(0.25*n_g)], st.median(z_pcts), z_pcts[int(0.75*n_g)], z_pcts[int(0.90*n_g)]

    print("\nZenith Distribution across all green trades:")
    print(f"  25th Percentile : +${p25_c:.3f} (+{p25_p:.2f}%)")
    print(f"  Median (50th)   : +${med_c:.3f} (+{med_p:.2f}%)")
    print(f"  Mean (Average)  : +${st.mean(z_cents):.3f} (+{st.mean(z_pcts):.2f}%)")
    print(f"  75th Percentile : +${p75_c:.3f} (+{p75_p:.2f}%)")
    print(f"  90th Percentile : +${p90_c:.3f} (+{p90_p:.2f}%)")

    # Zenith hit thresholds
    th_pcts = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.75, 1.00]
    print("\nShare of October trades reaching peak run-up milestones:")
    for th in th_pcts:
        hits = sum(1 for t in trades if t["zenith_pct"] >= th)
        print(f"  Peaked >= +{th:4.2f}% : {hits:3d}/{len(trades)} ({hits/len(trades)*100:5.1f}%)")

    # Section 3: Simulation Across Configuration Grid
    print("\n" + "=" * 90)
    print("SECTION 3: SIMULATION OF EXIT RULES & PROFIT CAPTURE ACROSS ALL OCTOBER TRADES")
    print("=" * 90)

    configs = [
        ("Actual Baseline (Recorded)", None, None),
        ("Current Desk (BE=0.15%, Cap=0.30%)", 0.15, 0.30),
        ("Prior Threshold (BE=0.25%, Cap=0.50%)", 0.25, 0.50),
        ("High Win-Rate (BE=0.15%, Cap=0.25%)", 0.15, 0.25),
        ("Quick Scalp (BE=0.18%, Cap=0.35%)", 0.18, 0.35),
        ("BE Lock Only (BE=0.15%, Cap=None)", 0.15, None),
        ("Profit Capture Only (BE=None, Cap=0.30%)", None, 0.30),
    ]

    print("\n--- A. ALL 531 OCTOBER TRADES (ACTUAL RECORDED SHARE SIZING) ---")
    print(f"{'Configuration':<40} | {'Win %':<7} | {'Loss %':<7} | {'Scratch %':<9} | {'Rescued':<9} | {'Mean bp':<8} | {'Total P&L':<10}")
    print("-" * 105)
    for label, be_val, cap_val in configs:
        res = summarize_results(run_simulation(trades, be_val, cap_val))
        print(f"{label:<40} | {res['win_rate']:5.1f}% | {res['loss_rate']:5.1f}% | {res['scratch_rate']:5.1f}%   | {res['rescued_count']:4d} trds | {res['mean_bp']:+6.1f}bp | ${res['total_pl']:+8.2f}")

    print("\n--- B. ACTIVE DUAL PRE-SQUARE & SQUARE SETUPS (N = " + str(sum(1 for t in trades if t["is_square"])) + ") ---")
    sq_trades = [t for t in trades if t["is_square"]]
    print(f"{'Configuration':<40} | {'Win %':<7} | {'Loss %':<7} | {'Scratch %':<9} | {'Rescued':<9} | {'Mean bp':<8} | {'Total P&L':<10}")
    print("-" * 105)
    for label, be_val, cap_val in configs:
        res = summarize_results(run_simulation(sq_trades, be_val, cap_val))
        print(f"{label:<40} | {res['win_rate']:5.1f}% | {res['loss_rate']:5.1f}% | {res['scratch_rate']:5.1f}%   | {res['rescued_count']:4d} trds | {res['mean_bp']:+6.1f}bp | ${res['total_pl']:+8.2f}")

    print("\n--- C. TIGHT SOURCE TRADES (N = " + str(sum(1 for t in trades if t["source"] == "tight")) + ") ---")
    tight_trades = [t for t in trades if t["source"] == "tight"]
    print(f"{'Configuration':<40} | {'Win %':<7} | {'Loss %':<7} | {'Scratch %':<9} | {'Rescued':<9} | {'Mean bp':<8} | {'Total P&L':<10}")
    print("-" * 105)
    for label, be_val, cap_val in configs:
        res = summarize_results(run_simulation(tight_trades, be_val, cap_val))
        print(f"{label:<40} | {res['win_rate']:5.1f}% | {res['loss_rate']:5.1f}% | {res['scratch_rate']:5.1f}%   | {res['rescued_count']:4d} trds | {res['mean_bp']:+6.1f}bp | ${res['total_pl']:+8.2f}")

    # Section 4: Day-by-Day Simulation of Current Desk Configuration (BE=0.15%, Cap=0.30%)
    print("\n" + "=" * 90)
    print("SECTION 4: DAY-BY-DAY COMPARISON: ACTUAL BASELINE VS CURRENT DESK (BE=0.15%, Cap=0.30%)")
    print("=" * 90)
    print(f"{'Date':<12} | {'Trades':<6} | {'Actual WR':<10} | {'Sim WR':<10} | {'Actual bp':<10} | {'Sim bp':<10} | {'Actual $':<10} | {'Sim $':<10}")
    print("-" * 92)

    for day in sorted(by_day.keys()):
        d_tr = by_day[day]
        actual_res = summarize_results(run_simulation(d_tr, None, None))
        sim_res = summarize_results(run_simulation(d_tr, 0.15, 0.30))
        print(f"{day:<12} | {len(d_tr):<6} | {actual_res['win_rate']:5.1f}%     | {sim_res['win_rate']:5.1f}%     | {actual_res['mean_bp']:+6.1f} bp   | {sim_res['mean_bp']:+6.1f} bp   | ${actual_res['total_pl']:+8.2f} | ${sim_res['total_pl']:+8.2f}")

    # Section 5: Position Sizing Simulation (Option B: $1000 Notional per Trade)
    print("\n" + "=" * 90)
    print("SECTION 5: POSITION SIZING SIMULATION (OPTION B: $1,000 NOTIONAL PER TRADE)")
    print("=" * 90)
    print(f"{'Configuration':<40} | {'Win %':<7} | {'Loss %':<7} | {'Mean bp':<8} | {'Total P&L ($1k/trade)':<22}")
    print("-" * 95)
    for label, be_val, cap_val in configs:
        res = summarize_results(run_simulation(trades, be_val, cap_val, fixed_notional=1000.0))
        print(f"{label:<40} | {res['win_rate']:5.1f}% | {res['loss_rate']:5.1f}% | {res['mean_bp']:+6.1f}bp | ${res['total_pl']:+12.2f}")

    print("\n" + "=" * 90)
    print("END OF SIMULATION REPORT")
    print("=" * 90)


if __name__ == "__main__":
    main()
