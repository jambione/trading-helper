#!/usr/bin/env python3
"""Does Finnhub carry a bid/ask the desk could price a premarket spread with?

§5G moved the blocker from economics to the live quote: the premarket book
costs what the RTH book costs, but the desk cannot see it, because IEX
barely quotes before 09:30 and SIP is delayed-only on this account.

Finnhub already holds a websocket in signal_engine.py, so it is the first
candidate. But its stream handles type=="trade" and its REST /quote returns
last/open/high/low/prevclose — neither is a book. This checks what is
actually entitled, live, rather than assuming:

  /quote          last trade    (known; confirms the key works)
  /stock/bidask   the NBBO      (premium on most plans — the question)

and prints Alpaca IEX beside it for the same instant, so the gap is visible.

Never prints the key. Read-only.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, ".")
sys.path.insert(0, "tools")

BASE = "https://finnhub.io/api/v1"


def key() -> str:
    try:
        import finnhub_stream as fs
        k = fs.engine_finnhub_key()
        if k:
            return k
    except Exception:  # noqa: BLE001
        pass
    try:
        return json.load(open("config/secrets.json"))["finnhub_key"]
    except Exception:  # noqa: BLE001
        return ""


def get(path: str, k: str, **params) -> tuple[int, object]:
    q = "&".join(f"{a}={b}" for a, b in params.items())
    url = f"{BASE}/{path}?{q}&token={k}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:  # noqa: BLE001
            pass
        return e.code, body
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def watchlist() -> list[str]:
    for p in ("ai_reports/ai_watch.json", "data/ai_watch.json"):
        if os.path.exists(p):
            try:
                d = json.load(open(p))
                syms = list(d.keys()) if isinstance(d, dict) else []
                if syms:
                    return syms[:4]
            except Exception:  # noqa: BLE001
                pass
    return []


k = key()
print("finnhub key:", "present" if k else "MISSING")
if not k:
    raise SystemExit(1)

syms = watchlist() or ["AAPL"]
print("symbols (live watchlist):", ", ".join(syms))
print()

st, body = get("stock/bidask", k, symbol=syms[0])
print(f"/stock/bidask  ->  HTTP {st}")
print(f"   {json.dumps(body)[:300] if not isinstance(body, str) else body[:300]}")
entitled = st == 200 and isinstance(body, dict) and body.get("a") and body.get("b")
print(f"   NBBO entitled: {'YES' if entitled else 'NO'}")
print()

st, body = get("quote", k, symbol=syms[0])
print(f"/quote         ->  HTTP {st}")
if isinstance(body, dict):
    print(f"   fields: {sorted(body.keys())}")
    print(f"   {json.dumps(body)[:200]}")
    print("   carries bid/ask:",
          "YES" if {"a", "b"} <= set(body) else "NO — last trade only")
print()

# Alpaca IEX beside it, same instant, so the gap is concrete.
try:
    import bars
    cl = bars.client()
    if cl:
        from alpaca.data.requests import StockLatestQuoteRequest
        from alpaca.data.enums import DataFeed
        print(f"{'symbol':<8}{'FH last':>10}{'IEX bid':>10}{'IEX ask':>10}"
              f"{'IEX wide':>10}{'age':>8}")
        print("-" * 56)
        for s in syms:
            _, fq = get("quote", k, symbol=s)
            last = fq.get("c") if isinstance(fq, dict) else None
            try:
                q = cl.get_stock_latest_quote(StockLatestQuoteRequest(
                    symbol_or_symbols=s, feed=DataFeed.IEX))[s]
                bid, ask = float(q.bid_price), float(q.ask_price)
                age = time.time() - q.timestamp.timestamp()
                wide = (ask - bid) / ask * 100 if ask > 0 else 0
                print(f"{s:<8}{last or 0:>10.2f}{bid:>10.2f}{ask:>10.2f}"
                      f"{wide:>9.2f}%{age:>7.0f}s")
            except Exception as e:  # noqa: BLE001
                print(f"{s:<8}{last or 0:>10.2f}   IEX quote failed: "
                      f"{type(e).__name__}")
except Exception as e:  # noqa: BLE001
    print("alpaca comparison unavailable:", type(e).__name__, e)
