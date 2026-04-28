"""
finnhub_stream.py — Real-time stock data via Finnhub WebSocket
Supports dynamic ticker subscriptions while the stream is live.
"""

import asyncio
import json
import logging
import queue as _queue
import threading
import time
from datetime import datetime
from collections import deque
from zoneinfo import ZoneInfo

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)


# ── Shared State ─────────────────────────────────────────────

class FinnhubState:
    def __init__(self):
        self.lock       = threading.Lock()
        self.prices     = {}           # ticker → {price, volume, timestamp, updated}
        self.subscribed = set()        # currently subscribed tickers
        self.connected  = False
        self.last_trade = {}           # ticker → last trade data
        self.log_lines  = deque(maxlen=100)

    def add_log(self, level: str, msg: str):
        ts = datetime.now(ET).strftime("%H:%M:%S")
        with self.lock:
            self.log_lines.append({"ts": ts, "level": level, "msg": msg})

    def update_price(self, ticker: str, price: float, volume: int = 0, timestamp: int = 0):
        with self.lock:
            self.prices[ticker] = {
                "price":     price,
                "volume":    volume,
                "timestamp": timestamp,
                "ts_unix":   time.time(),
                "updated":   datetime.now(ET).strftime("%H:%M:%S"),
            }

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "prices":     dict(self.prices),
                "subscribed": list(self.subscribed),
                "connected":  self.connected,
                "log_lines":  list(self.log_lines)[-50:],
            }


FINNHUB_STATE  = FinnhubState()
_STREAM_THREAD = None
_pending_subs: _queue.Queue = _queue.Queue()


# ── Dynamic subscription ──────────────────────────────────────

def request_subscribe(tickers: list):
    """Queue new tickers for subscription (thread-safe, works while stream runs)."""
    with FINNHUB_STATE.lock:
        already = set(FINNHUB_STATE.subscribed)
    for t in tickers:
        t = t.upper()
        if t not in already:
            _pending_subs.put(t)


# ── WebSocket Stream ─────────────────────────────────────────

async def _drain_pending(ws):
    """Send any queued subscriptions to the live WebSocket."""
    while not _pending_subs.empty():
        try:
            t = _pending_subs.get_nowait()
            await ws.send(json.dumps({"type": "subscribe", "symbol": t}))
            with FINNHUB_STATE.lock:
                FINNHUB_STATE.subscribed.add(t)
        except _queue.Empty:
            break
        except Exception as e:
            FINNHUB_STATE.add_log("WARN", f"Sub error for {t}: {e}")


async def _finnhub_stream(api_key: str, tickers: list):
    if not WEBSOCKETS_AVAILABLE:
        return

    url = f"wss://ws.finnhub.io?token={api_key}"
    reconnect_attempts = 0
    max_reconnect      = 5
    base_wait          = 1.0

    while True:
        try:
            async with websockets.connect(url, ping_interval=30) as ws:
                reconnect_attempts    = 0
                FINNHUB_STATE.connected = True
                FINNHUB_STATE.add_log("INFO", f"Finnhub connected ({len(tickers)} tickers)")

                for ticker in tickers[:50]:   # free tier: max 50 symbols
                    await ws.send(json.dumps({"type": "subscribe", "symbol": ticker}))
                    with FINNHUB_STATE.lock:
                        FINNHUB_STATE.subscribed.add(ticker)

                # Receive loop: 1-second timeout lets us drain pending subs
                while True:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        await _drain_pending(ws)
                        continue

                    try:
                        data = json.loads(message)
                        if data.get("type") == "trade":
                            for trade in data.get("data", []):
                                sym   = trade.get("s", "")
                                price = trade.get("p", 0)
                                vol   = trade.get("v", 0)
                                ts    = trade.get("t", 0)
                                if sym and price > 0:
                                    FINNHUB_STATE.update_price(sym, price, vol, ts)
                                    FINNHUB_STATE.last_trade[sym] = {
                                        "price": price, "volume": vol, "timestamp": ts
                                    }
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        FINNHUB_STATE.add_log("WARN", f"Parse error: {e}")

        except asyncio.CancelledError:
            FINNHUB_STATE.add_log("INFO", "Finnhub stream cancelled")
            break
        except Exception as e:
            FINNHUB_STATE.connected = False
            reconnect_attempts += 1
            wait = base_wait * (2 ** min(reconnect_attempts - 1, max_reconnect - 1))
            if reconnect_attempts >= max_reconnect:
                FINNHUB_STATE.add_log(
                    "ERROR",
                    f"Finnhub: {max_reconnect} failures. Retrying in {wait:.0f}s."
                )
                await asyncio.sleep(wait)
                reconnect_attempts = 0
            else:
                FINNHUB_STATE.add_log(
                    "WARN",
                    f"Finnhub error ({reconnect_attempts}/{max_reconnect}): "
                    f"{str(e)[:80]}. Reconnecting in {wait:.0f}s…"
                )
                await asyncio.sleep(wait)
        finally:
            FINNHUB_STATE.connected = False


def start_finnhub_stream(api_key: str, tickers: list):
    """Start (or restart) the Finnhub WebSocket stream in a background thread."""
    global _STREAM_THREAD
    if not WEBSOCKETS_AVAILABLE:
        FINNHUB_STATE.add_log("ERROR", "websockets library not installed")
        return None
    if not api_key:
        FINNHUB_STATE.add_log("ERROR", "No Finnhub API key configured")
        return None

    # If a stream is already running, just subscribe new tickers via the queue
    if _STREAM_THREAD and _STREAM_THREAD.is_alive():
        if tickers:
            request_subscribe(tickers)
        return _STREAM_THREAD

    # Flush any stale pending subs from a previous session
    while not _pending_subs.empty():
        try:
            _pending_subs.get_nowait()
        except _queue.Empty:
            break
    with FINNHUB_STATE.lock:
        FINNHUB_STATE.subscribed.clear()

    def _run():
        asyncio.run(_finnhub_stream(api_key, tickers))

    thread = threading.Thread(target=_run, daemon=True, name="finnhub-ws")
    thread.start()
    _STREAM_THREAD = thread
    FINNHUB_STATE.add_log("INFO", f"Starting Finnhub stream for {len(tickers)} tickers…")
    return thread


# ── REST API helpers ──────────────────────────────────────────

def fetch_realtime_quote(api_key: str, ticker: str) -> dict:
    """Fetch current quote from Finnhub REST API (fallback for non-streamed tickers)."""
    try:
        import urllib.request
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={api_key}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            return {
                "ok": True, "ticker": ticker,
                "c": data.get("c", 0), "h": data.get("h", 0),
                "l": data.get("l", 0), "o": data.get("o", 0),
                "pc": data.get("pc", 0),
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_latest_price(ticker: str) -> "float | None":
    """Return the last trade price for a ticker from the active stream."""
    with FINNHUB_STATE.lock:
        data = FINNHUB_STATE.prices.get(ticker)
        if data is None:
            return None
        return float(data["price"]) if data.get("price") else None


__all__ = [
    "FINNHUB_STATE",
    "start_finnhub_stream",
    "request_subscribe",
    "fetch_realtime_quote",
    "get_latest_price",
]
