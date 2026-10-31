#!/usr/bin/env python3
"""october_passive_exit_sample.py: Tests Passive Midpoint Exits across all 22 sessions of October 2026."""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import fill_truth_report as ft
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockQuotesRequest, StockTradesRequest
from alpaca.data.enums import DataFeed

ET = ZoneInfo("America/New_York")


def run():
    ft._load_env()
    k = os.getenv("ALPACA_DATA_API_KEY") or os.getenv("ALPACA_API_KEY")
    s = os.getenv("ALPACA_DATA_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    cl = StockHistoricalDataClient(k, s)

    oct_data = json.load(open(os.path.join(ROOT, "ai_reports", "october_counterfactual.json")))
    recs = oct_data["records"]

    days = sorted(set(r["day"] for r in recs))
    sample = []
    for d in days:
        d_recs = [r for r in recs if r["day"] == d]
        sample.extend(d_recs[::max(1, len(d_recs) // 5)][:5])

    print(f"Evaluating {len(sample)} representative trades across all {len(days)} sessions of October 2026...")

    timeouts = [5, 10, 15]
    results = {
        "midpoint": {t: [] for t in timeouts},
        "ask": {t: [] for t in timeouts},
    }

    evaluated = 0

    for idx, t in enumerate(sample):
        sym = t["symbol"]
        entry_px = float(t["entry_px"])
        actual_exit_px = float(t["exit_px"])
        s_time = datetime.fromisoformat(t["sell_time"].replace("Z", "+00:00"))

        start_t = s_time - timedelta(seconds=2)
        end_t = s_time + timedelta(seconds=20)

        try:
            quotes = cl.get_stock_quotes(StockQuotesRequest(
                symbol_or_symbols=sym, start=start_t, end=end_t, feed=DataFeed.SIP
            )).data.get(sym, [])
            trades = cl.get_stock_trades(StockTradesRequest(
                symbol_or_symbols=sym, start=start_t, end=end_t, feed=DataFeed.SIP
            )).data.get(sym, [])
        except Exception as e:
            continue

        if not quotes:
            continue

        prior_quotes = [q for q in quotes if q.timestamp <= s_time]
        q0 = prior_quotes[-1] if prior_quotes else quotes[0]
        bid0 = float(q0.bid_price)
        ask0 = float(q0.ask_price)
        if bid0 <= 0 or ask0 <= 0 or ask0 < bid0:
            continue

        midpoint0 = round((bid0 + ask0) / 2.0, 2)
        spread_bp = ((ask0 - bid0) / ask0) * 10000.0

        subsequent_trades = [tr for tr in trades if tr.timestamp > s_time]
        subsequent_quotes = [q for q in quotes if q.timestamp > s_time]

        evaluated += 1

        for wait_sec in timeouts:
            deadline = s_time + timedelta(seconds=wait_sec)
            trades_in_window = [tr for tr in subsequent_trades if tr.timestamp <= deadline]
            quotes_in_window = [q for q in subsequent_quotes if q.timestamp <= deadline]
            fallback_quote = quotes_in_window[-1] if quotes_in_window else q0
            fallback_bid = float(fallback_quote.bid_price) if float(fallback_quote.bid_price) > 0 else bid0

            # Midpoint order
            mid_fill = any(float(tr.price) >= midpoint0 for tr in trades_in_window)
            sim_exit_mid = midpoint0 if mid_fill else fallback_bid
            diff_bp_mid = ((sim_exit_mid - actual_exit_px) / actual_exit_px) * 10000.0

            results["midpoint"][wait_sec].append({
                "sym": sym,
                "day": t["day"],
                "status": "FILLED_PASSIVE" if mid_fill else "TIMEOUT",
                "diff_bp": diff_bp_mid,
                "spread_bp": spread_bp,
            })

            # Ask order
            ask_fill = any(float(tr.price) >= ask0 for tr in trades_in_window)
            sim_exit_ask = ask0 if ask_fill else fallback_bid
            diff_bp_ask = ((sim_exit_ask - actual_exit_px) / actual_exit_px) * 10000.0

            results["ask"][wait_sec].append({
                "sym": sym,
                "day": t["day"],
                "status": "FILLED_PASSIVE" if ask_fill else "TIMEOUT",
                "diff_bp": diff_bp_ask,
                "spread_bp": spread_bp,
            })

        if (idx + 1) % 25 == 0 or idx == len(sample) - 1:
            print(f"Evaluated {idx + 1}/{len(sample)} trades...")

    print("\n" + "=" * 90)
    print("OCTOBER FULL-MONTH PASSIVE EXIT SIMULATION (Representative 110-Trade Audit)")
    print("=" * 90)

    for strat_name, label in [("midpoint", "Midpoint Pegged Limit Exit"), ("ask", "Ask Pegged Limit Exit (Maker)")]:
        print(f"\n--- Strategy: {label} ---")
        print(f"{'Wait Sec':10s} | {'Fill Rate':12s} | {'Mean Lift (bp)':16s} | {'Total Bp Lift':15s} | {'Win / Flat / Loss':18s}")
        print("-" * 80)
        for wait_sec in timeouts:
            recs = results[strat_name][wait_sec]
            n_tot = len(recs)
            n_filled = sum(1 for r in recs if r["status"] == "FILLED_PASSIVE")
            fill_rate = (n_filled / n_tot * 100) if n_tot else 0
            mean_bp = float(np.mean([r["diff_bp"] for r in recs])) if recs else 0
            tot_bp = sum(r["diff_bp"] for r in recs)

            n_pos = sum(1 for r in recs if r["diff_bp"] > 0)
            n_zero = sum(1 for r in recs if r["diff_bp"] == 0)
            n_neg = sum(1 for r in recs if r["diff_bp"] < 0)

            print(f"{wait_sec:2d} seconds  | {fill_rate:5.1f}% ({n_filled:2d}/{n_tot:2d}) | {mean_bp:+6.2f} bp/trade | {tot_bp:+8.1f} bp      | {n_pos}W / {n_zero}F / {n_neg}L")

    # Project to full October 923 trades
    mid10_recs = results["midpoint"][10]
    mean_10_bp = float(np.mean([r["diff_bp"] for r in mid10_recs]))
    proj_bp = mean_10_bp * 923.0
    print("\n" + "=" * 90)
    print(f"FULL-MONTH PROJECTION (923 trades at {mean_10_bp:+.2f} bp/trade lift):")
    print(f"  Projected October Spread Recapture: {proj_bp:+,.1f} basis points")
    print(f"  Projected Dollar Recapture @ $1k tranches:  ${proj_bp * 0.10:+,.2f}")
    print(f"  Projected Dollar Recapture @ $10k tranches: ${proj_bp * 1.00:+,.2f}")
    print("=" * 90)


if __name__ == "__main__":
    run()
