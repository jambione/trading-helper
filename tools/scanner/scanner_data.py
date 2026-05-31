"""
scanner_data.py — Market data client for the embedded scanner engine.

Wraps YahooFinanceClient for dynamic ticker discovery and fetches the
Finnhub earnings calendar once per calendar day to seed the universe with
earnings-day tickers.

Interface matches what ScannerEngine expects:
  get_gainers() -> list[dict]
  get_losers()  -> list[dict]
  get_float(sym) -> Optional[float]
  _float_cache  -> dict
"""

import asyncio
import logging
from datetime import date
from typing import Optional

import httpx

from yahoo_client import YahooFinanceClient

log = logging.getLogger(__name__)

_FINNHUB_EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"


class ScannerDataClient:
    def __init__(self, finnhub_key: str = "") -> None:
        self._yahoo = YahooFinanceClient()
        self._finnhub_key = finnhub_key
        self._earnings_cache: list[str] = []
        self._earnings_date: Optional[date] = None

    @property
    def _float_cache(self) -> dict:
        return self._yahoo._float_cache

    async def get_gainers(self) -> list[dict]:
        gainers = await self._yahoo.get_gainers()
        earnings = await self._get_earnings_tickers()
        if earnings:
            existing = {g["ticker"] for g in gainers}
            new_syms = [s for s in earnings if s not in existing]
            if new_syms:
                loop = asyncio.get_running_loop()
                extra = await loop.run_in_executor(None, self._yahoo._fetch_watchlist, new_syms)
                gainers = gainers + extra
        return gainers

    async def get_losers(self) -> list[dict]:
        return await self._yahoo.get_losers()

    async def get_float(self, ticker: str) -> Optional[float]:
        return await self._yahoo.get_float(ticker)

    async def close(self) -> None:
        await self._yahoo.close()

    async def _get_earnings_tickers(self) -> list[str]:
        today = date.today()
        if self._earnings_date == today:
            return self._earnings_cache
        if not self._finnhub_key:
            self._earnings_cache = []
            self._earnings_date = today
            return []
        try:
            today_str = today.isoformat()
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    _FINNHUB_EARNINGS_URL,
                    params={"from": today_str, "to": today_str, "token": self._finnhub_key},
                )
                r.raise_for_status()
                items = r.json().get("earningsCalendar", [])
            tickers = [
                e["symbol"] for e in items
                if e.get("symbol") and "." not in e["symbol"] and len(e["symbol"]) <= 5
            ]
            self._earnings_cache = tickers
            self._earnings_date = today
            log.info("[SCANNER] Earnings calendar: %d tickers for %s", len(tickers), today_str)
        except Exception as ex:
            log.warning("[SCANNER] Earnings calendar fetch failed: %s", ex)
            self._earnings_cache = []
            self._earnings_date = today  # don't hammer Finnhub on repeated failures
        return self._earnings_cache
