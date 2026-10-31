#!/usr/bin/env python3
"""passive_exit_sim.py: Simulation of Passive 'Maker' Exits vs Market Exits.

Evaluates routing exit orders as passive limit orders (pegged at Midpoint or Ask)
with a time-collar (5s, 10s, 15s fallback to market), tested against actual millisecond
SIP trade prints and NBBO quotes across the 134 live trades from last week (2026-10-26 .. 2026-10-30).
"""
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

    print("Fetching Alpaca fills for last week (2026-10-26 .. 2026-10-30)...")
    fills = ft.fetch_alpaca_fills(days=10, limit=1000)
    ai_syms = ft._ai_symbols()
    eng_syms = ft._engine_symbols()
    fills = ft.tag_fills(fills, ai_syms, eng_syms)
    closed = ft._pair_round_trips(fills)

    # Filter to last week
    week_trades = []
    for c in closed:
        b_dt = datetime.fromisoformat(c["buy_time"].replace("Z", "+00:00")).astimezone(ET)
        day = b_dt.strftime("%Y-%m-%d")
        if "2026-10-26" <= day <= "2026-10-30":
            trade = dict(c)
            trade["day"] = day
            week_trades.append(trade)

    print(f"Loaded {len(week_trades)} live trades to evaluate.\n")

    timeouts = [5, 10, 15]
    results = {
        "midpoint": {t: [] for t in timeouts},
        "ask": {t: [] for t in timeouts},
    }

    evaluated_count = 0

    for idx, t in enumerate(week_trades):
        sym = t["symbol"]
        qty = float(t["qty"])
        entry_px = float(t["entry"])
        actual_exit_px = float(t["exit"])
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
            print(f"Error fetching tick data for {sym} at {s_time}: {e}")
            continue

        if not quotes:
            continue

        # Find NBBO quote at exit decision
        prior_quotes = [q for q in quotes if q.timestamp <= s_time]
        q0 = prior_quotes[-1] if prior_quotes else quotes[0]
        bid0 = float(q0.bid_price)
        ask0 = float(q0.ask_price)
        if bid0 <= 0 or ask0 <= 0 or ask0 < bid0:
            continue

        midpoint0 = round((bid0 + ask0) / 2.0, 2)
        spread_bp = ((ask0 - bid0) / ask0) * 10000.0

        # Trades occurring strictly after decision
        subsequent_trades = [tr for tr in trades if tr.timestamp > s_time]
        subsequent_quotes = [q for q in quotes if q.timestamp > s_time]

        evaluated_count += 1

        # Simulate for each timeout
        for wait_sec in timeouts:
            deadline = s_time + timedelta(seconds=wait_sec)
            trades_in_window = [tr for tr in subsequent_trades if tr.timestamp <= deadline]
            quotes_in_window = [q for q in subsequent_quotes if q.timestamp <= deadline]
            fallback_quote = quotes_in_window[-1] if quotes_in_window else q0
            fallback_bid = float(fallback_quote.bid_price) if float(fallback_quote.bid_price) > 0 else bid0

            # 1. Midpoint Order Simulation
            # Filled if any print >= midpoint
            mid_fill = any(float(tr.price) >= midpoint0 for tr in trades_in_window)
            if mid_fill:
                sim_exit_mid = midpoint0
                status_mid = "FILLED_PASSIVE"
            else:
                sim_exit_mid = fallback_bid
                status_mid = "TIMEOUT_MARKET"

            diff_bp_mid = ((sim_exit_mid - actual_exit_px) / actual_exit_px) * 10000.0
            diff_dollars_mid = qty * (sim_exit_mid - actual_exit_px)

            results["midpoint"][wait_sec].append({
                "sym": sym,
                "status": status_mid,
                "diff_bp": diff_bp_mid,
                "diff_dollars": diff_dollars_mid,
                "spread_bp": spread_bp,
            })

            # 2. Ask Order Simulation (Maker)
            # Filled if any print >= ask
            ask_fill = any(float(tr.price) >= ask0 for tr in trades_in_window)
            if ask_fill:
                sim_exit_ask = ask0
                status_ask = "FILLED_PASSIVE"
            else:
                sim_exit_ask = fallback_bid
                status_ask = "TIMEOUT_MARKET"

            diff_bp_ask = ((sim_exit_ask - actual_exit_px) / actual_exit_px) * 10000.0
            diff_dollars_ask = qty * (sim_exit_ask - actual_exit_px)

            results["ask"][wait_sec].append({
                "sym": sym,
                "status": status_ask,
                "diff_bp": diff_bp_ask,
                "diff_dollars": diff_dollars_ask,
                "spread_bp": spread_bp,
            })

        if (idx + 1) % 25 == 0 or idx == len(week_trades) - 1:
            print(f"Evaluated {idx + 1}/{len(week_trades)} trades...")

    print(f"\nSuccessfully evaluated {evaluated_count} live trades against high-resolution SIP tick tape.")
    print("=" * 95)
    print("PASSIVE 'MAKER' EXITS SIMULATION RESULTS (vs LIVE BASELINE MARKET EXITS)")
    print("=" * 95)

    for strat_name, label in [("midpoint", "Midpoint Pegged Limit Exit"), ("ask", "Ask Pegged Limit Exit (Full Maker)")]:
        print(f"\n--- Strategy: {label} ---")
        print(f"{'Wait Sec':10s} | {'Fill Rate':12s} | {'Mean Lift (bp)':16s} | {'Total Bp Lift':15s} | {'Total Realized $ Lift':23s}")
        print("-" * 85)
        for wait_sec in timeouts:
            recs = results[strat_name][wait_sec]
            n_tot = len(recs)
            n_filled = sum(1 for r in recs if r["status"] == "FILLED_PASSIVE")
            fill_rate = (n_filled / n_tot * 100) if n_tot else 0
            mean_bp = float(np.mean([r["diff_bp"] for r in recs])) if recs else 0
            tot_bp = sum(r["diff_bp"] for r in recs)
            tot_dollars = sum(r["diff_dollars"] for r in recs)

            print(f"{wait_sec:2d} seconds  | {fill_rate:5.1f}% ({n_filled:2d}/{n_tot:2d}) | {mean_bp:+6.2f} bp/trade | {tot_bp:+8.1f} bp      | ${tot_dollars:+8.2f}")

    out_file = os.path.join(ROOT, "ai_reports", "passive_exit_sim_results.json")
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved simulation records to {out_file}")


if __name__ == "__main__":
    run()
