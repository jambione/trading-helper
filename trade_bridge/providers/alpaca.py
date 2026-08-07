"""Alpaca providers for the Mobile Trader bridge (pip install alpaca-py).

Market data: latest NBBO quote → depth=1 L2Book (no multi-level depth).
Broker: TradingClient for orders / positions / account.

Activated by config/trade_bridge.json {"provider": "alpaca"} plus
ALPACA_API_KEY / ALPACA_SECRET_KEY (same env vars as alpaca_trader.py).
Optional: alpaca_paper (default true), alpaca_poll_sec, alpaca_data_feed
(IEX | SIP).

All SDK calls are blocking HTTP, so they run in the default executor —
same pattern as the mock providers (blocking SDK → executor).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import AsyncIterator, Optional

from trade_bridge.l2 import L2Book
from .base import Account, BrokerProvider, MarketDataProvider, Order, Position

log = logging.getLogger(__name__)


def _require_sdk():
    try:
        from alpaca.trading.client import TradingClient  # noqa: F401
        from alpaca.data.historical import StockHistoricalDataClient  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "alpaca provider needs the SDK: pip install alpaca-py"
        ) from e


def _credentials(cfg: dict) -> tuple[str, str]:
    key = (cfg.get("alpaca_api_key")
           or os.getenv("ALPACA_API_KEY", "")
           or "").strip()
    secret = (cfg.get("alpaca_secret_key")
              or os.getenv("ALPACA_SECRET_KEY", "")
              or "").strip()
    if not key or not secret:
        raise RuntimeError(
            "alpaca provider needs ALPACA_API_KEY / ALPACA_SECRET_KEY "
            "(env vars or alpaca_api_key/alpaca_secret_key in "
            "config/trade_bridge.json)")
    return key, secret


def _paper(cfg: dict) -> bool:
    """Default paper=True so a config flip never accidentally hits live."""
    if "alpaca_paper" in cfg:
        return bool(cfg["alpaca_paper"])
    env = os.getenv("ALPACA_PAPER", "").strip().lower()
    if env in ("0", "false", "no", "live"):
        return False
    if env in ("1", "true", "yes", "paper"):
        return True
    return True


def feed_kw(cfg: dict | None = None) -> dict:
    """Public helper: always IEX (free tier). SIP not used without a paid plan."""
    return _feed_kw(cfg or {})


def _feed_kw(cfg: dict) -> dict:
    try:
        from alpaca.data.enums import DataFeed
        # Free accounts only get IEX real-time. Do not request SIP.
        return {"feed": DataFeed.IEX}
    except Exception:
        return {}


def quote_to_book(quote, ts: float | None = None) -> Optional[L2Book]:
    """Alpaca latest-quote object/dict → depth-1 L2Book.

    Accepts SDK Quote models (bid_price/ask_price/…) or plain dicts.
    Returns None when prices are missing or the book is crossed/locked.
    """
    if quote is None:
        return None

    def g(*names, default=None):
        if isinstance(quote, dict):
            for n in names:
                if quote.get(n) is not None:
                    return quote[n]
            return default
        for n in names:
            v = getattr(quote, n, None)
            if v is not None:
                return v
        return default

    try:
        bid = float(g("bid_price", "bp", default=0) or 0)
        ask = float(g("ask_price", "ap", default=0) or 0)
        bid_sz = float(g("bid_size", "bs", default=0) or 0)
        ask_sz = float(g("ask_size", "as", default=0) or 0)
    except (TypeError, ValueError):
        return None

    if bid <= 0 or ask <= 0 or bid >= ask:
        return None
    # size can be zero outside RTH; still a valid top-of-book for mid/spread
    bid_sz = max(bid_sz, 0.0)
    ask_sz = max(ask_sz, 0.0)
    return L2Book([(bid, bid_sz)], [(ask, ask_sz)], ts=ts or time.time())


def _map_order_status(status) -> str:
    s = str(status).lower().split(".")[-1]  # OrderStatus.FILLED -> filled
    if s in ("filled",):
        return "FILLED"
    if s in ("canceled", "cancelled", "expired"):
        return "CANCELLED"
    if s in ("rejected", "suspended"):
        return "REJECTED"
    # new, accepted, pending_new, partially_filled, held, …
    return "PENDING"


def _map_order(o) -> Order:
    side = str(getattr(o, "side", "buy")).upper().split(".")[-1]
    otype = str(getattr(o, "type", getattr(o, "order_type", "market")))
    otype = otype.upper().split(".")[-1]
    if otype == "STOP_LIMIT":
        otype = "LIMIT"  # bridge only exposes MARKET | LIMIT
    elif otype not in ("MARKET", "LIMIT"):
        otype = "MARKET" if "MARKET" in otype else "LIMIT"

    limit = getattr(o, "limit_price", None)
    try:
        limit = float(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None

    filled_qty = getattr(o, "filled_qty", None) or 0
    avg = getattr(o, "filled_avg_price", None)
    try:
        avg = float(avg) if avg is not None else None
    except (TypeError, ValueError):
        avg = None

    qty = getattr(o, "qty", None)
    if qty is None:
        # notional-only orders may have no qty yet
        qty = filled_qty or 0
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        qty = 0.0

    created = time.time()
    submitted = getattr(o, "submitted_at", None) or getattr(o, "created_at", None)
    if submitted is not None:
        try:
            created = submitted.timestamp()
        except Exception:
            pass

    return Order(
        id=str(o.id),
        symbol=str(o.symbol).upper(),
        side=side if side in ("BUY", "SELL") else "BUY",
        qty=qty,
        order_type=otype if otype in ("MARKET", "LIMIT") else "MARKET",
        limit_price=limit,
        status=_map_order_status(getattr(o, "status", "")),
        filled_qty=float(filled_qty or 0),
        avg_price=avg,
        created=created,
    )


class AlpacaMarketData(MarketDataProvider):
    """Top-of-book via StockLatestQuoteRequest polling.

    Alpaca standard plans do not expose multi-level depth; this always
    yields depth=1 books only.
    """

    def __init__(self, cfg: dict):
        _require_sdk()
        from alpaca.data.historical import StockHistoricalDataClient
        key, secret = _credentials(cfg)
        self.cfg = cfg
        self.client = StockHistoricalDataClient(key, secret)
        self.poll = float(cfg.get("alpaca_poll_sec", 0.5))
        self.max_rps = float(cfg.get("alpaca_max_rps", 10.0))
        self._feed_kw = _feed_kw(cfg)
        self._last: dict[str, L2Book] = {}
        self._active = 0
        self._bad: dict[str, float] = {}
        self._bad_ttl = float(cfg.get("alpaca_bad_symbol_ttl", 600.0))
        # Consecutive empty (not failed) quotes per symbol. An OTC symbol is
        # not an error to Alpaca — it answers with no quote at all, forever, so
        # the error-based cooldown below never catches it and the symbol is
        # re-polled every cycle for the rest of the session. That is the shape
        # of the "too many requests" floods: unquotable names eating the
        # request budget the quotable ones need. Park them the same way, then
        # let the TTL expire so a symbol that starts quoting later recovers.
        self._empty: dict[str, int] = {}
        self._empty_limit = int(cfg.get("alpaca_empty_quote_limit", 10))

    def _interval(self) -> float:
        return max(self.poll, max(self._active, 1) / self.max_rps)

    def known_bad(self, symbol: str) -> bool:
        until = self._bad.get(symbol.upper())
        if until is None:
            return False
        if time.time() >= until:
            del self._bad[symbol.upper()]
            return False
        return True

    def _fetch(self, symbol: str) -> Optional[L2Book]:
        if self.known_bad(symbol):
            return None
        try:
            from alpaca.data.requests import StockLatestQuoteRequest
            req = StockLatestQuoteRequest(
                symbol_or_symbols=symbol,
                **self._feed_kw,
            )
            quotes = self.client.get_stock_latest_quote(req)
            # SDK returns {symbol: Quote} mapping
            if isinstance(quotes, dict):
                q = quotes.get(symbol) or quotes.get(symbol.upper())
                if q is None and len(quotes) == 1:
                    q = next(iter(quotes.values()))
            else:
                q = quotes
            book = quote_to_book(q)
            sym = symbol.upper()
            if book:
                self._last[symbol] = book
                self._empty.pop(sym, None)
            else:
                n = self._empty.get(sym, 0) + 1
                self._empty[sym] = n
                if n >= self._empty_limit:
                    self._bad[sym] = time.time() + self._bad_ttl
                    del self._empty[sym]
                    log.warning(
                        "[ALPACA] %s no quote after %d tries (OTC or unlisted) "
                        "— cooldown %.0fs", symbol, n, self._bad_ttl)
            return book
        except Exception as e:
            err = str(e)
            if any(x in err.lower() for x in (
                    "not found", "invalid symbol", "404", "no quote")):
                self._bad[symbol.upper()] = time.time() + self._bad_ttl
                log.warning("[ALPACA] %s bad symbol — cooldown %.0fs: %s",
                            symbol, self._bad_ttl, err[:120])
                return None
            log.warning("[ALPACA] latest quote %s failed: %s", symbol, err[:200])
            return None

    async def subscribe_depth(self, symbol: str) -> AsyncIterator[L2Book]:
        symbol = symbol.upper()
        loop = asyncio.get_running_loop()
        misses = 0
        self._active += 1
        try:
            while True:
                t0 = time.time()
                book = await loop.run_in_executor(None, self._fetch, symbol)
                if book is not None:
                    misses = 0
                    yield book
                else:
                    misses += 1
                    if misses in (5, 50) or misses % 200 == 0:
                        log.warning("[ALPACA] %s: %d consecutive empty quotes",
                                    symbol, misses)
                base = self._interval()
                delay = base if misses < 5 else min(base * misses, 10.0)
                await asyncio.sleep(max(0.0, delay - (time.time() - t0)))
        finally:
            self._active -= 1

    async def snapshot(self, symbol: str) -> Optional[L2Book]:
        symbol = symbol.upper()
        book = self._last.get(symbol)
        if book and time.time() - book.ts < 5:
            return book
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch, symbol)


class AlpacaBroker(BrokerProvider):
    """Order/account access via alpaca.trading.client.TradingClient.

    Qty-based place_order (bridge routes pass share qty, not notional).
    Paper vs live is cfg alpaca_paper (default True).
    """

    def __init__(self, cfg: dict):
        _require_sdk()
        from alpaca.trading.client import TradingClient
        key, secret = _credentials(cfg)
        self.cfg = cfg
        self.paper = _paper(cfg)
        self.client = TradingClient(key, secret, paper=self.paper)
        log.info("[ALPACA] broker ready  paper=%s", self.paper)

    async def place_order(self, symbol: str, side: str, qty: float,
                          order_type: str = "MARKET",
                          limit_price: Optional[float] = None) -> Order:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._place_order_sync, symbol, side, qty, order_type,
            limit_price)

    def _place_order_sync(self, symbol, side, qty, order_type, limit_price):
        from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        symbol = symbol.upper()
        side_u = str(side).upper()
        otype = str(order_type).upper()
        side_e = OrderSide.BUY if side_u == "BUY" else OrderSide.SELL

        order = Order(id="", symbol=symbol, side=side_u, qty=float(qty),
                      order_type=otype if otype in ("MARKET", "LIMIT")
                      else "MARKET",
                      limit_price=limit_price)

        try:
            if otype == "LIMIT":
                if limit_price is None or float(limit_price) <= 0:
                    order.status = "REJECTED"
                    log.error("[ALPACA] LIMIT order needs limit_price")
                    return order
                req = LimitOrderRequest(
                    symbol=symbol,
                    qty=float(qty),
                    side=side_e,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(float(limit_price), 2),
                )
            else:
                req = MarketOrderRequest(
                    symbol=symbol,
                    qty=float(qty),
                    side=side_e,
                    time_in_force=TimeInForce.DAY,
                )
            submitted = self.client.submit_order(req)
            mapped = _map_order(submitted)
            log.info("[ALPACA] placed %s %s %s qty=%s id=%s status=%s",
                     side_u, otype, symbol, qty, mapped.id, mapped.status)
            return mapped
        except Exception as e:
            order.status = "REJECTED"
            log.error("[ALPACA] place_order failed: %s", e)
            return order

    async def cancel_order(self, order_id: str) -> bool:
        loop = asyncio.get_running_loop()

        def _cancel():
            try:
                self.client.cancel_order_by_id(order_id)
                return True
            except Exception as e:
                log.warning("[ALPACA] cancel %s failed: %s", order_id, e)
                return False

        return await loop.run_in_executor(None, _cancel)

    async def orders(self) -> list[Order]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._orders_sync)

    def _orders_sync(self) -> list[Order]:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        out: list[Order] = []
        seen: set[str] = set()
        # open first, then recent closed so the UI has history
        for status in (QueryOrderStatus.OPEN, QueryOrderStatus.CLOSED):
            try:
                req = GetOrdersRequest(status=status, limit=50)
                rows = self.client.get_orders(filter=req)
            except Exception as e:
                log.warning("[ALPACA] get_orders(%s) failed: %s", status, e)
                continue
            for row in rows or []:
                o = _map_order(row)
                if o.id not in seen:
                    seen.add(o.id)
                    out.append(o)
        return out

    async def positions(self) -> list[Position]:
        loop = asyncio.get_running_loop()

        def _pos():
            try:
                rows = self.client.get_all_positions()
            except Exception as e:
                log.warning("[ALPACA] get_all_positions failed: %s", e)
                return []
            out = []
            for row in rows or []:
                try:
                    qty = float(row.qty)
                except (TypeError, ValueError):
                    continue
                if qty == 0:
                    continue
                last = getattr(row, "current_price", None)
                try:
                    last = float(last) if last is not None else None
                except (TypeError, ValueError):
                    last = None
                out.append(Position(
                    symbol=str(row.symbol).upper(),
                    qty=qty,
                    avg_price=float(row.avg_entry_price or 0),
                    last_price=last,
                ))
            return out

        return await loop.run_in_executor(None, _pos)

    async def account(self) -> Account:
        loop = asyncio.get_running_loop()

        def _acct():
            acct = self.client.get_account()
            return Account(
                account_id=str(acct.id),
                cash=float(acct.cash or 0),
                buying_power=float(acct.buying_power or 0),
                currency="USD",
                paper=self.paper or str(
                    getattr(acct, "account_number", "")).startswith("PA"),
            )

        return await loop.run_in_executor(None, _acct)
