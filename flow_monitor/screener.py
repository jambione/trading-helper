"""Alpaca stock screener — top movers / most-actives (replaces Webull OCR movers)."""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)


class MoversCache:
    def __init__(self, api_key: str, secret_key: str, ttl: float = 45.0):
        self.api_key = api_key
        self.secret_key = secret_key
        self.ttl = ttl
        self._rows: list[dict] = []
        self._ts = 0.0
        self._err: Optional[str] = None

    def get(self, top: int = 8) -> list[dict]:
        now = time.time()
        if now - self._ts > self.ttl:
            self._refresh()
        return self._rows[:top]

    def _refresh(self) -> None:
        try:
            from alpaca.data.historical.screener import ScreenerClient
            from alpaca.data.requests import MarketMoversRequest
            client = ScreenerClient(self.api_key, self.secret_key)
            # top gainers by percent
            req = MarketMoversRequest(top=10)
            movers = client.get_market_movers(req)
            rows = []
            # SDK may return object with .gainers / .losers or a dict
            gainers = getattr(movers, "gainers", None)
            if gainers is None and isinstance(movers, dict):
                gainers = movers.get("gainers") or movers.get("movers") or []
            for g in gainers or []:
                if isinstance(g, dict):
                    sym = g.get("symbol") or g.get("ticker")
                    chg = g.get("percent_change") or g.get("change") or 0
                    px = g.get("price") or g.get("last") or 0
                else:
                    sym = getattr(g, "symbol", None)
                    chg = getattr(g, "percent_change", None) or getattr(g, "change", 0)
                    px = getattr(g, "price", None) or getattr(g, "last", 0)
                if not sym:
                    continue
                try:
                    chg_f = float(chg)
                except (TypeError, ValueError):
                    chg_f = 0.0
                rows.append({
                    "symbol": str(sym).upper(),
                    "pct": chg_f,
                    "price": float(px or 0),
                })
            self._rows = rows
            self._ts = time.time()
            self._err = None
        except Exception as e:
            self._err = str(e)[:160]
            log.warning("[screener] movers failed: %s", self._err)
            # keep stale rows if any
            self._ts = time.time()  # back off

    @property
    def error(self) -> Optional[str]:
        return self._err
