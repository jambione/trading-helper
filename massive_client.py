#!/usr/bin/env python3
"""
massive_client.py — Massive.com Stock Market API client

Wraps the Massive REST API for:
  • fetch_bars()     — OHLCV aggregate bars (matches Alpaca DataFrame format)
  • fetch_snapshot() — latest price / previous-day bar
  • fetch_news()     — recent news articles with sentiment

API docs: https://massive.com/docs/rest/stocks
Endpoint: https://api.massive.com

Free tier (Stocks Basic):
  • Aggregate bars  — end-of-day data only, 2 years history
  • News            — hourly updates, 2 years history

Paid tier ($29/mo Starter):
  • Aggregate bars  — 15-minute delayed, 5 years history

Set MASSIVE_API_KEY in signal_engine.env or as an environment variable.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL   = "https://api.massive.com"
TIMEOUT    = 10   # seconds per HTTP request
MAX_LIMIT  = 5000 # Massive max bars per request

# Timespan mapping: Alpaca-style → Massive timespan enum
_TIMESPAN_MAP = {
    "1Min":  ("minute",  1),
    "5Min":  ("minute",  5),
    "15Min": ("minute", 15),
    "30Min": ("minute", 30),
    "1Hour": ("hour",    1),
    "1Day":  ("day",     1),
    # direct Massive names also accepted
    "minute": ("minute", 1),
    "hour":   ("hour",   1),
    "day":    ("day",    1),
}


class MassiveClient:
    """
    Thin REST client for the Massive.com API.

    Usage:
        client = MassiveClient(api_key="YOUR_KEY")
        df = client.fetch_bars("AAPL", timeframe="1Min", count=200, lookback_days=5)
    """

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or os.getenv("MASSIVE_API_KEY", "")
        self._session = requests.Session()
        if self.api_key:
            self._session.params = {"apiKey": self.api_key}  # type: ignore[assignment]

    # ── Public API ─────────────────────────────────────────────────────────────

    def fetch_bars(
        self,
        symbol: str,
        timeframe: str = "1Min",
        count: int = 200,
        lookback_days: int = 5,
    ) -> Optional[pd.DataFrame]:
        """
        Fetch OHLCV bars from Massive and return a DataFrame that mirrors the
        Alpaca format used throughout signal_engine.py:
          columns: open, high, low, close, volume, time

        Args:
            symbol:       Ticker symbol (e.g. "AAPL")
            timeframe:    Alpaca-style timeframe string: "1Min", "5Min", "1Hour", "1Day"
            count:        Maximum number of bars to return
            lookback_days: How many calendar days back to start from

        Returns:
            pd.DataFrame or None if the request fails / no data.
        """
        if not self.api_key:
            log.warning("[MASSIVE] No API key — skipping bar fetch for %s", symbol)
            return None

        timespan, multiplier = _TIMESPAN_MAP.get(timeframe, ("minute", 1))

        now      = datetime.now(timezone.utc)
        from_dt  = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        to_dt    = now.strftime("%Y-%m-%d")

        url = (
            f"{BASE_URL}/v2/aggs/ticker/{symbol}/range"
            f"/{multiplier}/{timespan}/{from_dt}/{to_dt}"
        )
        params = {
            "adjusted": "true",
            "sort":     "asc",
            "limit":    min(count, MAX_LIMIT),
        }

        try:
            resp = self._session.get(url, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as e:
            log.warning("[MASSIVE] HTTP error for %s: %s", symbol, e)
            return None
        except Exception as e:
            log.warning("[MASSIVE] Request failed for %s: %s", symbol, e)
            return None

        if data.get("status") not in ("OK", "DELAYED"):
            log.warning("[MASSIVE] Non-OK status for %s: %s", symbol, data.get("status"))
            return None

        results = data.get("results", [])
        if not results:
            log.debug("[MASSIVE] No bars returned for %s (%s → %s)", symbol, from_dt, to_dt)
            return None

        df = pd.DataFrame(results).rename(columns={
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
            "t": "time",   # Unix millisecond timestamp
            "vw": "vwap",
        })

        # Ensure numeric types
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # Convert ms timestamp → ISO string (for compatibility with Alpaca)
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        df = df.reset_index(drop=True)
        log.info("[MASSIVE] %s: %d bars fetched (%s → %s)", symbol, len(df), from_dt, to_dt)
        return df

    def fetch_snapshot(self, symbol: str) -> Optional[dict]:
        """
        Fetch the latest price snapshot for a symbol.
        Returns a dict with keys: price, open, high, low, close, volume, updated_ts
        or None on failure.

        Uses the Previous Day Bar endpoint (available on free tier).
        """
        if not self.api_key:
            return None

        url = f"{BASE_URL}/v2/aggs/ticker/{symbol}/prev"
        try:
            resp = self._session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.debug("[MASSIVE] Snapshot failed for %s: %s", symbol, e)
            return None

        results = data.get("results", [])
        if not results:
            return None

        bar = results[0]
        return {
            "price":      bar.get("c"),
            "open":       bar.get("o"),
            "high":       bar.get("h"),
            "low":        bar.get("l"),
            "close":      bar.get("c"),
            "volume":     bar.get("v"),
            "vwap":       bar.get("vw"),
            "updated_ts": bar.get("t"),
        }

    def fetch_news(
        self,
        symbol: str,
        limit: int = 5,
    ) -> list[dict]:
        """
        Fetch recent news articles for a ticker with sentiment analysis.
        Available on free tier (updated hourly, 2 years history).

        Returns a list of dicts with keys:
          title, description, article_url, published_utc,
          sentiment, sentiment_reasoning, publisher_name
        """
        if not self.api_key:
            return []

        url = f"{BASE_URL}/v2/reference/news"
        params = {
            "ticker": symbol,
            "limit":  limit,
            "order":  "desc",
            "sort":   "published_utc",
        }
        try:
            resp = self._session.get(url, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.debug("[MASSIVE] News fetch failed for %s: %s", symbol, e)
            return []

        articles = []
        for item in data.get("results", []):
            # Extract sentiment from insights array if present
            sentiment        = None
            sentiment_reason = None
            for insight in item.get("insights", []):
                if insight.get("ticker", "").upper() == symbol.upper():
                    sentiment        = insight.get("sentiment")
                    sentiment_reason = insight.get("sentiment_reasoning")
                    break

            articles.append({
                "title":               item.get("title", ""),
                "description":         item.get("description", ""),
                "article_url":         item.get("article_url", ""),
                "published_utc":       item.get("published_utc", ""),
                "publisher_name":      item.get("publisher", {}).get("name", ""),
                "tickers":             item.get("tickers", []),
                "sentiment":           sentiment,
                "sentiment_reasoning": sentiment_reason,
            })

        log.debug("[MASSIVE] %s: %d news articles fetched", symbol, len(articles))
        return articles

    # ── Convenience helpers ────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        """Return True if an API key is set."""
        return bool(self.api_key)

    def test_connection(self) -> bool:
        """
        Quick connectivity check using the Previous Day Bar endpoint for AAPL.
        Returns True if the API responds successfully.
        """
        snap = self.fetch_snapshot("AAPL")
        ok   = snap is not None
        if ok:
            log.info("[MASSIVE] Connection OK — AAPL prev close: %s", snap.get("close"))
        else:
            log.warning("[MASSIVE] Connection test failed")
        return ok


# ── Module-level singleton (loaded lazily from env) ───────────────────────────
# signal_engine.py imports this and calls massive.fetch_bars() directly.

_client: Optional[MassiveClient] = None


def get_client() -> MassiveClient:
    """Return the module-level MassiveClient, initialising it on first call."""
    global _client
    if _client is None:
        _client = MassiveClient()
    return _client


def fetch_bars(
    symbol: str,
    timeframe: str = "1Min",
    count: int = 200,
    lookback_days: int = 5,
) -> Optional[pd.DataFrame]:
    """Module-level convenience wrapper for get_client().fetch_bars()."""
    return get_client().fetch_bars(symbol, timeframe, count, lookback_days)


def fetch_news(symbol: str, limit: int = 5) -> list[dict]:
    """Module-level convenience wrapper for get_client().fetch_news()."""
    return get_client().fetch_news(symbol, limit)
