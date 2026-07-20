"""Fetch recent trades from Alpaca REST for the tape window."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Tuple

log = logging.getLogger(__name__)


def fetch_recent_trades(data_client, symbol: str, lookback_sec: float = 90.0,
                        feed_kw: dict | None = None) -> list[Tuple[float, float, float]]:
    """Return list of (ts_epoch, price, size) newest last."""
    try:
        from alpaca.data.requests import StockTradesRequest
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=lookback_sec)
        req = StockTradesRequest(
            symbol_or_symbols=symbol,
            start=start,
            end=end,
            limit=500,
            **(feed_kw or {}),
        )
        result = data_client.get_stock_trades(req)
        # result.df multi-index or .data dict
        trades = []
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            rows = data.get(symbol) or data.get(symbol.upper()) or []
            for t in rows:
                ts = getattr(t, "timestamp", None)
                px = float(getattr(t, "price", 0) or 0)
                sz = float(getattr(t, "size", 0) or 0)
                if ts is not None and px > 0 and sz > 0:
                    try:
                        epoch = ts.timestamp()
                    except Exception:
                        epoch = float(ts)
                    trades.append((epoch, px, sz))
        else:
            df = getattr(result, "df", None)
            if df is not None and not df.empty:
                import pandas as pd
                if isinstance(df.index, pd.MultiIndex):
                    try:
                        df = df.xs(symbol, level="symbol")
                    except Exception:
                        pass
                for idx, row in df.iterrows():
                    try:
                        epoch = idx.timestamp() if hasattr(idx, "timestamp") else float(idx)
                    except Exception:
                        continue
                    px = float(row.get("price", 0) or 0)
                    sz = float(row.get("size", 0) or 0)
                    if px > 0 and sz > 0:
                        trades.append((epoch, px, sz))
        trades.sort(key=lambda x: x[0])
        return trades
    except Exception as e:
        log.debug("[trades] %s: %s", symbol, e)
        return []
