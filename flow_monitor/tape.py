"""Rolling trade tape from Alpaca prints — sides for the confidence pillar."""
from __future__ import annotations

import time
from collections import deque
from typing import Deque, Optional, Tuple


class TapeWindow:
    """60s (configurable) buy/sell volume from sided prints."""

    def __init__(self, window_sec: float = 60.0):
        self.window = float(window_sec)
        # (ts, price, size, side) side in {"B","S",""}
        self._prints: Deque[Tuple[float, float, float, str]] = deque()
        self._last_px: Optional[float] = None

    def side_print(self, price: float, size: float, bid: float, ask: float,
                   ts: float | None = None) -> str:
        """Quote rule then tick rule. Returns B / S / ''."""
        ts = time.time() if ts is None else ts
        side = ""
        if bid > 0 and ask > 0 and bid < ask:
            if price >= ask:
                side = "B"
            elif price <= bid:
                side = "S"
        if not side and self._last_px is not None:
            if price > self._last_px:
                side = "B"
            elif price < self._last_px:
                side = "S"
        self._last_px = price
        if size and size > 0 and price > 0:
            self._prints.append((ts, price, float(size), side))
            self._trim(ts)
        return side

    def _trim(self, now: float):
        cutoff = now - self.window
        while self._prints and self._prints[0][0] < cutoff:
            self._prints.popleft()

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._trim(now)
        buy = sell = unsided = 0.0
        sided_n = 0
        for _, _, sz, side in self._prints:
            if side == "B":
                buy += sz
                sided_n += 1
            elif side == "S":
                sell += sz
                sided_n += 1
            else:
                unsided += sz
        total = buy + sell + unsided
        sided = buy + sell
        dom = ((buy - sell) / sided) if sided > 0 else 0.0
        return {
            "buy": buy,
            "sell": sell,
            "unsided": unsided,
            "total": total,
            "sided_n": sided_n,
            "dom": dom,
            "n": len(self._prints),
            "span": (self._prints[-1][0] - self._prints[0][0]
                     if len(self._prints) >= 2 else 0.0),
        }
