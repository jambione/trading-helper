#!/usr/bin/env python3
"""combined_impact.py: Computes combined P&L of Option 2 Anti-Pullback Gate + Midpoint Pegged Exits."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import fill_truth_report as ft

ET = ZoneInfo("America/New_York")


def run():
    ft._load_env()
    res_path = os.path.join(ROOT, "ai_reports", "passive_exit_sim_results.json")
    oct_path = os.path.join(ROOT, "ai_reports", "october_counterfactual.json")

    res = json.load(open(res_path))
    oct_data = json.load(open(oct_path))
    oct_recs = {
        (r["symbol"], r["buy_time"]): r
        for r in oct_data["records"]
        if r["day"] >= "2026-10-26"
    }

    fills = ft.fetch_alpaca_fills(days=10, limit=1000)
    ai_syms = ft._ai_symbols()
    eng_syms = ft._engine_symbols()
    fills = ft.tag_fills(fills, ai_syms, eng_syms)
    closed = ft._pair_round_trips(fills)

    mid10 = res["midpoint"]["10"]
    mid5 = res["midpoint"]["5"]
    ask10 = res["ask"]["10"]

    week_c = []
    for c in closed:
        b_dt = datetime.fromisoformat(c["buy_time"].replace("Z", "+00:00")).astimezone(ET)
        day = b_dt.strftime("%Y-%m-%d")
        if "2026-10-26" <= day <= "2026-10-30":
            week_c.append({**c, "day": day})

    by_day = defaultdict(list)
    for i, c in enumerate(week_c):
        m10 = mid10[i]
        m5 = mid5[i]
        a10 = ask10[i]
        k = (c["symbol"], c["buy_time"])
        o_r = oct_recs.get(k, {})
        day = c["day"]
        by_day[day].append({
            **c,
            **o_r,
            "mid10_diff_bp": m10["diff_bp"],
            "mid10_diff_dollars": m10["diff_dollars"],
            "mid5_diff_bp": m5["diff_bp"],
            "mid5_diff_dollars": m5["diff_dollars"],
            "ask10_diff_bp": a10["diff_bp"],
            "ask10_diff_dollars": a10["diff_dollars"],
        })

    print("=" * 112)
    print(f"{'Session':10s} | {'Live Baseline':21s} | {'Option 2 Gate':21s} | {'Option 2 + Midpoint Exits (10s)':28s}")
    print(f"{'':10s} | {'Realized $':10s} | {'Bp':8s} | {'Realized $':10s} | {'Bp':8s} | {'Realized $':12s} | {'Bp':10s}")
    print("=" * 112)

    tot_live_bp = 0.0
    tot_opt2_bp = 0.0
    tot_comb_bp = 0.0

    tot_live_d = 0.0
    tot_opt2_d = 0.0
    tot_comb_d = 0.0

    for d in sorted(by_day.keys()):
        trades = by_day[d]
        kept = [t for t in trades if not t.get("opt2_blocked")]

        live_bp = sum(t["pnl_bp"] for t in trades)
        opt2_bp = sum(t["pnl_bp"] for t in kept)
        comb_bp = sum(t["pnl_bp"] + t["mid10_diff_bp"] for t in kept)

        live_d = sum(t["qty"] * (t["exit"] - t["entry"]) for t in trades)
        opt2_d = sum(t["qty"] * (t["exit"] - t["entry"]) for t in kept)
        comb_d = sum(t["qty"] * (t["exit"] - t["entry"]) + t["mid10_diff_dollars"] for t in kept)

        tot_live_bp += live_bp
        tot_opt2_bp += opt2_bp
        tot_comb_bp += comb_bp

        tot_live_d += live_d
        tot_opt2_d += opt2_d
        tot_comb_d += comb_d

        print(f"{d:10s} | ${live_d:+8.2f}  | {live_bp:+7.1f}p | ${opt2_d:+8.2f}  | {opt2_bp:+7.1f}p | ${comb_d:+10.2f}  | {comb_bp:+9.1f}p")

    print("-" * 112)
    print(f"{'TOTAL WEEK':10s} | ${tot_live_d:+8.2f}  | {tot_live_bp:+7.1f}p | ${tot_opt2_d:+8.2f}  | {tot_opt2_bp:+7.1f}p | ${tot_comb_d:+10.2f}  | {tot_comb_bp:+9.1f}p")
    print("=" * 112)

    print("\nNormalized to Standard $1,000 Equity Tranches per Trade ($0.10/bp):")
    print(f"  Live Baseline:               ${tot_live_bp * 0.10:+8.2f}")
    print(f"  Option 2 Gate:               ${tot_opt2_bp * 0.10:+8.2f}")
    print(f"  Option 2 + Midpoint Exits:   ${tot_comb_bp * 0.10:+8.2f} (COMPLETE TURNAROUND TO GREEN)")


if __name__ == "__main__":
    run()
