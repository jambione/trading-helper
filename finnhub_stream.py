"""
finnhub_stream.py — Real-time stock data via Finnhub WebSocket
Supports dynamic ticker subscriptions while the stream is live.
"""

import asyncio
import json
import logging
import os
import queue as _queue
import threading
import time
from datetime import datetime
from collections import deque
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)


# ── Shared State ─────────────────────────────────────────────

# rolling per-symbol price history: keep ~6 min, subsampled to ~1 point/s.
# Lets a consumer that subscribes a watchlist ahead of time (e.g. the L2
# monitor) seed a symbol's trend the instant it switches to it, instead of
# warming up blind for minutes. Subsampling caps memory to ~360 pts/symbol.
_HIST_SECS    = 360.0
_HIST_MIN_GAP = 1.0


class FinnhubState:
    def __init__(self):
        self.lock       = threading.Lock()
        self.prices     = {}           # ticker → {price, volume, timestamp, updated}
        self.subscribed = set()        # currently subscribed tickers
        self.connected  = False
        self.last_trade = {}           # ticker → last trade data
        self.log_lines  = deque(maxlen=100)
        self.history    = {}           # ticker → deque[(ts_unix, price)]
        self._hist_last = {}           # ticker → last stored ts_unix

    def add_log(self, level: str, msg: str):
        ts = datetime.now(ET).strftime("%H:%M:%S")
        with self.lock:
            self.log_lines.append({"ts": ts, "level": level, "msg": msg})

    def update_price(self, ticker: str, price: float, volume: int = 0, timestamp: int = 0):
        """Record a price. `timestamp` is the trade's OWN time in ms (stream
        trades have one; REST quotes do not) and is kept separately from
        ts_unix.

        ts_unix is when WE learned the price, which is the right basis for
        "is this source still covering the symbol". trade_ts is when the print
        happened, which is the only honest basis for "how old is this number".
        Collapsing the two made a 30s REST re-fetch of an unchanged price read
        as a 3-second-old tick.
        """
        now = time.time()
        trade_ts = (timestamp / 1000.0) if timestamp and timestamp > 0 else None
        # A clock-skewed or malformed stamp is worse than none: it would claim
        # a print from the future and win every merge.
        if trade_ts is not None and not (0 < trade_ts <= now + 5):
            trade_ts = None
        with self.lock:
            self.prices[ticker] = {
                "price":     price,
                "volume":    volume,
                "timestamp": timestamp,
                "trade_ts":  trade_ts,
                "ts_unix":   now,
                "updated":   datetime.now(ET).strftime("%H:%M:%S"),
            }
            # subsampled rolling history for trend seeding
            if now - self._hist_last.get(ticker, 0.0) >= _HIST_MIN_GAP:
                dq = self.history.get(ticker)
                if dq is None:
                    # new symbol: opportunistically evict any that went quiet
                    # (unsubscribed/rotated off) so the dict tracks only live
                    # names instead of every symbol seen all session
                    if len(self.history) > 64:
                        for k in [k for k, v in self.history.items()
                                  if not v or now - v[-1][0] > _HIST_SECS]:
                            self.history.pop(k, None)
                            self._hist_last.pop(k, None)
                    dq = self.history[ticker] = deque(maxlen=512)
                dq.append((now, price))
                self._hist_last[ticker] = now
                while dq and now - dq[0][0] > _HIST_SECS:
                    dq.popleft()

    def recent_prices(self, ticker: str, seconds: float) -> list:
        """(ts_unix, price) points for `ticker` within the last `seconds`.
        Empty if the symbol was never streamed (nothing to seed from)."""
        cut = time.time() - seconds
        with self.lock:
            dq = self.history.get(ticker.upper())
            return [(t, p) for (t, p) in dq if t >= cut] if dq else []

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
_pending_unsubs: _queue.Queue = _queue.Queue()

# Optional per-trade callbacks. Each is invoked fn(symbol, price, volume, ts_ms)
# on every trade — used by realtime_bars to aggregate live OHLCV bars. Kept
# separate from FINNHUB_STATE so subscribers see individual trades, not just the
# collapsed last price.
_trade_callbacks: list = []


def register_trade_callback(fn):
    """Register fn(symbol, price, volume, ts_ms), called on every incoming trade."""
    _trade_callbacks.append(fn)


# ── Dynamic subscription ──────────────────────────────────────

def request_subscribe(tickers: list):
    """Queue new tickers for subscription (thread-safe, works while stream runs)."""
    with FINNHUB_STATE.lock:
        already = set(FINNHUB_STATE.subscribed)
    for t in tickers:
        t = t.upper()
        if t not in already:
            _pending_subs.put(t)


def request_unsubscribe(tickers: list):
    """Queue tickers to unsubscribe (thread-safe). Frees a slot toward the
    free-tier 50-symbol cap so rotating watchlists don't starve new movers."""
    for t in tickers:
        _pending_unsubs.put(t.upper())


# ── WebSocket Stream ─────────────────────────────────────────

async def _drain_pending(ws):
    """Send any queued (un)subscriptions to the live WebSocket."""
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
    while not _pending_unsubs.empty():
        try:
            t = _pending_unsubs.get_nowait()
            await ws.send(json.dumps({"type": "unsubscribe", "symbol": t}))
            with FINNHUB_STATE.lock:
                FINNHUB_STATE.subscribed.discard(t)
                # free the symbol's rolling history too (no longer warmed)
                FINNHUB_STATE.history.pop(t, None)
                FINNHUB_STATE._hist_last.pop(t, None)
        except _queue.Empty:
            break
        except Exception as e:
            FINNHUB_STATE.add_log("WARN", f"Unsub error for {t}: {e}")


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
                                    for cb in _trade_callbacks:
                                        try:
                                            cb(sym, price, vol, ts)
                                        except Exception:
                                            pass
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

    # Flush any stale pending (un)subs from a previous session
    for q in (_pending_subs, _pending_unsubs):
        while not q.empty():
            try:
                q.get_nowait()
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


def _key_from_engine_env(name: str) -> str:
    path = Path(__file__).resolve().parent / "signal_engine.env"
    if not path.exists():
        return ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def engine_finnhub_key() -> str:
    """The key the signal engine will use for its trade socket. Never log it."""
    for name in ("FINNHUB_API_KEY_ENGINE", "FINNHUB_API_KEY"):
        got = (os.getenv(name) or "").strip() or _key_from_engine_env(name)
        if got:
            return got
    return ""


def dashboard_ws_collides_with_engine(dashboard_key: str) -> bool:
    """True when opening a dashboard WS would steal the engine's one connection.

    Finnhub free tier allows one socket per key. 2026-08-20 the dashboard won
    the race and the engine's aggregator saw no trades all session.
    """
    dash = (dashboard_key or "").strip()
    eng = engine_finnhub_key()
    return bool(dash and eng and dash == eng)


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
    "request_unsubscribe",
    "fetch_realtime_quote",
    "get_latest_price",
]
