#!/usr/bin/env python3
"""Read-only Webull OpenAPI smoke test — run this FIRST after getting keys.

Prints raw responses for: account list, balance, positions, open orders,
and a depth-10 quote. Places NO orders. If any field mapping in
webull_bridge/providers/webull.py doesn't match what you see here, the
raw output tells us exactly what to fix.

Usage:
    WEBULL_APP_KEY=... WEBULL_APP_SECRET=... venv/bin/python scripts/webull_smoke.py [SYMBOL]
Keys can also live in config/webull_bridge.json (webull_app_key/_secret).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webull_bridge.config import load_config  # noqa: E402


def show(label, res):
    print(f"\n── {label} ─────────────────────────────")
    print("HTTP", res.status_code)
    try:
        print(json.dumps(res.json(), indent=2)[:3000])
    except Exception:
        print(res.text[:1000])


def main():
    cfg = load_config()
    if not cfg.get("webull_app_key") or not cfg.get("webull_app_secret"):
        sys.exit("Set WEBULL_APP_KEY / WEBULL_APP_SECRET (env) or add "
                 "webull_app_key / webull_app_secret to "
                 "config/webull_bridge.json")

    symbol = (sys.argv[1] if len(sys.argv) > 1 else "AAPL").upper()

    from webull.core.client import ApiClient
    from webull.data.data_client import DataClient
    from webull.trade.trade_client import TradeClient

    api = ApiClient(cfg["webull_app_key"], cfg["webull_app_secret"],
                    cfg.get("webull_region", "us"))
    trade = TradeClient(api)
    data = DataClient(api)

    res = trade.account_v2.get_account_list()
    show("account list", res)
    account_id = str(cfg.get("webull_account_id") or "")
    if not account_id and res.status_code == 200:
        body = res.json()
        accounts = body if isinstance(body, list) else \
            body.get("data") or body.get("accounts") or []
        if accounts:
            account_id = str(accounts[0].get("account_id")
                             or accounts[0].get("accountId") or "")
    print("\nusing account_id:", account_id or "NOT FOUND")

    if account_id:
        show("balance", trade.account_v2.get_account_balance(account_id))
        show("positions", trade.account_v2.get_account_position(account_id))
        show("open orders", trade.order_v2.get_order_open(account_id=account_id))

    show(f"snapshot {symbol}",
         data.market_data.get_snapshot(symbol, "US_STOCK"))
    res = data.market_data.get_quotes(symbol, "US_STOCK", depth=10)
    show(f"depth quotes {symbol} (needs OpenAPI Advanced Quotes sub)", res)

    if res.status_code == 200:
        from webull_bridge.providers.webull import parse_depth_payload
        book = parse_depth_payload(res.json())
        if book:
            print(f"\n✓ parsed L2Book: {len(book.bids)} levels, "
                  f"best {book.best_bid}/{book.best_ask}, "
                  f"imbalance {book.imbalance:.2f}")
        else:
            print("\n✗ depth payload did NOT parse into an L2Book — "
                  "send me the JSON above so the parser can be adjusted")


if __name__ == "__main__":
    main()
