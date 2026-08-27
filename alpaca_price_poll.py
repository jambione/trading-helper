"""Alpaca IEX latest-trade poller — free-tier live prices for the signal engine.

Replaces (or backs up) Finnhub's free 50-symbol WebSocket for last-price
lookups. Polls StockLatestTrade for the subscribed set on a short interval.

API surface mirrors the bits of finnhub_stream the engine uses:
  start_alpaca_price_poll, request_subscribe, request_unsubscribe,
  get_latest_price, register_trade_callback
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Callable, Optional

log = logging.getLogger(__name__)

_HIST_SECS = 360.0
_HIST_MIN_GAP = 1.0


class _PriceState:
    def __init__(self):
        self.lock = threading.Lock()
        self.prices: dict = {}       # symbol -> {price, ts_unix, ...}
        self.subscribed: set = set()
        self.connected = False
        self.history: dict = {}      # symbol -> deque[(ts, price)]
        self._hist_last: dict = {}

    def update(self, symbol: str, price: float, size: float = 0.0):
        now = time.time()
        sym = symbol.upper()
        with self.lock:
            self.prices[sym] = {
                "price": price,
                "volume": size,
                "ts_unix": now,
                "updated": time.strftime("%H:%M:%S"),
            }
            if now - self._hist_last.get(sym, 0.0) >= _HIST_MIN_GAP:
                dq = self.history.get(sym)
                if dq is None:
                    if len(self.history) > 64:
                        for k in [k for k, v in self.history.items()
                                  if not v or now - v[-1][0] > _HIST_SECS]:
                            self.history.pop(k, None)
                            self._hist_last.pop(k, None)
                    dq = self.history[sym] = deque(maxlen=512)
                dq.append((now, price))
                self._hist_last[sym] = now
                while dq and now - dq[0][0] > _HIST_SECS:
                    dq.popleft()


STATE = _PriceState()
_callbacks: list[Callable] = []
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_client = None
_poll_sec = 1.0


def register_trade_callback(cb: Callable):
    """cb(symbol, price, volume, ts_unix) on each successful poll update."""
    if cb not in _callbacks:
        _callbacks.append(cb)


def request_subscribe(tickers: list):
    with STATE.lock:
        for t in tickers or []:
            if t:
                STATE.subscribed.add(str(t).upper())


def request_unsubscribe(tickers: list):
    with STATE.lock:
        for t in tickers or []:
            STATE.subscribed.discard(str(t).upper())


def get_latest_price(ticker: str) -> Optional[float]:
    with STATE.lock:
        row = STATE.prices.get(str(ticker).upper())
        if not row:
            return None
        # stale after 30s
        if time.time() - row.get("ts_unix", 0) > 30:
            return None
        return float(row["price"])


def recent_prices(ticker: str, seconds: float) -> list:
    now = time.time()
    with STATE.lock:
        dq = STATE.history.get(str(ticker).upper()) or []
        return [(t, p) for t, p in dq if now - t <= seconds]


def _poll_once():
    global _client
    if _client is None:
        return
    with STATE.lock:
        symbols = list(STATE.subscribed)
    if not symbols:
        return
    try:
        from alpaca.data.requests import StockLatestTradeRequest
        from alpaca.data.enums import DataFeed
        # batch if possible
        req = StockLatestTradeRequest(
            symbol_or_symbols=symbols if len(symbols) > 1 else symbols[0],
            feed=DataFeed.IEX,
        )
        trades = _client.get_stock_latest_trade(req)
        if not isinstance(trades, dict):
            trades = {symbols[0]: trades} if trades is not None else {}
        now = time.time()
        for sym, tr in trades.items():
            if tr is None:
                continue
            try:
                px = float(tr.price)
                sz = float(getattr(tr, "size", 0) or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            if px <= 0:
                continue
            # The TRADE's own timestamp, not this poll's read time.
            #
            # `now` is identical for every symbol in the batch, so stamping
            # trades with it told the bar aggregator that a print from twenty
            # minutes ago had just happened — and that clock is what
            # rt_price_age_sec, and therefore the dashboard's price merge,
            # reads. The tell on 2026-08-27: eight symbols publishing the
            # exact same 0.4s age. Real trade clocks do not line up.
            #
            # StockLatestTradeRequest has always returned this; it was simply
            # dropped on the floor. `now` survives only as a last resort for a
            # trade with no timestamp at all.
            t_raw = getattr(tr, "timestamp", None)
            try:
                t_unix = t_raw.timestamp() if t_raw is not None else now
            except (AttributeError, TypeError, ValueError, OSError):
                t_unix = now
            STATE.update(str(sym).upper(), px, sz)
            for cb in list(_callbacks):
                try:
                    cb(str(sym).upper(), px, sz, t_unix)
                except Exception:
                    pass
        STATE.connected = True
    except Exception as e:
        log.debug("[ALPACA_PX] poll failed: %s", e)


def _loop():
    while not _stop.is_set():
        _poll_once()
        _stop.wait(_poll_sec)


def start_alpaca_price_poll(api_key: str, secret_key: str,
                            tickers: list | None = None,
                            poll_sec: float = 1.0) -> bool:
    """Start background IEX latest-trade polling. Returns True if started."""
    global _client, _thread, _poll_sec
    if not api_key or not secret_key:
        return False
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        _client = StockHistoricalDataClient(api_key, secret_key)
    except Exception as e:
        log.error("[ALPACA_PX] client init failed: %s", e)
        return False

    _poll_sec = max(0.5, float(poll_sec))
    if tickers:
        request_subscribe(tickers)

    if _thread and _thread.is_alive():
        return True

    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="alpaca-px-poll")
    _thread.start()
    log.info("[ALPACA_PX] IEX latest-trade poller started (%.1fs)", _poll_sec)
    return True


def stop_alpaca_price_poll():
    _stop.set()
