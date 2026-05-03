import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Active tickers used when the Yahoo screener is unavailable (e.g. outside market hours).
FALLBACK_WATCHLIST = [
    "TSLA", "NVDA", "AMD", "AMZN", "META", "GOOGL", "MSFT", "AAPL",
    "PLTR", "NIO", "RIVN", "LCID", "SOFI", "HOOD", "COIN",
    "MARA", "RIOT", "SNDL", "CLOV", "WKHS",
    "BBAI", "BCRX", "KOSS", "SPCE", "VNET",
]

_SCREENER_URL = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
_SCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


class YahooFinanceClient:
    def __init__(self) -> None:
        self._float_cache: dict[str, Optional[float]] = {}
        self._http = httpx.AsyncClient(timeout=15.0, headers=_SCREENER_HEADERS)

    async def close(self) -> None:
        await self._http.aclose()

    async def get_gainers(self) -> list[dict]:
        quotes = await self._fetch_screener("day_gainers")
        if quotes:
            return quotes
        logger.info("Screener unavailable — using fallback watchlist (%d tickers)", len(FALLBACK_WATCHLIST))
        return await asyncio.get_running_loop().run_in_executor(None, self._fetch_watchlist, FALLBACK_WATCHLIST)

    async def get_losers(self) -> list[dict]:
        return await self._fetch_screener("day_losers")

    async def get_float(self, ticker: str) -> Optional[float]:
        return self._float_cache.get(ticker)

    async def _fetch_screener(self, screen_id: str) -> list[dict]:
        try:
            r = await self._http.get(
                _SCREENER_URL,
                params={"scrIds": screen_id, "count": 50, "formatted": "false",
                        "lang": "en-US", "region": "US"},
            )
            r.raise_for_status()
            quotes = r.json()["finance"]["result"][0]["quotes"]
            logger.info("Screener '%s' returned %d quotes", screen_id, len(quotes))
            return [self._convert(q) for q in quotes if q.get("symbol")]
        except Exception as e:
            logger.warning("Screener '%s' failed: %s", screen_id, e)
            return []

    def _fetch_watchlist(self, symbols: list[str]) -> list[dict]:
        try:
            import yfinance as yf
            tickers = yf.Tickers(" ".join(symbols))
            results = []
            for sym in symbols:
                try:
                    fi = tickers.tickers[sym].fast_info
                    price = float(fi.last_price or 0)
                    if price <= 0:
                        continue
                    prev = float(fi.previous_close or price)
                    high = float(fi.day_high or price)
                    low = float(fi.day_low or price)
                    vol = int(fi.three_month_average_volume or 0)
                    chg_pct = ((price - prev) / prev * 100) if prev else 0
                    results.append({
                        "ticker": sym,
                        "day": {
                            "o": float(fi.open or price),
                            "h": high,
                            "l": low,
                            "c": price,
                            "v": vol,
                            "vw": round((high + low + price) / 3, 2),
                        },
                        "prevDay": {"c": prev, "v": vol},
                        "todaysChangePerc": round(chg_pct, 2),
                        "todaysChange": round(price - prev, 2),
                        "min": {"v": 0, "c": price},
                    })
                except Exception:
                    pass
            return results
        except Exception as e:
            logger.error("Watchlist fetch failed: %s", e)
            return []

    def _convert(self, quote: dict) -> dict:
        symbol = quote.get("symbol", "")
        price = float(quote.get("regularMarketPrice") or 0)
        high = float(quote.get("regularMarketDayHigh") or price)
        low = float(quote.get("regularMarketDayLow") or price)
        open_p = float(quote.get("regularMarketOpen") or price)
        volume = int(quote.get("regularMarketVolume") or 0)
        prev_close = float(quote.get("regularMarketPreviousClose") or price)
        change_pct = float(quote.get("regularMarketChangePercent") or 0)
        change = float(quote.get("regularMarketChange") or 0)
        avg_vol = int(quote.get("averageDailyVolume3Month") or 0)
        float_shares = quote.get("floatShares")

        if float_shares and symbol:
            self._float_cache[symbol] = round(float(float_shares) / 1_000_000, 2)

        return {
            "ticker": symbol,
            "day": {
                "o": open_p,
                "h": high,
                "l": low,
                "c": price,
                "v": volume,
                "vw": round((high + low + price) / 3, 2),
            },
            "prevDay": {"c": prev_close, "v": avg_vol},
            "todaysChangePerc": change_pct,
            "todaysChange": change,
            "min": {"v": 0, "c": price},
        }
