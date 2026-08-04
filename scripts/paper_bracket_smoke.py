#!/usr/bin/env python3
"""Place ONE paper limit+bracket order (risk-sized) to verify the desk path.

Uses the same building blocks as monitor auto-limit:
  entry_pricing (optional join at ask) → desk_risk.plan_long → buy_limit_bracket

PAPER ONLY. Refuses live keys/mode for submit.

Usage:
  set -a && source signal_engine.env && set +a   # or rely on auto-load
  python3 scripts/paper_bracket_smoke.py              # AAPL, dry-run plan
  python3 scripts/paper_bracket_smoke.py --submit     # place paper order
  python3 scripts/paper_bracket_smoke.py SOFI --submit
  python3 scripts/paper_bracket_smoke.py --submit --cancel-after 30

Env: ALPACA_API_KEY, ALPACA_SECRET_KEY (paper keys from app.alpaca.markets)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


def _load_keys() -> tuple[str, str]:
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        sys.exit(
            "Set ALPACA_API_KEY / ALPACA_SECRET_KEY (paper) in env or signal_engine.env"
        )
    return key, secret


def _quote(symbol: str) -> tuple[float | None, float | None, float | None]:
    """Return (bid, ask, last) via alpaca data or trader helpers after init."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest
        from alpaca.data.enums import DataFeed

        key, secret = _load_keys()
        client = StockHistoricalDataClient(key, secret)
        feed = DataFeed.IEX
        q = client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=feed)
        )
        t = client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol, feed=feed)
        )
        quote = q.get(symbol) if isinstance(q, dict) else q
        trade = t.get(symbol) if isinstance(t, dict) else t
        bid = float(getattr(quote, "bid_price", 0) or 0) or None
        ask = float(getattr(quote, "ask_price", 0) or 0) or None
        last = float(getattr(trade, "price", 0) or 0) or None
        return bid, ask, last
    except Exception as e:
        print(f"  quote via data API failed: {e}")
        return None, None, None


def main() -> int:
    ap = argparse.ArgumentParser(description="Paper limit+bracket smoke test")
    ap.add_argument("symbol", nargs="?", default="AAPL", help="US equity ticker")
    ap.add_argument(
        "--submit",
        action="store_true",
        help="actually place the paper order (default: plan only)",
    )
    ap.add_argument("--risk-pct", type=float, default=0.35, help="%% of equity at risk")
    ap.add_argument("--stop-pct", type=float, default=0.40, help="%% below entry for stop")
    ap.add_argument("--reward-r", type=float, default=2.0, help="TP distance in R")
    ap.add_argument(
        "--max-notional",
        type=float,
        default=500.0,
        help="hard $ cap on entry notional (keeps smoke small)",
    )
    ap.add_argument(
        "--cancel-after",
        type=float,
        default=0,
        help="seconds to wait then cancel open orders for symbol (0=leave)",
    )
    ap.add_argument(
        "--force-join",
        action="store_true",
        help="use ask as entry (skip passive pricing)",
    )
    args = ap.parse_args()
    symbol = args.symbol.upper().strip()

    key, secret = _load_keys()
    print("mode=PAPER only (this script never uses live)")
    print(f"key={key[:6]}…  symbol={symbol}")

    import alpaca_trader as tr
    import desk_risk as dsk
    import entry_pricing as ep

    # Force paper
    tr.init(
        mode="paper",
        api_key=key,
        secret_key=secret,
        trade_amount=args.max_notional,
        extended_hours=False,
        use_brackets=False,
    )
    if not tr.is_active():
        sys.exit("trader init failed — check paper keys")

    equity = tr.get_equity()
    print(f"equity=${equity:,.2f}" if equity else "equity=unknown")

    bid, ask, last = _quote(symbol)
    print(f"quote bid={bid} ask={ask} last={last}")
    if not ask and not last:
        sys.exit("no quote — market closed or symbol invalid?")

    if args.force_join:
        entry = float(ask or last or 0)
        if entry <= 0:
            sys.exit("no ask/last for --force-join")
        pricing = {"style": "join_forced", "limit_px": entry}
        print(f"pricing: join_forced → limit=${entry:.2f}")
    else:
        # IEX top-of-book can be absurdly wide; try policy, then last, then mid.
        dec = ep.decide(
            bid=bid,
            ask=ask or last,
            last=last,
            rvol=2.0,
            proximity_pct=100,
            max_spread_pct=5.0,
            pad_pct=0.1,
            pad_max_pct=0.15,
        )
        if dec.ok and dec.limit_px:
            entry = float(dec.limit_px)
            pricing = dec.as_dict()
            print(f"pricing: {dec.reason} → limit=${entry:.2f}")
        elif last:
            entry = float(last)
            pricing = {"style": "last_fallback", "limit_px": entry, "note": dec.reason}
            print(f"pricing: last_fallback (${entry:.2f}) — policy said {dec.reason}")
        else:
            sys.exit(f"pricing rejected: {dec.reason}")

    plan = dsk.plan_long(
        entry,
        equity=float(equity or 100_000),
        risk_pct=args.risk_pct,
        stop_pct=args.stop_pct,
        reward_r=args.reward_r,
        max_notional=args.max_notional,
    )
    if plan is None:
        sys.exit("plan_long returned None — stop/risk/size invalid")

    print("\n── plan ─────────────────────────────────")
    print(f"  entry   ${plan.entry:.2f}")
    print(f"  stop    ${plan.stop:.2f}   (−{args.stop_pct}% / R=${plan.r_per_share:.4f})")
    print(f"  target  ${plan.target:.2f}  (+{args.reward_r}R)")
    print(f"  qty     {plan.qty} sh")
    print(f"  notional ${plan.notional:.2f}  risk≈${plan.risk_dollars:.2f}")
    print(f"  pricing {pricing.get('style')}")

    if not args.submit:
        print("\n(dry-run) pass --submit to place PAPER bracket order")
        return 0

    print("\n── submit paper limit+bracket ───────────")
    out = tr.buy_limit_bracket(
        symbol, plan.qty, plan.entry, plan.stop, plan.target,
    )
    print(out)
    if not out.get("ok"):
        # Fallback plain limit for diagnostics
        print("bracket failed — trying plain limit (no TP/SL)…")
        plain = tr.buy_limit_at_price(
            symbol, plan.entry, dollar_amount=plan.notional, note="smoke_plain",
        )
        print(plain)
        return 1 if not plain.get("ok") else 0

    oid = out.get("buy_order_id")
    print(f"✓ paper order id={oid}")

    if args.cancel_after and args.cancel_after > 0:
        print(f"waiting {args.cancel_after:.0f}s then cancel open orders for {symbol}…")
        time.sleep(args.cancel_after)
        try:
            for o in tr.get_open_orders() or []:
                if str(o.get("symbol") or "").upper() != symbol:
                    continue
                oid2 = o.get("id")
                if not oid2:
                    continue
                try:
                    tr._client.cancel_order_by_id(oid2)
                    print(f"  canceled {oid2} ({o.get('side')} {o.get('type')})")
                except Exception as e:
                    print(f"  cancel {oid2}: {e}")
        except Exception as e:
            print(f"  cancel sweep failed: {e}")

    print("\nCheck Alpaca paper UI for the bracket (entry + TP + SL).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
