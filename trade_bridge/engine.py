"""Per-symbol signal pipeline + the manager that runs one per watched symbol.

SymbolEngine is synchronous and side-effect free (book in, state dict out)
so tests can drive it with scripted books. BridgeManager owns the asyncio
tasks, fans state out to WebSocket subscribers, and keeps the last state
for REST reads.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from trade_bridge.l2 import (L2Book, LongView, SignalEngine, WallTracker,
                              market_bias, playbook, project_price)
from trade_bridge.providers import build_providers

log = logging.getLogger(__name__)


class SymbolEngine:
    """Per-symbol stance pipeline for Mobile Trader (NBBO / depth-1 books)."""

    def __init__(self, symbol: str, cfg: dict):
        self.symbol = symbol.upper()
        self.cfg = cfg
        self.engine = SignalEngine(cfg)
        # WallTracker kept only for API stability; never drives stance at depth=1
        self.walls = WallTracker(cfg.get("wall_multiple", 4.0))
        self.longview = LongView(cfg)
        self.recent_events: list = []

    def on_book(self, book: L2Book, now: Optional[float] = None) -> dict:
        """Update stance from a book snapshot.

        Multi-level walls are not claimed (NBBO cannot support them).
        `walls` is always []. Touch skew is book.imbalance / touch_skew.
        """
        now = now if now is not None else book.ts
        sig = self.engine.update(book)
        events: list = []
        live_events: list = []

        t1 = self.engine.trend_pct(60)
        t5 = self.engine.trend_pct(self.cfg.get("trend_window", 300))
        lv = self.longview.update(book, t5, events, now)
        score, label, why = market_bias(book, t1, t5)
        pb = playbook(book, t1, t5, live_events)

        proj = None
        pp = project_price(self.engine.history,
                           self.cfg.get("projection_minutes", 5.0))
        if pp:
            mid, target = pp
            proj = {"mid": round(mid, 4), "target": round(target, 4),
                    "note": "pace estimate (NBBO only)"}

        mids = [round((b.best_bid + b.best_ask) / 2, 4)
                for b in list(self.engine.history)[-40:]]

        return {
            "symbol": self.symbol,
            "ts": now,
            "stance": {
                "stance": lv["stance"],
                "held": round(lv["held"], 1),
                "pending": lv["pending"],
                "pending_left": round(lv["pending_left"], 1),
                "med_imb": round(lv["med_imb"], 2),
                "t5": round(lv["t5"], 3) if lv["t5"] is not None else None,
                "ask_breaks": 0,
                "bid_cracks": 0,
            },
            "bias": {"score": round(score, 1), "label": label, "reason": why},
            "playbook": {
                "verdict": pb["verdict"],
                "trend_up": pb["trend_up"], "trend_dn": pb["trend_dn"],
                "imb_up": pb["imb_up"], "imb_dn": pb["imb_dn"],
                "ask_break": [], "bid_crack": [],
            },
            "book": {
                "bids": [[p, int(s)] for p, s in book.bids],
                "asks": [[p, int(s)] for p, s in book.asks],
                "best_bid": round(book.best_bid, 4),
                "best_ask": round(book.best_ask, 4),
                "spread": round(book.spread, 4),
                "spread_pct": round(book.spread_pct, 3),
                "bid_size": int(book.bid_size),
                "ask_size": int(book.ask_size),
                "imbalance": round(book.imbalance, 2),
                "touch_skew": round(book.imbalance, 2),
            },
            "walls": [],
            "trend1": round(t1, 3) if t1 is not None else None,
            "trend5": round(t5, 3) if t5 is not None else None,
            "projection": proj,
            "mids": mids,
            "signal": ({"action": sig.action, "reason": sig.reason,
                        "price": round(sig.price, 4)} if sig else None),
        }


class BridgeManager:
    """Owns one engine task per watched symbol and the provider pair."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.market_data, self.broker = build_providers(cfg)
        self._tasks: dict[str, asyncio.Task] = {}
        self._state: dict[str, dict] = {}
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._auto_task: asyncio.Task | None = None

    # ── auto-watch: keep an engine running for every momentum ticker ──

    def start_auto_watch(self, get_symbols):
        """Sync the engine set to `get_symbols()` (the dashboard's momentum
        ticker list) so every symbol has a live stance before it's opened.
        Symbols a client is actively streaming are never auto-unwatched."""
        if not self.cfg.get("auto_watch", True) or self._auto_task:
            return
        self._auto_task = asyncio.create_task(
            self._auto_watch_loop(get_symbols), name="l2-auto-watch")

    def sync_symbols(self, symbols: list[str]):
        """One sync pass — factored out of the loop for testability."""
        limit = int(self.cfg.get("max_engines", 8))
        target = [s.upper() for s in symbols][:limit]
        # keep anything a client is currently streaming
        keep = set(target) | {s for s, subs in self._subs.items() if subs}
        for symbol in list(self._tasks):
            if symbol not in keep:
                self.unwatch(symbol)
        for symbol in target:
            if len(self._tasks) >= limit and symbol not in self._tasks:
                continue
            self.watch(symbol)

    async def _auto_watch_loop(self, get_symbols):
        interval = float(self.cfg.get("auto_watch_interval", 15))
        loop = asyncio.get_running_loop()
        while True:
            try:
                symbols = await loop.run_in_executor(None, get_symbols)
                self.sync_symbols(list(symbols or []))
            except Exception:
                log.exception("[L2] auto-watch sync failed")
            await asyncio.sleep(interval)

    # ── watch lifecycle ──────────────────────────────────────────────

    def watching(self) -> list[str]:
        return sorted(self._tasks)

    def watch(self, symbol: str) -> bool:
        symbol = symbol.upper()
        if symbol in self._tasks:
            return True
        if len(self._tasks) >= int(self.cfg.get("max_engines", 8)):
            return False
        self._tasks[symbol] = asyncio.create_task(self._run(symbol),
                                                  name=f"l2-{symbol}")
        return True

    def unwatch(self, symbol: str):
        symbol = symbol.upper()
        task = self._tasks.pop(symbol, None)
        if task:
            task.cancel()
        self._state.pop(symbol, None)
        for q in self._subs.pop(symbol, set()):
            q.put_nowait(None)   # tell WS handlers to close

    def state(self, symbol: str) -> Optional[dict]:
        return self._state.get(symbol.upper())

    def subscribe(self, symbol: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subs.setdefault(symbol.upper(), set()).add(q)
        return q

    def unsubscribe(self, symbol: str, q: asyncio.Queue):
        self._subs.get(symbol.upper(), set()).discard(q)

    async def shutdown(self):
        if self._auto_task:
            self._auto_task.cancel()
            self._auto_task = None
        for symbol in list(self._tasks):
            self.unwatch(symbol)

    # ── the per-symbol loop ─────────────────────────────────────────

    async def _run(self, symbol: str):
        engine = SymbolEngine(symbol, self.cfg)
        try:
            async for book in self.market_data.subscribe_depth(symbol):
                state = engine.on_book(book)
                self._state[symbol] = state
                for q in list(self._subs.get(symbol, ())):
                    try:
                        q.put_nowait(state)
                    except asyncio.QueueFull:
                        # slow client: drop the oldest tick, keep the newest
                        try:
                            q.get_nowait()
                            q.put_nowait(state)
                        except (asyncio.QueueEmpty, asyncio.QueueFull):
                            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[L2] engine for %s crashed", symbol)
            self._tasks.pop(symbol, None)
