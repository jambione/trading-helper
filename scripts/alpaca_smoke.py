#!/usr/bin/env python3
"""Read-only Alpaca smoke test — run this BEFORE flipping the bridge to alpaca.

Prints account, positions, open orders, and a latest-quote → depth-1 L2Book.
Places NO orders.

Usage:
    # keys from env (or signal_engine.env via your shell)
    ALPACA_API_KEY=... ALPACA_SECRET_KEY=... \\
        python3 scripts/alpaca_smoke.py [SYMBOL]

    # or export from signal_engine.env first
    set -a && source signal_engine.env && set +a
    python3 scripts/alpaca_smoke.py AAPL

Paper is the default (alpaca_paper=true). Pass --live only if you intend to
hit the live account for a read-only check (still places no orders).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load signal_engine.env if present and keys not already set
_env = ROOT / "signal_engine.env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().split("#")[0].strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def main():
    ap = argparse.ArgumentParser(description="Alpaca bridge read-only smoke test")
    ap.add_argument("symbol", nargs="?", default="AAPL")
    ap.add_argument("--live", action="store_true",
                    help="use live account (default: paper)")
    ap.add_argument("--feed", default=os.getenv("ALPACA_DATA_FEED", "IEX"),
                    choices=("IEX", "SIP", "iex", "sip"))
    args = ap.parse_args()
    symbol = args.symbol.upper()

    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        sys.exit("Set ALPACA_API_KEY / ALPACA_SECRET_KEY (env or signal_engine.env)")

    cfg = {
        "alpaca_api_key": key,
        "alpaca_secret_key": secret,
        "alpaca_paper": not args.live,
        "alpaca_data_feed": args.feed.upper(),
        "alpaca_poll_sec": 0.5,
    }

    from webull_bridge.providers.alpaca import (
        AlpacaBroker, AlpacaMarketData, quote_to_book,
    )

    print(f"mode={'LIVE' if args.live else 'PAPER'}  feed={cfg['alpaca_data_feed']}")
    print(f"key  ={key[:8]}…")

    broker = AlpacaBroker(cfg)
    import asyncio

    async def run():
        acct = await broker.account()
        print("\n── account ─────────────────────────────")
        print(acct.to_dict())

        pos = await broker.positions()
        print("\n── positions ───────────────────────────")
        if not pos:
            print("(none)")
        for p in pos:
            print(p.to_dict())

        orders = await broker.orders()
        print("\n── orders (open+recent closed) ─────────")
        if not orders:
            print("(none)")
        for o in orders[:15]:
            print(o.to_dict())

        md = AlpacaMarketData(cfg)
        print(f"\n── latest quote → L2Book  {symbol} ─────")
        book = await md.snapshot(symbol)
        if book is None:
            print("✗ no quote / could not build depth-1 book")
            # raw dump for debugging
            try:
                from alpaca.data.historical import StockHistoricalDataClient
                from alpaca.data.requests import StockLatestQuoteRequest
                client = StockHistoricalDataClient(key, secret)
                raw = client.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=symbol))
                print("raw:", raw)
                print("parsed:", quote_to_book(
                    raw.get(symbol) if isinstance(raw, dict) else raw))
            except Exception as e:
                print("raw fetch failed:", e)
            sys.exit(1)

        print(f"✓ depth-1  bid={book.best_bid} x {book.bids[0][1]:.0f}  "
              f"ask={book.best_ask} x {book.asks[0][1]:.0f}  "
              f"mid={book.mid:.4f}  spr={book.spread:.4f}  "
              f"touch_skew={book.imbalance:.2f}")

    asyncio.run(run())
    print("\nOK — safe to set config/webull_bridge.json "
          "provider (or broker_provider / market_data_provider) to \"alpaca\"")


if __name__ == "__main__":
    main()
