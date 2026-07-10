"""Real Webull OpenAPI providers (pip install webull-openapi-python-sdk).

Market data: polls GET quotes with depth=10 (Level 2 requires the separate
*OpenAPI Advanced Quotes* subscription — the L2 bought in the Webull app
does NOT apply). Broker: trade_client v2 order API.

Activated by config/webull_bridge.json {"provider": "webull"} plus
WEBULL_APP_KEY / WEBULL_APP_SECRET (env or webull_app_key/secret keys).
Run scripts/webull_smoke.py first to verify credentials read-only.

All SDK calls are blocking HTTP, so they run in the default executor.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import AsyncIterator, Optional

from webull_bridge.l2 import L2Book
from .base import Account, BrokerProvider, MarketDataProvider, Order, Position

log = logging.getLogger(__name__)


def _require_sdk():
    try:
        from webull.core.client import ApiClient  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "webull provider needs the SDK: pip install webull-openapi-python-sdk"
        ) from e


def _make_api_client(cfg: dict):
    from webull.core.client import ApiClient
    key = cfg.get("webull_app_key", "")
    secret = cfg.get("webull_app_secret", "")
    if not key or not secret:
        raise RuntimeError(
            "webull provider needs WEBULL_APP_KEY / WEBULL_APP_SECRET "
            "(env vars or webull_app_key/webull_app_secret in "
            "config/webull_bridge.json)")
    return ApiClient(key, secret, cfg.get("webull_region", "us"))


def _f(v, default=0.0) -> float:
    """Tolerant float: Webull returns numbers as strings, may include commas."""
    if v is None:
        return default
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _first(d: dict, *keys, default=None):
    """First present key — Webull response field names vary by endpoint
    version, so probe the likely spellings."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def parse_depth_payload(payload: dict, ts: float | None = None) -> Optional[L2Book]:
    """Webull depth-quote JSON -> L2Book. Accepts asks/bids or askList/bidList,
    entries as {price, size} dicts (values may be strings)."""
    if not isinstance(payload, dict):
        return None
    asks_raw = _first(payload, "asks", "askList", "ask", default=[])
    bids_raw = _first(payload, "bids", "bidList", "bid", default=[])

    def levels(raw):
        out = []
        for lvl in raw or []:
            price = _f(_first(lvl, "price", "p"), -1)
            size = _f(_first(lvl, "size", "volume", "v"), -1)
            if price > 0 and size >= 0:
                out.append((price, size))
        return out

    bids, asks = levels(bids_raw), levels(asks_raw)
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    n = min(len(bids), len(asks))
    if n < 1 or bids[0][0] >= asks[0][0]:
        return None
    return L2Book(bids[:n], asks[:n], ts=ts or time.time())


class WebullMarketData(MarketDataProvider):
    """Depth via GET quotes polling (depth=10). Poll rate is
    webull_poll_sec (default 0.5s ≈ 2 reads/sec — same regime the OCR
    monitor ran at)."""

    def __init__(self, cfg: dict):
        _require_sdk()
        from webull.data.data_client import DataClient
        self.cfg = cfg
        self.client = DataClient(_make_api_client(cfg))
        self.poll = float(cfg.get("webull_poll_sec", 0.5))
        self.max_rps = float(cfg.get("webull_max_rps", 6.0))
        self.depth = int(cfg.get("webull_depth_levels", 10))
        self.category = cfg.get("webull_category", "US_STOCK")
        self._last: dict[str, L2Book] = {}
        self._active = 0   # live subscribe_depth generators

    def _interval(self) -> float:
        """Per-symbol poll interval, stretched so the combined request
        rate across all active engines stays under webull_max_rps."""
        return max(self.poll, max(self._active, 1) / self.max_rps)

    def _fetch(self, symbol: str) -> Optional[L2Book]:
        try:
            res = self.client.market_data.get_quotes(
                symbol, self.category, depth=self.depth)
            if res.status_code != 200:
                log.warning("[WEBULL] get_quotes %s -> HTTP %s: %s",
                            symbol, res.status_code, res.text[:200])
                return None
            book = parse_depth_payload(res.json())
            if book:
                self._last[symbol] = book
            return book
        except Exception:
            log.exception("[WEBULL] get_quotes %s failed", symbol)
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
                    # back off while the API is unhappy (rate limit, closed
                    # market, bad symbol) instead of hammering it
                    misses += 1
                    if misses in (5, 50) or misses % 200 == 0:
                        log.warning("[WEBULL] %s: %d consecutive empty reads",
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


class WebullBroker(BrokerProvider):
    """Order/account access via trade_client v2 (equities, US market).

    Order dicts follow samples/trade/trade_client_v2.py. We use our
    client_order_id as the order id everywhere so cancel works without a
    second lookup.
    """

    def __init__(self, cfg: dict):
        _require_sdk()
        from webull.trade.trade_client import TradeClient
        self.cfg = cfg
        self.client = TradeClient(_make_api_client(cfg))
        self._account_id: str = str(cfg.get("webull_account_id", "") or "")
        self._lock = asyncio.Lock()

    # ── account discovery ────────────────────────────────────────────

    def _account(self) -> str:
        if self._account_id:
            return self._account_id
        res = self.client.account_v2.get_account_list()
        res.raise_for_status()
        data = res.json()
        accounts = data if isinstance(data, list) else \
            _first(data, "data", "accounts", default=[])
        if not accounts:
            raise RuntimeError(f"no Webull accounts visible to this app key: {data}")
        self._account_id = str(_first(accounts[0], "account_id", "accountId",
                                      "secAccountId", default=""))
        if not self._account_id:
            raise RuntimeError(f"couldn't find account id in {accounts[0]}")
        log.info("[WEBULL] using account %s", self._account_id)
        return self._account_id

    # ── broker interface (each wraps the blocking SDK in the executor) ──

    async def place_order(self, symbol: str, side: str, qty: float,
                          order_type: str = "MARKET",
                          limit_price: Optional[float] = None) -> Order:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._place_order_sync, symbol, side, qty, order_type,
            limit_price)

    def _place_order_sync(self, symbol, side, qty, order_type, limit_price):
        account_id = self._account()
        client_order_id = uuid.uuid4().hex
        payload = {
            "combo_type": "NORMAL",
            "client_order_id": client_order_id,
            "symbol": symbol.upper(),
            "instrument_type": "EQUITY",
            "market": "US",
            "order_type": order_type,
            "quantity": str(int(qty)),
            "support_trading_session": "N",   # regular hours only
            "side": side,
            "time_in_force": "DAY",
            "entrust_type": "QTY",
        }
        if order_type == "LIMIT":
            payload["limit_price"] = f"{limit_price:.2f}"
        order = Order(id=client_order_id, symbol=symbol.upper(), side=side,
                      qty=qty, order_type=order_type, limit_price=limit_price)
        res = self.client.order_v2.place_order(account_id, [payload])
        body = res.json() if res.status_code == 200 else {}
        if res.status_code != 200:
            order.status = "REJECTED"
            log.error("[WEBULL] place_order failed HTTP %s: %s",
                      res.status_code, res.text[:300])
        else:
            order.status = "PENDING"
            log.info("[WEBULL] placed %s: %s", client_order_id, body)
        return order

    async def cancel_order(self, order_id: str) -> bool:
        loop = asyncio.get_running_loop()

        def _cancel():
            res = self.client.order_v2.cancel_order(self._account(), order_id)
            return res.status_code == 200

        return await loop.run_in_executor(None, _cancel)

    async def orders(self) -> list[Order]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._orders_sync)

    def _orders_sync(self) -> list[Order]:
        account_id = self._account()
        out = []
        for res in (self.client.order_v2.get_order_open(account_id=account_id),
                    self.client.order_v2.get_order_history(account_id)):
            if res.status_code != 200:
                continue
            data = res.json()
            rows = data if isinstance(data, list) else \
                _first(data, "data", "orders", "items", default=[])
            for row in rows or []:
                o = self._map_order(row)
                if o and all(x.id != o.id for x in out):
                    out.append(o)
        return out

    @staticmethod
    def _map_order(row: dict) -> Optional[Order]:
        # order rows may nest the instrument info under "items"
        if isinstance(row.get("items"), list) and row["items"]:
            leg = row["items"][0]
        else:
            leg = row
        cid = str(_first(row, "client_order_id", "clientOrderId", default="")
                  or _first(leg, "client_order_id", "clientOrderId", default=""))
        symbol = str(_first(leg, "symbol", "ticker", default=""))
        if not cid or not symbol:
            return None
        status_raw = str(_first(row, "order_status", "status", "orderStatus",
                                default="")).upper()
        status = ("FILLED" if "FILL" in status_raw else
                  "CANCELLED" if "CANCEL" in status_raw else
                  "REJECTED" if ("REJECT" in status_raw or "FAIL" in status_raw)
                  else "PENDING")
        return Order(
            id=cid, symbol=symbol.upper(),
            side=str(_first(leg, "side", default="BUY")).upper(),
            qty=_f(_first(leg, "quantity", "qty")),
            order_type=str(_first(leg, "order_type", "orderType",
                                  default="LIMIT")).upper(),
            limit_price=(_f(_first(leg, "limit_price", "limitPrice"), -1)
                         or None if _f(_first(leg, "limit_price", "limitPrice"), -1) > 0
                         else None),
            status=status,
            filled_qty=_f(_first(row, "filled_quantity", "filledQuantity",
                                 "filled_qty")),
            avg_price=(_f(_first(row, "avg_fill_price", "avgFilledPrice",
                                 "filled_price"), -1) or None
                       if _f(_first(row, "avg_fill_price", "avgFilledPrice",
                                    "filled_price"), -1) > 0 else None),
            created=_f(_first(row, "create_time", "createTime"),
                       time.time()),
        )

    async def positions(self) -> list[Position]:
        loop = asyncio.get_running_loop()

        def _pos():
            res = self.client.account_v2.get_account_position(self._account())
            if res.status_code != 200:
                return []
            data = res.json()
            rows = data if isinstance(data, list) else \
                _first(data, "data", "positions", "holdings", "items",
                       default=[])
            out = []
            for row in rows or []:
                symbol = str(_first(row, "symbol", "ticker", default=""))
                qty = _f(_first(row, "quantity", "qty", "position"))
                if not symbol or qty == 0:
                    continue
                out.append(Position(
                    symbol=symbol.upper(), qty=qty,
                    avg_price=_f(_first(row, "cost_price", "avg_cost",
                                        "costPrice", "avgCost")),
                    last_price=_f(_first(row, "last_price", "lastPrice",
                                         "market_price"), -1) or None,
                ))
            return out

        return await loop.run_in_executor(None, _pos)

    async def account(self) -> Account:
        loop = asyncio.get_running_loop()

        def _acct():
            account_id = self._account()
            res = self.client.account_v2.get_account_balance(account_id)
            data = res.json() if res.status_code == 200 else {}
            # balances may sit at the top level or nested per-currency
            node = data if isinstance(data, dict) else {}
            if isinstance(_first(node, "account_currency_assets",
                                 "currency_assets"), list):
                assets = _first(node, "account_currency_assets",
                                "currency_assets")
                usd = next((a for a in assets
                            if str(a.get("currency", "")).upper() == "USD"),
                           assets[0] if assets else {})
                node = {**node, **usd}
            return Account(
                account_id=account_id,
                cash=_f(_first(node, "cash_balance", "cashBalance",
                               "available_cash", "cash")),
                buying_power=_f(_first(node, "buying_power", "buyingPower",
                                       "day_buying_power", "available_funds")),
                currency="USD", paper=False)

        return await loop.run_in_executor(None, _acct)
