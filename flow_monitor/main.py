#!/usr/bin/env python3
"""Alpaca Flow Monitor — top-of-book + confidence banner (no multi-level L2).

Streams / polls Alpaca latest quotes, builds depth-1 books, and runs the
shared flow_core SignalEngine + LongView stance. Replaces desktop OCR L2.

Usage:
    python3 -m flow_monitor.main [SYMBOL]
    python3 -m flow_monitor.main --symbol NVDA --feed SIP

Credentials: ALPACA_API_KEY / ALPACA_SECRET_KEY (env or signal_engine.env).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load signal_engine.env if keys not already set
_env = ROOT / "signal_engine.env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().split("#")[0].strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v

HERE = Path(__file__).resolve().parent
CFG_PATH = HERE / "config.json"


def load_cfg() -> dict:
    cfg = {}
    if CFG_PATH.exists():
        try:
            cfg = json.loads(CFG_PATH.read_text())
        except Exception:
            pass
    return cfg


def main():
    file_cfg = load_cfg()
    ap = argparse.ArgumentParser(description="Alpaca Flow Monitor")
    ap.add_argument("symbol", nargs="?",
                    default=file_cfg.get("symbol", "AAPL"))
    ap.add_argument("--feed", default=file_cfg.get("feed", "IEX"),
                    choices=("IEX", "SIP", "iex", "sip"))
    ap.add_argument("--poll", type=float,
                    default=float(file_cfg.get("poll_sec", 1.0)))
    args = ap.parse_args()
    symbol = args.symbol.upper()
    feed = args.feed.upper()

    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        sys.exit("Set ALPACA_API_KEY / ALPACA_SECRET_KEY")

    from flow_core import LongView, SignalEngine
    from trade_bridge.providers.alpaca import AlpacaMarketData

    bridge_cfg = {
        "alpaca_api_key": key,
        "alpaca_secret_key": secret,
        "alpaca_data_feed": feed,
        "alpaca_poll_sec": args.poll,
        "trend_window": int(file_cfg.get("trend_window", 300)),
        "long_confirm_secs": int(file_cfg.get("long_confirm_secs", 20)),
        "long_window": 60,
        "wall_multiple": 4.0,
        "imbalance_buy": 1.8,
        "imbalance_sell": 0.55,
        "confirm_reads": 3,
        "max_spread_pct": 1.0,
        "alert_cooldown": 30,
        "min_room_pct": 1.0,
        "momentum_reads": 6,
        "max_downtrend_pct": 0.2,
        "trend_min_coverage": 0.6,
        "tape_min_sided": 4,
        "tape_sided_share": 0.5,
        "tape_dom_min": 0.25,
        "projection_minutes": 5.0,
    }

    md = AlpacaMarketData(bridge_cfg)
    eng = SignalEngine(bridge_cfg)
    lv = LongView(bridge_cfg)

    log_path = HERE / "flow_log.csv"
    csv_on = bool(file_cfg.get("csv_log", True))
    writer = None
    log_f = None
    if csv_on:
        new = not log_path.exists()
        log_f = open(log_path, "a", newline="", encoding="utf-8")
        writer = csv.writer(log_f)
        if new:
            writer.writerow([
                "ts", "symbol", "mid", "bid", "ask", "bid_sz", "ask_sz",
                "spread", "touch_skew", "stance", "agree", "total",
                "trend5", "held",
            ])

    print(f"Alpaca Flow Monitor  symbol={symbol}  feed={feed}  "
          f"poll={args.poll}s  (Ctrl+C to stop)")
    print("NBBO only — not multi-level L2. Walls are not claimed.\n")

    try:
        while True:
            t0 = time.time()
            book = md._fetch(symbol)
            if book is None:
                print(f"\r{datetime.now():%H:%M:%S}  {symbol}  no quote    ",
                      end="", flush=True)
            else:
                sig = eng.update(book)
                t5 = eng.trend_pct(bridge_cfg["trend_window"])
                state = lv.update(book, t5, [], time.time())
                stance = state["stance"]
                agree = state.get("agree", 0)
                total = state.get("total", 0)
                held = state.get("held", 0)
                dots = "●" * agree + "○" * max(0, total - agree)
                line = (
                    f"{datetime.now():%H:%M:%S}  {symbol}  "
                    f"mid={book.mid:.4f}  "
                    f"bid={book.best_bid:.2f}x{book.bids[0][1]:.0f}  "
                    f"ask={book.best_ask:.2f}x{book.asks[0][1]:.0f}  "
                    f"spr={book.spread:.3f}  skew={book.imbalance:.2f}  "
                    f"{stance} {dots}({agree}/{total})  "
                    f"t5={t5 if t5 is not None else '…'}  "
                    f"held={held:.0f}s"
                )
                if sig:
                    line += f"  signal={sig.action}"
                print(f"\r{line}    ", end="", flush=True)
                if writer:
                    writer.writerow([
                        datetime.now(timezone.utc).isoformat(),
                        symbol, round(book.mid, 4),
                        book.best_bid, book.best_ask,
                        book.bids[0][1], book.asks[0][1],
                        round(book.spread, 4), round(book.imbalance, 3),
                        stance, agree, total,
                        round(t5, 4) if t5 is not None else "",
                        round(held, 1),
                    ])
                    log_f.flush()
            time.sleep(max(0.05, args.poll - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        if log_f:
            log_f.close()


if __name__ == "__main__":
    main()
