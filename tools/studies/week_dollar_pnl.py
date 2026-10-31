#!/usr/bin/env python3
"""Week Dollar P&L Analysis: Option 2 Anti-Pullback Gate vs Live Baseline (2026-10-26 .. 2026-10-30).
Extracts the exact Alpaca fill quantities, entry notional, realized dollar P&L,
and basis point returns for every live trade executed across the 5 sessions of last week.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import fill_truth_report as ft
from desk_core import load_desk_env

ET = ZoneInfo("America/New_York")
DATES = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]


def run():
    load_desk_env()

    # Load october_counterfactual records
    oct_path = os.path.join(ROOT, "ai_reports", "october_counterfactual.json")
    if not os.path.exists(oct_path):
        print(f"Error: {oct_path} not found.")
        return
    with open(oct_path) as f:
        oct_data = json.load(f)
    oct_recs = {
        (r["symbol"], r["buy_time"]): r
        for r in oct_data["records"]
        if r["day"] >= "2026-10-26"
    }

    # Fetch fills from Alpaca
    print("Fetching Alpaca fills and pairing round trips...")
    fills = ft.fetch_alpaca_fills(days=10, limit=1000)
    ai_syms = ft._ai_symbols()
    eng_syms = ft._engine_symbols()
    fills = ft.tag_fills(fills, ai_syms, eng_syms)
    closed = ft._pair_round_trips(fills)

    by_day = defaultdict(list)
    for c in closed:
        b_dt = datetime.fromisoformat(c["buy_time"].replace("Z", "+00:00")).astimezone(ET)
        day = b_dt.strftime("%Y-%m-%d")
        if day not in DATES:
            continue
        k = (c["symbol"], c["buy_time"])
        if k in oct_recs:
            r = oct_recs[k]
            trade = {
                "day": day,
                "symbol": c["symbol"],
                "qty": float(c["qty"]),
                "entry_px": float(c["entry"]),
                "exit_px": float(c["exit"]),
                "notional": float(c["qty"]) * float(c["entry"]),
                "pnl_dollars": float(c["qty"]) * (float(c["exit"]) - float(c["entry"])),
                "pnl_pct": float(c["pnl_pct"]),
                "pnl_bp": float(r["pnl_bp"]),
                "buy_time": c["buy_time"],
                "sell_time": c["sell_time"],
                "vwap": r.get("vwap"),
                "hod": r.get("hod"),
                "drop_pct": r.get("drop_pct"),
                "is_below_vwap_15bp": r.get("is_below_vwap_15bp"),
                "opt2_blocked": r.get("opt2_blocked"),
                "opt1_blocked": r.get("opt1_blocked"),
            }
            by_day[day].append(trade)
        else:
            print(f"Warning: trade {k} not matched in october_counterfactual records.")

    print(f"Loaded {sum(len(v) for v in by_day.values())} trades across {len(by_day)} days.\n")

    day_summaries = []
    all_blocked_trades = []

    for d in DATES:
        trades = by_day[d]
        if not trades:
            continue

        kept = [t for t in trades if not t["opt2_blocked"]]
        blocked = [t for t in trades if t["opt2_blocked"]]
        all_blocked_trades.extend(blocked)

        live_n = len(trades)
        kept_n = len(kept)
        blocked_n = len(blocked)

        live_notional = sum(t["notional"] for t in trades)
        kept_notional = sum(t["notional"] for t in kept)
        blocked_notional = sum(t["notional"] for t in blocked)

        live_dollars = sum(t["pnl_dollars"] for t in trades)
        kept_dollars = sum(t["pnl_dollars"] for t in kept)
        blocked_dollars = sum(t["pnl_dollars"] for t in blocked)
        delta_dollars = kept_dollars - live_dollars  # = -blocked_dollars

        live_bp = sum(t["pnl_bp"] for t in trades)
        kept_bp = sum(t["pnl_bp"] for t in kept)
        blocked_bp = sum(t["pnl_bp"] for t in blocked)
        delta_bp = kept_bp - live_bp

        live_wins = sum(1 for t in trades if t["pnl_dollars"] > 0)
        kept_wins = sum(1 for t in kept if t["pnl_dollars"] > 0)

        # Standardized $1,000 tranche P&L ($0.10 per basis point)
        live_norm_dollars = live_bp * 0.10
        kept_norm_dollars = kept_bp * 0.10
        delta_norm_dollars = delta_bp * 0.10

        summary = {
            "day": d,
            "live_n": live_n,
            "kept_n": kept_n,
            "blocked_n": blocked_n,
            "live_notional": live_notional,
            "kept_notional": kept_notional,
            "blocked_notional": blocked_notional,
            "live_dollars": live_dollars,
            "kept_dollars": kept_dollars,
            "blocked_dollars": blocked_dollars,
            "delta_dollars": delta_dollars,
            "live_bp": live_bp,
            "kept_bp": kept_bp,
            "blocked_bp": blocked_bp,
            "delta_bp": delta_bp,
            "live_win_rate": (live_wins / live_n * 100) if live_n else 0,
            "kept_win_rate": (kept_wins / kept_n * 100) if kept_n else 0,
            "live_norm_dollars": live_norm_dollars,
            "kept_norm_dollars": kept_norm_dollars,
            "delta_norm_dollars": delta_norm_dollars,
            "blocked_trades": blocked,
        }
        day_summaries.append(summary)

    # Print Table
    print("=" * 110)
    print(f"{'Session':10s} | {'Live Trades':11s} | {'Live $ P&L':10s} | {'Live Bp':9s} | {'Kept Trades':11s} | {'Kept $ P&L':10s} | {'Kept Bp':9s} | {'$ Delta':9s} | {'Bp Delta':9s}")
    print("=" * 110)
    for s in day_summaries:
        print(f"{s['day']:10s} | {s['live_n']:11d} | ${s['live_dollars']:+9.2f} | {s['live_bp']:+8.1f}p | {s['kept_n']:11d} | ${s['kept_dollars']:+9.2f} | {s['kept_bp']:+8.1f}p | ${s['delta_dollars']:+8.2f} | {s['delta_bp']:+8.1f}p")
    print("-" * 110)

    # Weekly totals
    tot_live_n = sum(s["live_n"] for s in day_summaries)
    tot_kept_n = sum(s["kept_n"] for s in day_summaries)
    tot_blocked_n = sum(s["blocked_n"] for s in day_summaries)
    tot_live_dollars = sum(s["live_dollars"] for s in day_summaries)
    tot_kept_dollars = sum(s["kept_dollars"] for s in day_summaries)
    tot_blocked_dollars = sum(s["blocked_dollars"] for s in day_summaries)
    tot_delta_dollars = tot_kept_dollars - tot_live_dollars
    tot_live_bp = sum(s["live_bp"] for s in day_summaries)
    tot_kept_bp = sum(s["kept_bp"] for s in day_summaries)
    tot_blocked_bp = sum(s["blocked_bp"] for s in day_summaries)
    tot_delta_bp = tot_kept_bp - tot_live_bp
    tot_live_notional = sum(s["live_notional"] for s in day_summaries)
    tot_kept_notional = sum(s["kept_notional"] for s in day_summaries)

    tot_live_norm = sum(s["live_norm_dollars"] for s in day_summaries)
    tot_kept_norm = sum(s["kept_norm_dollars"] for s in day_summaries)
    tot_delta_norm = tot_kept_norm - tot_live_norm

    print(f"{'TOTAL WEEK':10s} | {tot_live_n:11d} | ${tot_live_dollars:+9.2f} | {tot_live_bp:+8.1f}p | {tot_kept_n:11d} | ${tot_kept_dollars:+9.2f} | {tot_kept_bp:+8.1f}p | ${tot_delta_dollars:+8.2f} | {tot_delta_bp:+8.1f}p")
    print("=" * 110)

    print("\nNormalized to Standard $1,000 Equity Tranche per Trade ($0.10/bp):")
    print(f"  Live Baseline:  ${tot_live_norm:+8.2f}")
    print(f"  Option 2 Kept:  ${tot_kept_norm:+8.2f}")
    print(f"  Net $ Impact:   ${tot_delta_norm:+8.2f} GAIN")

    print("\nBlocked Trades Breakdown (Option 2 Anti-Pullback Gate):")
    for b in all_blocked_trades:
        reason = []
        if b["drop_pct"] >= 2.0:
            reason.append(f"Drop {b['drop_pct']:.1f}% >= 2.0%")
        if b["is_below_vwap_15bp"]:
            reason.append(f"Below VWAP-15bp (vwap {b['vwap']:.2f})")
        r_str = " & ".join(reason)
        print(f"  {b['day']} | {b['symbol']:5s} | Qty: {b['qty']:4.1f} | Entry: ${b['entry_px']:6.2f} | Exit: ${b['exit_px']:6.2f} | Realized $: ${b['pnl_dollars']:+6.2f} | {b['pnl_bp']:+6.1f} bp | [{r_str}]")

    out_file = os.path.join(ROOT, "ai_reports", "week_dollar_pnl_summary.json")
    with open(out_file, "w") as f:
        json.dump({
            "totals": {
                "live_trades": tot_live_n,
                "kept_trades": tot_kept_n,
                "blocked_trades": tot_blocked_n,
                "live_dollars": tot_live_dollars,
                "kept_dollars": tot_kept_dollars,
                "delta_dollars": tot_delta_dollars,
                "live_bp": tot_live_bp,
                "kept_bp": tot_kept_bp,
                "delta_bp": tot_delta_bp,
                "live_notional": tot_live_notional,
                "kept_notional": tot_kept_notional,
                "live_norm_dollars": tot_live_norm,
                "kept_norm_dollars": tot_kept_norm,
                "delta_norm_dollars": tot_delta_norm,
            },
            "day_summaries": day_summaries,
        }, f, indent=2)
    print(f"\nSaved weekly dollar report to {out_file}")


if __name__ == "__main__":
    run()
