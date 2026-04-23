from datetime import datetime, timedelta, timezone
from typing import Optional
import time
import logging

import pandas as pd


def retry_with_backoff(max_retries: int = 3, base_wait: float = 1.0):
    """Decorator for Alpaca API calls — exponential backoff on 429/503."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    err = str(e)
                    retryable = "429" in err or "503" in err or "rate" in err.lower() or "service unavailable" in err.lower()
                    if retryable and attempt < max_retries - 1:
                        wait = base_wait * (2 ** attempt)
                        logging.warning(f"[ALPACA] {func.__name__} retry {attempt+1}/{max_retries} in {wait}s — {err[:80]}")
                        time.sleep(wait)
                    else:
                        raise
            return None
        return wrapper
    return decorator


def connect_data_client(cfg: dict):
    """Return an Alpaca StockHistoricalDataClient for bar/price fetching."""
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(cfg["api_key"], cfg["secret_key"])


def _get_feed_arg(cfg: dict = None) -> dict:
    try:
        from alpaca.data.enums import DataFeed as _DF
        cfg = cfg or {}
        feed = _DF.SIP if cfg.get("data_feed", "IEX").upper() == "SIP" else _DF.IEX
        return {"feed": feed}
    except Exception:
        return {}


def fetch_bars(data_client, ticker: str, cfg: dict) -> Optional[pd.DataFrame]:
    @retry_with_backoff(max_retries=3, base_wait=1.0)
    def _fetch():
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        tf_map = {
            "1Min":  TimeFrame(1,  TimeFrameUnit.Minute),
            "5Min":  TimeFrame(5,  TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "1Hour": TimeFrame(1,  TimeFrameUnit.Hour),
            "1Day":  TimeFrame(1,  TimeFrameUnit.Day),
        }
        tf = tf_map.get(cfg.get("bar_timeframe", "5Min"), TimeFrame(5, TimeFrameUnit.Minute))
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=tf,
            start=datetime.now(timezone.utc) - timedelta(days=10),
            limit=cfg.get("bar_count", 300),
            **_get_feed_arg(cfg),
        )
        bars = data_client.get_stock_bars(req).df
        if bars is None or bars.empty:
            return None
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(ticker, level="symbol")
        bars = bars[["open", "high", "low", "close", "volume"]].copy()
        bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
        return bars.dropna(subset=["close"])

    try:
        return _fetch()
    except Exception:
        return None


def fetch_bars_batch(data_client, tickers: list, cfg: dict) -> dict:
    if not tickers or data_client is None:
        return {}

    @retry_with_backoff(max_retries=3, base_wait=1.0)
    def _fetch():
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        tf_map = {
            "1Min":  TimeFrame(1,  TimeFrameUnit.Minute),
            "5Min":  TimeFrame(5,  TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "1Hour": TimeFrame(1,  TimeFrameUnit.Hour),
            "1Day":  TimeFrame(1,  TimeFrameUnit.Day),
        }
        tf = tf_map.get(cfg.get("bar_timeframe", "5Min"), TimeFrame(5, TimeFrameUnit.Minute))
        req = StockBarsRequest(
            symbol_or_symbols=tickers,
            timeframe=tf,
            start=datetime.now(timezone.utc) - timedelta(days=10),
            limit=cfg.get("bar_count", 300),
            **_get_feed_arg(cfg),
        )
        bars = data_client.get_stock_bars(req).df
        if bars is None or bars.empty:
            return {}

        results = {}
        if isinstance(bars.index, pd.MultiIndex):
            for t in tickers:
                try:
                    df = bars.xs(t, level="symbol")[["open", "high", "low", "close", "volume"]].copy()
                    df["close"] = pd.to_numeric(df["close"], errors="coerce")
                    df = df.dropna(subset=["close"])
                    if not df.empty:
                        results[t] = df
                except KeyError:
                    continue
        else:
            bars = bars[["open", "high", "low", "close", "volume"]].copy()
            bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
            bars = bars.dropna(subset=["close"])
            if not bars.empty:
                results[tickers[0]] = bars
        return results

    try:
        return _fetch()
    except Exception:
        return {}


def get_latest_trade_price(data_client, ticker: str, cfg: dict = None) -> Optional[float]:
    cfg = cfg or {}

    @retry_with_backoff(max_retries=3, base_wait=1.0)
    def _fetch():
        from alpaca.data.requests import StockLatestBarRequest
        resp = data_client.get_stock_latest_bar(StockLatestBarRequest(
            symbol_or_symbols=ticker,
            **_get_feed_arg(cfg)
        ))
        bar = resp.get(ticker)
        return float(bar.close) if bar else None

    try:
        return _fetch()
    except Exception:
        return None


def get_latest_trade_prices(data_client, tickers: list, cfg: dict = None) -> dict:
    cfg = cfg or {}
    if not tickers or data_client is None:
        return {}

    @retry_with_backoff(max_retries=3, base_wait=1.0)
    def _fetch():
        from alpaca.data.requests import StockLatestBarRequest
        resp = data_client.get_stock_latest_bar(StockLatestBarRequest(
            symbol_or_symbols=tickers,
            **_get_feed_arg(cfg)
        ))
        results = {}
        for t in tickers:
            try:
                bar = resp.get(t)
                if bar is not None and getattr(bar, "close", None) is not None:
                    results[t] = float(bar.close)
            except Exception:
                continue
        return results

    try:
        return _fetch()
    except Exception:
        return {}
