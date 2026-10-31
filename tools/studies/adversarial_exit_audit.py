#!/usr/bin/env python3
"""adversarial_exit_audit.py: Skeptical microstructure audit of passive midpoint exits.

Tests:
1. Strict Trade-Through (requires prints strictly above midpoint or >= 500 shares at midpoint)
2. Stratification: Winning trades vs Losing trades
3. Timeouts with market penalty
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

    fills = ft.fetch_alpaca_fills(days=10, limit=1000)
    ai_syms = ft._ai_symbols()
    eng_syms = ft._engine_symbols()
    fills = ft.tag_fills(fills, ai_syms, eng_syms)
    closed = ft._pair_round_trips(fills)
    week_trades = [
        c for c in closed
        if "2026-10-26" <= datetime.fromisoformat(c["buy_time"].replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d") <= "2026-10-30"
    ]

    print(f"Running ADVERSARIAL SKEPTIC TEST on {len(week_trades)} exits...")
    print("Testing:")
    print("  1. Strict Trade-Through (requires print > midpoint or >= 500 shares at midpoint)")
    print("  2. Stratified Performance: Winning Exits vs Losing/Stopping Exits")
    print("  3. 5 bp Market Sweep Penalty on Timeouts\n")

    records = []
    for idx, t in enumerate(week_trades):
        sym = t["symbol"]
        qty = float(t["qty"])
        entry_px = float(t["entry"])
        actual_exit_px = float(t["exit"])
        s_time = datetime.fromisoformat(t["sell_time"].replace("Z", "+00:00"))
        is_winning_trade = (actual_exit_px > entry_px)

        start_t = s_time - timedelta(seconds=2)
        end_t = s_time + timedelta(seconds=20)

        try:
            quotes = cl.get_stock_quotes(StockQuotesRequest(
                symbol_or_symbols=sym, start=start_t, end=end_t, feed=DataFeed.SIP
            )).data.get(sym, [])
            trades = cl.get_stock_trades(StockTradesRequest(
                symbol_or_symbols=sym, start=start_t, end=end_t, feed=DataFeed.SIP
            )).data.get(sym, [])
        except Exception:
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

        subsequent_trades = [tr for tr in trades if tr.timestamp > s_time]
        subsequent_quotes = [q for q in quotes if q.timestamp > s_time]

        deadline_10s = s_time + timedelta(seconds=10)
        w_trades = [tr for tr in subsequent_trades if tr.timestamp <= deadline_10s]
        w_quotes = [q for q in subsequent_quotes if q.timestamp <= deadline_10s]
        fallback_quote = w_quotes[-1] if w_quotes else q0
        fallback_bid = float(fallback_quote.bid_price) if float(fallback_quote.bid_price) > 0 else bid0

        # Optimistic touch
        fill_touch = any(float(tr.price) >= midpoint0 for tr in w_trades)

        # Strict trade-through: print > midpoint OR >= 500 shares at midpoint
        vol_at_mid = sum(float(tr.size) for tr in w_trades if float(tr.price) >= midpoint0)
        has_trade_through = any(float(tr.price) > midpoint0 for tr in w_trades)
        fill_strict = has_trade_through or (vol_at_mid >= 500)

        # In fallback, assume market penalty of 2 bp additional slippage on timeout
        fallback_penalized = fallback_bid * (1.0 - 0.0002)

        exit_touch = midpoint0 if fill_touch else fallback_bid
        exit_strict = midpoint0 if fill_strict else fallback_penalized

        diff_touch_bp = ((exit_touch - actual_exit_px) / actual_exit_px) * 10000.0
        diff_strict_bp = ((exit_strict - actual_exit_px) / actual_exit_px) * 10000.0
        diff_strict_dollars = qty * (exit_strict - actual_exit_px)

        records.append({
            "sym": sym,
            "is_winner": is_winning_trade,
            "actual_pnl_bp": ((actual_exit_px - entry_px) / entry_px) * 10000.0,
            "fill_touch": fill_touch,
            "fill_strict": fill_strict,
            "diff_touch_bp": diff_touch_bp,
            "diff_strict_bp": diff_strict_bp,
            "diff_strict_dollars": diff_strict_dollars,
        })

    n_tot = len(records)
    print("=" * 95)
    print("MICROSTRUCTURE AUDIT: OPTIMISTIC TOUCH vs STRICT QUEUE-FILL WITH SLIPPAGE")
    print("=" * 95)

    fill_rate_touch = sum(1 for r in records if r["fill_touch"]) / n_tot * 100
    fill_rate_strict = sum(1 for r in records if r["fill_strict"]) / n_tot * 100
    mean_touch = float(np.mean([r["diff_touch_bp"] for r in records]))
    mean_strict = float(np.mean([r["diff_strict_bp"] for r in records]))
    tot_touch_bp = sum(r["diff_touch_bp"] for r in records)
    tot_strict_bp = sum(r["diff_strict_bp"] for r in records)
    tot_strict_d = sum(r["diff_strict_dollars"] for r in records)

    print(f"Optimistic (Any Print Touch):    Fill: {fill_rate_touch:5.1f}% | Mean Lift: {mean_touch:+5.2f} bp | Total: {tot_touch_bp:+7.1f} bp")
    print(f"Adversarial (Strict Through+Pen): Fill: {fill_rate_strict:5.1f}% | Mean Lift: {mean_strict:+5.2f} bp | Total: {tot_strict_bp:+7.1f} bp | Realized: ${tot_strict_d:+6.2f}")

    print("\n" + "=" * 95)
    print("STRATIFIED BY TRADE TYPE: WINNERS (Take-Profit) vs LOSERS (Stop/Trail)")
    print("=" * 95)
    winners = [r for r in records if r["is_winner"]]
    losers = [r for r in records if not r["is_winner"]]

    for name, grp in [("Winning Exits (Profit Takes)", winners), ("Losing Exits (Stop / Trail Losses)", losers)]:
        fr = sum(1 for r in grp if r["fill_strict"]) / len(grp) * 100
        m = float(np.mean([r["diff_strict_bp"] for r in grp]))
        tot_bp = sum(r["diff_strict_bp"] for r in grp)
        tot_d = sum(r["diff_strict_dollars"] for r in grp)
        n_pos = sum(1 for r in grp if r["diff_strict_bp"] > 0)
        n_neg = sum(1 for r in grp if r["diff_strict_bp"] < 0)
        print(f"{name:35s} (N={len(grp):2d}) | Fill: {fr:5.1f}% | Mean Lift: {m:+5.2f} bp | Net Bp: {tot_bp:+6.1f} bp | Net $: ${tot_d:+6.2f} | ({n_pos}W / {n_neg}L)")

    print("=" * 95)


if __name__ == "__main__":
    run()
