"""Stocktwits free trending poll for the momentum monitor.

Public JSON (no key required today):
  GET https://api.stocktwits.com/api/2/trending/symbols.json

Same family of data as https://stocktwits.com/sentiment — rank, symbol,
trending_score, watchers. Live last price is usually missing here; the
monitor overlays dashboard/Alpaca prices when available and can filter
to stocks under max_price.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional
from urllib.request import Request, urlopen

TRENDING_URL = "https://api.stocktwits.com/api/2/trending/symbols.json"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 "
    "BrasfieldMomentum/1.0"
)


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_trending_payload(data: dict) -> list[dict[str, Any]]:
    """Normalize API payload → list of equity/crypto rows with rank."""
    out: list[dict[str, Any]] = []
    for s in data.get("symbols") or []:
        if not isinstance(s, dict):
            continue
        sym = str(s.get("symbol") or s.get("symbol_display") or "").upper().strip()
        if not sym:
            continue
        # Drop pure crypto stream tickers (e.g. HBAR.X) when class says so
        iclass = str(s.get("instrument_class") or "").lower()
        fund = s.get("fundamentals") if isinstance(s.get("fundamentals"), dict) else {}
        mcap = _f(fund.get("MarketCapitalization") or fund.get("market_cap"))
        out.append({
            "symbol": sym,
            "rank": int(s.get("rank") or 0) or None,
            "title": s.get("title") or "",
            "trending_score": _f(s.get("trending_score")),
            "watchlist_count": int(s.get("watchlist_count") or 0),
            "instrument_class": iclass or "stock",
            "is_equity": iclass in ("", "stock", "etf", "adr"),
            "is_crypto": iclass == "cryptocurrency" or sym.endswith(".X"),
            "market_cap": mcap,
            "exchange": s.get("exchange") or "",
        })
    out.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 999))
    return out


def fetch_trending(timeout: float = 10.0) -> list[dict[str, Any]]:
    """HTTP GET trending symbols. Empty list on any failure."""
    try:
        req = Request(
            TRENDING_URL,
            headers={"Accept": "application/json", "User-Agent": _UA},
        )
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if not isinstance(data, dict):
            return []
        return parse_trending_payload(data)
    except Exception:
        return []


class StocktwitsTrending:
    """Throttled cache of Stocktwits trending (default poll ~60s)."""

    def __init__(self, poll_interval: float = 60.0, stocks_only: bool = True,
                 max_price: Optional[float] = 30.0):
        self.poll_interval = max(15.0, float(poll_interval))
        self.stocks_only = bool(stocks_only)
        self.max_price = float(max_price) if max_price is not None else None
        self.rows: list[dict[str, Any]] = []
        self.by_symbol: dict[str, dict[str, Any]] = {}
        self.last_ok: float = 0.0
        self.last_attempt: float = 0.0
        self.error: str = ""

    def refresh(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        if self.last_attempt and (now - self.last_attempt) < self.poll_interval:
            return False
        self.last_attempt = now
        rows = fetch_trending()
        if not rows:
            self.error = "fetch failed or empty"
            return False
        if self.stocks_only:
            rows = [r for r in rows if r.get("is_equity") and not r.get("is_crypto")]
        self.rows = rows
        self.by_symbol = {r["symbol"]: r for r in rows}
        self.last_ok = now
        self.error = ""
        return True

    def rank_of(self, symbol: str) -> Optional[int]:
        r = self.by_symbol.get((symbol or "").upper())
        if not r:
            return None
        return r.get("rank")

    def display_rows(self, price_by_sym: dict[str, Optional[float]],
                     limit: int = 12) -> list[dict[str, Any]]:
        """Rows for the monitor panel; attach prices and optional max_price filter."""
        out = []
        for r in self.rows:
            sym = r["symbol"]
            px = price_by_sym.get(sym)
            if px is None:
                # allow unknown price through (still useful as social heat)
                row = {**r, "price": None, "price_known": False}
            else:
                if self.max_price is not None and px >= self.max_price:
                    continue
                row = {**r, "price": px, "price_known": True}
            out.append(row)
            if len(out) >= limit:
                break
        return out
