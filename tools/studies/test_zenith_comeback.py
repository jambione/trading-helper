#!/usr/bin/env python3
"""Empirical study: Zenith (Peak MFE) for positions that 'come back' (reverse/scratch/lose).

Answers:
1. What is the most common zenith (peak gain in $ and %) for trades that reverse?
2. How many trades went green before rolling over and ending red or flat?
3. What is the optimal "I'll take it" threshold to capture those gains before the pullback?
"""
import json
import statistics
from pathlib import Path
from collections import Counter

OUTCOMES_PATH = Path("ai_reports/outcomes.jsonl")

def run_zenith_study():
    trades = []
    with OUTCOMES_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except Exception:
                pass

    print(f"Total historical trades loaded: {len(trades)}")

    # Filter trades with valid entry and peak
    valid_trades = []
    for t in trades:
        entry = t.get("entry_price")
        peak = t.get("peak_price")
        exit_px = t.get("exit_price") or t.get("exit_price_approx")
        pnl = t.get("realized_pl_usd")
        if entry and peak and exit_px and entry > 0:
            zenith_cents = peak - entry
            zenith_pct = (peak - entry) / entry * 100.0
            valid_trades.append({
                "symbol": t.get("symbol"),
                "entry": entry,
                "peak": peak,
                "exit": exit_px,
                "pnl": pnl if pnl is not None else (exit_px - entry) * (t.get("total_qty") or 1.0),
                "zenith_cents": zenith_cents,
                "zenith_pct": zenith_pct,
                "exit_gain_cents": exit_px - entry,
                "exit_gain_pct": (exit_px - entry) / entry * 100.0,
                "qty": t.get("total_qty") or 1.0,
                "arm": t.get("arm_why") or (t.get("features") or {}).get("arm_why") or "",
                "seat": t.get("exh_seat_class_fill") or (t.get("features") or {}).get("exh_seat_class") or "",
            })

    print(f"Valid trades with entry and peak: {len(valid_trades)}")

    # Subset 1: Went green (zenith > 0), but closed RED (pnl < 0 / exit < entry) - "Came all the way back to red"
    came_back_to_red = [t for t in valid_trades if t["zenith_cents"] > 0.005 and t["exit_gain_cents"] < -0.005]

    # Subset 2: Went green (zenith > 0), but closed at Scratch / Red (pnl <= +$0.02/sh) - "Gave back virtually all profit"
    came_back_flat_or_red = [t for t in valid_trades if t["zenith_cents"] > 0.005 and t["exit_gain_cents"] <= 0.01]

    # Subset 3: Retraced >= 60% from zenith (gave back majority of the move)
    gave_back_60pct = [t for t in valid_trades if t["zenith_cents"] > 0.02 and (t["exit_gain_cents"] <= 0.40 * t["zenith_cents"])]

    print(f"\nBreakdown:")
    print(f"  - Trades that went green but ended RED (full comeback to loss): {len(came_back_to_red)} ({len(came_back_to_red)/len(valid_trades)*100:.1f}%)")
    print(f"  - Trades that went green but ended SCRATCH or RED (exit <= +$0.01): {len(came_back_flat_or_red)} ({len(came_back_flat_or_red)/len(valid_trades)*100:.1f}%)")
    print(f"  - Trades that gave back >= 60% of their peak: {len(gave_back_60pct)} ({len(gave_back_60pct)/len(valid_trades)*100:.1f}%)")

    def print_distribution(name, dataset):
        cents = [t["zenith_cents"] for t in dataset]
        pcts = [t["zenith_pct"] for t in dataset]
        if not cents:
            return
        cents.sort()
        pcts.sort()
        n = len(cents)

        med_c = statistics.median(cents)
        mean_c = statistics.mean(cents)
        p25_c = cents[int(0.25 * n)]
        p75_c = cents[int(0.75 * n)]
        p90_c = cents[int(0.90 * n)]

        med_p = statistics.median(pcts)
        mean_p = statistics.mean(pcts)
        p25_p = pcts[int(0.25 * n)]
        p75_p = pcts[int(0.75 * n)]
        p90_p = pcts[int(0.90 * n)]

        print(f"\n========================================================")
        print(f"ZENITH DISTRIBUTION: {name} (N = {n})")
        print(f"========================================================")
        print(f"Metric        | Gain / Share ($) | Gain (%)")
        print(f"------------- | ---------------- | --------")
        print(f"25th %        | +${p25_c:.3f}          | +{p25_p:.2f}%")
        print(f"Median (50th) | +${med_c:.3f}          | +{med_p:.2f}%")
        print(f"Mean (Avg)    | +${mean_c:.3f}          | +{mean_p:.2f}%")
        print(f"75th %        | +${p75_c:.3f}          | +{p75_p:.2f}%")
        print(f"90th %        | +${p90_c:.3f}          | +{p90_p:.2f}%")

        # Cents Histogram (rounded to nearest 2 cents)
        rounded_cents = [round(c, 2) for c in cents]
        c_bins = [
            ("+$0.01 to +$0.02", lambda x: 0.005 <= x < 0.025),
            ("+$0.03 to +$0.04", lambda x: 0.025 <= x < 0.045),
            ("+$0.05 to +$0.06", lambda x: 0.045 <= x < 0.065),
            ("+$0.07 to +$0.08", lambda x: 0.065 <= x < 0.085),
            ("+$0.09 to +$0.10", lambda x: 0.085 <= x < 0.105),
            ("+$0.11 to +$0.15", lambda x: 0.105 <= x < 0.155),
            ("+$0.16 to +$0.20", lambda x: 0.155 <= x < 0.205),
            ("+$0.21 to +$0.30", lambda x: 0.205 <= x < 0.305),
            ("Over +$0.30",      lambda x: x >= 0.305),
        ]

        print(f"\nZenith In Cents Breakdown:")
        for label, cond in c_bins:
            cnt = sum(1 for c in cents if cond(c))
            pct_cnt = cnt / n * 100.0
            bar = "█" * int(pct_cnt / 2)
            print(f"  {label:18s}: {cnt:4d} ({pct_cnt:5.1f}%) {bar}")

        # Tiers of potential "I'll take it" triggers
        test_thresholds = [0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]
        print(f"\nHow Many Of These 'Comeback' Trades Reached 'I'll take it' Targets Before Reversing?")
        print(f"Threshold ($/sh) | Trades Reaching It | % of Total Comebacks | Would Have Rescued")
        print(f"---------------- | ------------------ | -------------------- | ------------------")
        for thr in test_thresholds:
            reached = sum(1 for c in cents if c >= thr - 1e-6)
            share = reached / n * 100.0
            print(f" +${thr:.2f} / share   | {reached:5d}              | {share:5.1f}%                | {reached} trades saved from loss")

    # Dual Pre-Square and Square active subset
    square_trades = [t for t in valid_trades if "square" in t["arm"].lower() or "square" in t["seat"].lower()]
    sq_came_back = [t for t in square_trades if t["zenith_cents"] > 0.005 and t["exit_gain_cents"] <= 0.01]
    if sq_came_back:
        print_distribution("ACTIVE DUAL PRE-SQUARE & SQUARE TRADES THAT CAME BACK FLAT/RED", sq_came_back)

if __name__ == "__main__":
    run_zenith_study()
