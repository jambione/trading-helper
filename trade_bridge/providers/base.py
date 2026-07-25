"""Provider interfaces: market data (L2 depth) and broker (orders/positions).

Both the mock simulator and the real Alpaca implementation conform
to these, so the engine and routes never care which one is running.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from trade_bridge.l2 import L2Book


@dataclass
class Order:
    id: str
    symbol: str
    side: str                 # "BUY" | "SELL"
    qty: float
    order_type: str           # "MARKET" | "LIMIT"
    limit_price: Optional[float] = None
    status: str = "PENDING"   # PENDING | FILLED | CANCELLED | REJECTED
    filled_qty: float = 0.0
    avg_price: Optional[float] = None
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "side": self.side,
            "qty": self.qty, "order_type": self.order_type,
            "limit_price": self.limit_price, "status": self.status,
            "filled_qty": self.filled_qty, "avg_price": self.avg_price,
            "created": self.created,
        }


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    last_price: Optional[float] = None

    def to_dict(self) -> dict:
        upnl_pct = None
        if self.last_price and self.avg_price:
            upnl_pct = round(100.0 * (self.last_price - self.avg_price)
                             / self.avg_price, 2)
        return {"symbol": self.symbol, "qty": self.qty,
                "avg_price": self.avg_price, "last_price": self.last_price,
                "upnl_pct": upnl_pct}


@dataclass
class Account:
    account_id: str
    cash: float
    buying_power: float
    currency: str = "USD"
    paper: bool = True

    def to_dict(self) -> dict:
        return {"account_id": self.account_id, "cash": round(self.cash, 2),
                "buying_power": round(self.buying_power, 2),
                "currency": self.currency, "paper": self.paper}


class MarketDataProvider(ABC):
    @abstractmethod
    def subscribe_depth(self, symbol: str) -> AsyncIterator[L2Book]:
        """Yield L2Book snapshots for `symbol` until the consumer stops."""

    @abstractmethod
    async def snapshot(self, symbol: str) -> Optional[L2Book]:
        """One-shot current depth snapshot (None if unavailable)."""


class BrokerProvider(ABC):
    @abstractmethod
    async def place_order(self, symbol: str, side: str, qty: float,
                          order_type: str = "MARKET",
                          limit_price: Optional[float] = None) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    async def orders(self) -> list[Order]: ...

    @abstractmethod
    async def positions(self) -> list[Position]: ...

    @abstractmethod
    async def account(self) -> Account: ...
