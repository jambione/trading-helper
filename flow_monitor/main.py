#!/usr/bin/env python3
"""Alpaca Flow Monitor (Flow-B) — NBBO + tape + session VWAP confidence banner.

Usage:
    python3 -m flow_monitor.main [SYMBOL]
    python3 -m flow_monitor.main NVDA --feed SIP --watch AAPL,MSFT,AMD

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
    if CFG_PATH.exists():
        try:
            return json.loads(CFG_PATH.read_text())
        except Exception:
            pass
    return {}


def _beep(high: bool = True):
    try:
        # macOS / linux terminal bell; Windows may ignore
        print("\a", end="", flush=True)
    except Exception:
        pass


def main():
    file_cfg = load_cfg()
    ap = argparse.ArgumentParser(description="Alpaca Flow Monitor")
    ap.add_argument("symbol", nargs="?",
                    default=file_cfg.get("symbol", "AAPL"))
    ap.add_argument("--feed", default=file_cfg.get("feed", "IEX"),
                    choices=("IEX", "SIP", "iex", "sip"))
    ap.add_argument("--poll", type=float,
                    default=float(file_cfg.get("poll_sec", 1.0)))
    ap.add_argument("--watch", default=file_cfg.get("watch", ""),
                    help="comma-separated symbols to warm VWAP/tape")
    ap.add_argument("--no-screener", action="store_true")
    args = ap.parse_args()

    focus = args.symbol.upper()
    feed = args.feed.upper()
    watch = [s.strip().upper() for s in str(args.watch).split(",") if s.strip()]
    if focus not in watch:
        watch = [focus] + watch
    symbols_max = int(file_cfg.get("symbols_max", 30))
    watch = watch[:symbols_max]

    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        sys.exit("Set ALPACA_API_KEY / ALPACA_SECRET_KEY")

    from flow_core import LongView, SessionVWAP, SignalEngine, tape_gate_ok
    from trade_bridge.providers.alpaca import AlpacaMarketData, feed_kw as _feed_kw
    from flow_monitor.tape import TapeWindow
    from flow_monitor.trades import fetch_recent_trades
    from flow_monitor.screener import MoversCache

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
        "alert_cooldown": float(file_cfg.get("alert_cooldown", 30)),
        "min_room_pct": 1.0,
        "momentum_reads": 6,
        "max_downtrend_pct": 0.2,
        "trend_min_coverage": 0.6,
        "tape_min_sided": int(file_cfg.get("tape_min_sided", 4)),
        "tape_sided_share": float(file_cfg.get("tape_sided_share", 0.5)),
        "tape_dom_min": float(file_cfg.get("tape_dom_min", 0.25)),
        "vwap_min_age": float(file_cfg.get("vwap_min_age", 900)),
        "projection_minutes": 5.0,
        "book_pillar": False,
    }

    md = AlpacaMarketData(bridge_cfg)
    feed_kw = _feed_kw(bridge_cfg)
    # reuse historical client from market data for trades
    data_client = md.client

    eng = SignalEngine(bridge_cfg)
    lv = LongView(bridge_cfg)
    session_vwap = SessionVWAP()
    tapes: dict[str, TapeWindow] = {s: TapeWindow(60.0) for s in watch}

    screener = None
    if not args.no_screener and file_cfg.get("screener", True):
        screener = MoversCache(key, secret,
                               ttl=float(file_cfg.get("screener_sec", 45)))

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
                "trend5", "tape_dom", "tape_sided_n", "vwap", "vwap_age",
                "held",
            ])

    last_stance = None
    last_alert = 0.0
    trade_poll_every = max(1, int(2.0 / max(args.poll, 0.2)))  # ~ every 2s
    tick = 0

    print(f"Alpaca Flow Monitor  focus={focus}  feed={feed}  "
          f"watch={','.join(watch)}  poll={args.poll}s")
    print("NBBO + tape + session VWAP — not multi-level L2.  Ctrl+C to stop.\n")

    try:
        while True:
            t0 = time.time()
            tick += 1
            # warm trades for all watched symbols periodically
            if tick % trade_poll_every == 1:
                for sym in watch:
                    book_snap = md._last.get(sym)
                    bid = book_snap.best_bid if book_snap else 0.0
                    ask = book_snap.best_ask if book_snap else 0.0
                    for ts, px, sz in fetch_recent_trades(
                            data_client, sym, lookback_sec=90.0, feed_kw=feed_kw):
                        tapes[sym].side_print(px, sz, bid, ask, ts=ts)
                        session_vwap.ingest(sym, px, sz, ts=ts)

            book = md._fetch(focus)
            if book is None:
                print(f"\r{datetime.now():%H:%M:%S}  {focus}  no quote    ",
                      end="", flush=True)
            else:
                # side any very fresh trades against this quote
                for ts, px, sz in fetch_recent_trades(
                        data_client, focus, lookback_sec=5.0, feed_kw=feed_kw):
                    tapes[focus].side_print(px, sz, book.best_bid, book.best_ask,
                                            ts=ts)
                    session_vwap.ingest(focus, px, sz, ts=ts)

                tape = tapes[focus].snapshot()
                vwap = session_vwap.vwap(focus)
                vwap_age = session_vwap.age(focus)

                eng.update(book, tape=tape)
                t5 = eng.trend_pct(bridge_cfg["trend_window"])
                state = lv.update(
                    book, t5, [], time.time(),
                    tape=tape, vwap=vwap, vwap_age=vwap_age,
                    vwap_src="stream" if vwap else None,
                )
                stance = state["stance"]
                agree = state.get("agree", 0)
                total = state.get("total", 0)
                held = state.get("held", 0)
                dots = "●" * int(agree) + "○" * max(0, int(total) - int(agree))
                tape_live = tape_gate_ok(
                    tape, bridge_cfg["tape_min_sided"],
                    bridge_cfg["tape_sided_share"])

                movers_s = ""
                if screener:
                    mv = screener.get(6)
                    if mv:
                        movers_s = "  movers: " + " ".join(
                            f"{m['symbol']}{m['pct']:+.1f}%" for m in mv[:5])
                    elif screener.error:
                        movers_s = f"  movers:err"

                t5_s = f"{t5:+.3f}%" if t5 is not None else "…"
                tape_s = (f"LIVE {tape['dom']:+.2f}" if tape_live else "dark")
                vwap_s = f"{vwap:.2f}" if vwap else "…"
                age_s = f" age={vwap_age/60:.0f}m" if vwap_age else ""
                line = (
                    f"{datetime.now():%H:%M:%S}  {focus}  "
                    f"mid={book.mid:.4f}  "
                    f"{book.best_bid:.2f}x{book.bids[0][1]:.0f}/"
                    f"{book.best_ask:.2f}x{book.asks[0][1]:.0f}  "
                    f"spr={book.spread:.3f}  skew={book.imbalance:.2f}  "
                    f"{stance} {dots}({agree}/{total})  "
                    f"t5={t5_s}  tape={tape_s}  "
                    f"vwap={vwap_s}{age_s}  held={held:.0f}s"
                    f"{movers_s}"
                )
                print(f"\r{line}    ", end="", flush=True)

                # banner-transition alert
                if (stance != last_stance and last_stance is not None
                        and stance in ("LONG", "BEAR")
                        and time.time() - last_alert >= bridge_cfg["alert_cooldown"]):
                    print(f"\n  ⚡ STANCE {last_stance} → {stance}")
                    _beep(high=(stance == "LONG"))
                    last_alert = time.time()
                last_stance = stance

                if writer:
                    writer.writerow([
                        datetime.now(timezone.utc).isoformat(),
                        focus, round(book.mid, 4),
                        book.best_bid, book.best_ask,
                        book.bids[0][1], book.asks[0][1],
                        round(book.spread, 4), round(book.imbalance, 3),
                        stance, agree, total,
                        round(t5, 4) if t5 is not None else "",
                        round(tape["dom"], 4), tape["sided_n"],
                        round(vwap, 4) if vwap else "",
                        round(vwap_age, 1) if vwap_age else "",
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
