#!/usr/bin/env python3
"""test_exit_quotes.py: Analyze exit fills vs NBBO quotes."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockQuotesRequest
from alpaca.data.enums import DataFeed

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import fill_truth_report as ft

ET = ZoneInfo("America/New_York")


def run():
    ft._load_env()
    k = os.getenv("ALPACA_DATA_API_KEY") or os.getenv("ALPACA_API_KEY")
    s = os.getenv("ALPACA_DATA_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    cl = StockHistoricalDataClient(k, s)

    fills = ft.fetch_alpaca_fills(days=5, limit=200)
    ai_syms = ft._ai_symbols()
    eng_syms = ft._engine_symbols()
    fills = ft.tag_fills(fills, ai_syms, eng_syms)
    closed = ft._pair_round_trips(fills)

    sample = [c for c in closed if c.get("source") == "ai"][-12:]
    print(f"Analyzing {len(sample)} sample AI desk exits against SIP NBBO quotes...")
    print(f"{'Symbol':6s} | {'Sell Time ET':12s} | {'Fill Px':8s} | {'Bid':8s} | {'Ask':8s} | {'Spread (c)':10s} | {'Spread (bp)':11s} | {'Fill vs Bid':11s}")
    print("-" * 88)

    spreads_bp = []
    half_spreads_bp = []

    for t in sample:
        sym = t["symbol"]
        s_time = datetime.fromisoformat(t["sell_time"].replace("Z", "+00:00"))
        s_time_et = s_time.astimezone(ET)
        start_t = s_time - timedelta(seconds=5)
        end_t = s_time + timedelta(seconds=15)
        try:
            quotes = cl.get_stock_quotes(StockQuotesRequest(
                symbol_or_symbols=sym, start=start_t, end=end_t, feed=DataFeed.SIP
            )).data.get(sym, [])
        except Exception as e:
            print(f"Error fetching quotes for {sym}: {e}")
            continue

        if not quotes:
            continue

        q_at_sell = [q for q in quotes if q.timestamp <= s_time]
        q0 = q_at_sell[-1] if q_at_sell else quotes[0]
        bid = float(q0.bid_price)
        ask = float(q0.ask_price)
        sp_cents = ask - bid
        sp_bp = (sp_cents / ask) * 10000.0 if ask > 0 else 0.0
        half_sp_bp = sp_bp / 2.0
        fill_px = float(t["exit"])
        fill_diff = fill_px - bid

        spreads_bp.append(sp_bp)
        half_spreads_bp.append(half_sp_bp)

        t_str = s_time_et.strftime("%H:%M:%S")
        print(f"{sym:6s} | {t_str:12s} | ${fill_px:7.2f} | ${bid:7.2f} | ${ask:7.2f} | {sp_cents*100:6.1f}c    | {sp_bp:7.1f} bp   | {fill_diff:+7.2f}")

    if spreads_bp:
        print("-" * 88)
        print(f"Average Quoted Spread:      {sum(spreads_bp)/len(spreads_bp):.1f} bp")
        print(f"Average Exit Friction Cost: {sum(half_spreads_bp)/len(half_spreads_bp):.1f} bp per exit")


if __name__ == "__main__":
    run()
