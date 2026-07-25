"""Mock providers — a depth simulator and a paper broker.

The simulator produces books that exercise every part of the signal stack:
regimes (bull/bear/chop) drive both price drift and book imbalance so the
LongView stance actually transitions, and walls spawn/collapse so
WallTracker events fire. The paper broker fills against the simulated book.
"""
from __future__ import annotations

import asyncio
import itertools
import random
import time
from typing import AsyncIterator, Optional

from trade_bridge.l2 import L2Book
from .base import Account, BrokerProvider, MarketDataProvider, Order, Position

LEVELS = 10

# regime -> (price drift %/sec, target bid/ask imbalance)
_REGIMES = {
    "bull": (+0.0025, 2.2),
    "bear": (-0.0025, 0.45),
    "chop": (0.0, 1.0),
}


class _SymbolSim:
    """Random-walk L2 book for one symbol."""

    def __init__(self, symbol: str, rng: random.Random | None = None):
        self.symbol = symbol
        self.rng = rng or random.Random(hash(symbol) & 0xFFFFFFFF)
        self.mid = self.rng.uniform(2.0, 30.0)
        self.tick = 0.01
        self.base_size = self.rng.uniform(300, 1500)
        self.regime = "chop"
        self.regime_until = 0.0
        self.imb = 1.0
        # active wall: (side, level_index, size_mult, expires_ts) or None
        self.wall = None
        self.last_ts = 0.0
        self.last_book: Optional[L2Book] = None

    def _maybe_switch_regime(self, now: float):
        if now < self.regime_until:
            return
        self.regime = self.rng.choice(list(_REGIMES))
        # long enough for LongView (60s window + 20s confirm) to lock on
        self.regime_until = now + self.rng.uniform(45, 150)

    def step(self, now: float) -> L2Book:
        dt = min(now - self.last_ts, 2.0) if self.last_ts else 0.35
        self.last_ts = now
        self._maybe_switch_regime(now)
        drift, imb_target = _REGIMES[self.regime]

        # price random-walk with regime drift
        noise = self.rng.gauss(0.0, 0.0015)
        self.mid = max(0.5, self.mid * (1 + (drift / 100.0) * dt + noise * 0.1))

        # imbalance eases toward the regime target with noise
        self.imb += (imb_target - self.imb) * 0.1 + self.rng.gauss(0, 0.05)
        self.imb = max(0.2, min(5.0, self.imb))

        # walls: spawn occasionally, collapse near expiry, then vanish
        if self.wall is None and self.rng.random() < 0.008:
            side = "ASK" if self.regime != "bull" else self.rng.choice(["BID", "ASK"])
            self.wall = [side, self.rng.randint(1, 6),
                         self.rng.uniform(5.0, 8.0), now + self.rng.uniform(20, 60)]
        elif self.wall is not None:
            if now > self.wall[3]:
                self.wall = None                       # gone
            elif now > self.wall[3] - 5:
                self.wall[2] = max(1.0, self.wall[2] * 0.8)  # collapsing

        spread = self.tick * self.rng.choice([1, 1, 1, 2])
        best_bid = round(self.mid - spread / 2, 2)
        best_ask = round(best_bid + spread, 2)

        bid_scale = self.imb ** 0.5
        ask_scale = 1.0 / bid_scale
        bids, asks = [], []
        for i in range(LEVELS):
            bsz = self.base_size * bid_scale * self.rng.lognormvariate(0, 0.4)
            asz = self.base_size * ask_scale * self.rng.lognormvariate(0, 0.4)
            if self.wall and self.wall[1] == i:
                if self.wall[0] == "BID":
                    bsz = self.base_size * bid_scale * self.wall[2]
                else:
                    asz = self.base_size * ask_scale * self.wall[2]
            bids.append((round(best_bid - i * self.tick, 2), round(bsz)))
            asks.append((round(best_ask + i * self.tick, 2), round(asz)))

        self.last_book = L2Book(bids, asks, ts=now)
        return self.last_book


class MockMarketData(MarketDataProvider):
    def __init__(self, cfg: dict):
        self.hz = float(cfg.get("stream_hz", 3.0))
        self.sims: dict[str, _SymbolSim] = {}

    def _sim(self, symbol: str) -> _SymbolSim:
        symbol = symbol.upper()
        if symbol not in self.sims:
            self.sims[symbol] = _SymbolSim(symbol)
        return self.sims[symbol]

    async def subscribe_depth(self, symbol: str) -> AsyncIterator[L2Book]:
        sim = self._sim(symbol)
        interval = 1.0 / self.hz
        while True:
            yield sim.step(time.time())
            await asyncio.sleep(interval)

    async def snapshot(self, symbol: str) -> Optional[L2Book]:
        sim = self._sim(symbol)
        return sim.last_book or sim.step(time.time())


class MockBroker(BrokerProvider):
    """Paper broker: market orders fill instantly at the simulated touch,
    limit orders fill when the book crosses them (checked lazily)."""

    def __init__(self, cfg: dict, md: MockMarketData):
        self.md = md
        self.cash = float(cfg.get("mock_cash", 25_000.0))
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()

    def _book(self, symbol: str) -> L2Book:
        sim = self.md._sim(symbol)
        return sim.last_book or sim.step(time.time())

    def _fill(self, o: Order, price: float):
        o.status, o.filled_qty, o.avg_price = "FILLED", o.qty, price
        signed = o.qty if o.side == "BUY" else -o.qty
        self.cash -= signed * price
        pos = self._positions.get(o.symbol)
        if pos is None:
            self._positions[o.symbol] = Position(o.symbol, signed, price)
        else:
            new_qty = pos.qty + signed
            if abs(new_qty) < 1e-9:
                del self._positions[o.symbol]
            else:
                # adding to a position averages in; reducing keeps the basis
                if pos.qty * signed > 0:
                    pos.avg_price = ((pos.avg_price * abs(pos.qty)
                                      + price * abs(signed)) / abs(new_qty))
                pos.qty = new_qty

    def _sweep(self):
        """Fill any pending limit orders the current book has crossed."""
        for o in self._orders.values():
            if o.status != "PENDING" or o.order_type != "LIMIT":
                continue
            book = self._book(o.symbol)
            if o.side == "BUY" and book.best_ask <= o.limit_price:
                self._fill(o, book.best_ask)
            elif o.side == "SELL" and book.best_bid >= o.limit_price:
                self._fill(o, book.best_bid)

    async def place_order(self, symbol: str, side: str, qty: float,
                          order_type: str = "MARKET",
                          limit_price: Optional[float] = None) -> Order:
        async with self._lock:
            symbol = symbol.upper()
            o = Order(id=f"MOCK-{next(self._ids)}", symbol=symbol, side=side,
                      qty=qty, order_type=order_type, limit_price=limit_price)
            self._orders[o.id] = o
            if order_type == "MARKET":
                book = self._book(symbol)
                self._fill(o, book.best_ask if side == "BUY" else book.best_bid)
            else:
                self._sweep()
            return o

    async def cancel_order(self, order_id: str) -> bool:
        async with self._lock:
            o = self._orders.get(order_id)
            if o is None or o.status != "PENDING":
                return False
            o.status = "CANCELLED"
            return True

    async def orders(self) -> list[Order]:
        async with self._lock:
            self._sweep()
            return sorted(self._orders.values(),
                          key=lambda o: o.created, reverse=True)

    async def positions(self) -> list[Position]:
        async with self._lock:
            self._sweep()
            out = []
            for pos in self._positions.values():
                pos.last_price = self._book(pos.symbol).best_bid
                out.append(pos)
            return out

    async def account(self) -> Account:
        async with self._lock:
            self._sweep()
            return Account(account_id="PAPER", cash=self.cash,
                           buying_power=max(0.0, self.cash),
                           paper=True)
