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
        # Re-entrant: _drain_pending logs the cap-hit WARN via add_log while
        # holding this lock. With a plain Lock the finnhub-ws thread blocked
        # on itself and every caller of request_subscribe hung behind it.
        # That was the engine/dashboard freeze of 2026-09-23 13:51 and
        # 2026-09-24 10:20-10:43, and it fired the moment subscriptions hit
        # MAX_WS_SUBSCRIPTIONS (py-spy: add_log <- _drain_pending).
        self.lock       = threading.RLock()
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
        """Record a price. `timestamp` is the trade's OWN time in ms and is
        kept separately from ts_unix.

        Stream trades carry one. So does the REST /quote (`t`, in seconds) —
        this docstring used to say it did not, and the caller believed it and
        passed nothing, which is how price_age_sec came to be None on most
        rows and left every staleness guard failing open (fixed 8/26).

        ts_unix is when WE learned the price, which is the right basis for
        "is this source still covering the symbol". trade_ts is when the print
        happened, which is the only honest basis for "how old is this number".
        Collapsing the two made a 30s REST re-fetch of an unchanged price read
        as a 3-second-old tick.

        Refuse to displace an existing entry that already has a *newer*
        trade_ts. REST always stamps ts_unix=now on learn, so without this
        guard a re-poll of an older print overwrote a younger stream trade
        and the desk ages looked worse (rest / stale_quote).
        """
        now = time.time()
        trade_ts = (timestamp / 1000.0) if timestamp and timestamp > 0 else None
        # A clock-skewed or malformed stamp is worse than none: it would claim
        # a print from the future and win every merge.
        if trade_ts is not None and not (0 < trade_ts <= now + 5):
            trade_ts = None
        with self.lock:
            existing = self.prices.get(ticker)
            if existing is not None:
                cur_tts = existing.get("trade_ts")
                if cur_tts is not None:
                    # Keep the younger dated print. An undated incoming
                    # (REST missing `t`) must not wipe a dated stream tick.
                    if trade_ts is None or float(trade_ts) < float(cur_tts):
                        return
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

# Free-tier WebSocket ceiling. Dynamic (un)subscribe rotates under this;
# overflowing silently means no trades / no forming bars for overflow names.
# Override with FINNHUB_MAX_SUBSCRIPTIONS if the account tier changes.
# Default 40, not the tier's 50: at 50 subscriptions the dashboard hung
# (2026-09-23 13:51, 2026-09-24 10:20 and again within 40s of a restart).
# At 42-43 it ran all morning. The env override is read at import, before
# dashboard.py loads signal_engine.env, so setting it there does nothing;
# the default is the value that actually binds.
MAX_WS_SUBSCRIPTIONS = int(os.getenv("FINNHUB_MAX_SUBSCRIPTIONS", "40"))

# Watch/book/seed symbols — never evict these for non-book rotation.
# Updated by the engine/dashboard from the live entry_watch / entry_book set
# (book-wide; never a hardcoded ticker list).
_PRIORITY_SYMS: set[str] = set()
_PRIORITY_LOCK = threading.Lock()


def set_subscribe_priority(tickers: list) -> None:
    """Mark symbols that must keep a Finnhub WS slot (watch/book/seed)."""
    global _PRIORITY_SYMS
    pri: set[str] = set()
    for raw in tickers or []:
        t = str(raw or "").upper().strip()
        if t:
            pri.add(t)
    with _PRIORITY_LOCK:
        _PRIORITY_SYMS = pri


def get_subscribe_priority() -> set[str]:
    with _PRIORITY_LOCK:
        return set(_PRIORITY_SYMS)


def request_subscribe(tickers: list):
    """Queue new tickers for Finnhub WS subscription (thread-safe).

    Respects ``MAX_WS_SUBSCRIPTIONS`` (default 50, free-tier ceiling). Symbols
    that would overflow are NOT queued — logged via ``FINNHUB_STATE.add_log`` —
    so overflow is loud instead of silent. Prefer ``request_unsubscribe`` to
    free slots before rotating new names in.

    Priority (watch/book/seed) symbols: if the cap would skip them, drop
    non-priority subscribers first so MOVE-on-book is never starved by desk
    noise. Non-priority overflow still skips loudly.
    """
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in tickers or []:
        t = str(raw or "").upper().strip()
        if not t or t in seen:
            continue
        seen.add(t)
        wanted.append(t)
    if not wanted:
        return

    priority = get_subscribe_priority()
    with FINNHUB_STATE.lock:
        already = set(FINNHUB_STATE.subscribed)
    # Count in-flight queue depth so a burst of callers cannot silent-overflow
    # before _drain_pending runs. May briefly overcount under race; safer than
    # undercounting past the broker ceiling.
    pending = _pending_subs.qsize()
    room = max(0, MAX_WS_SUBSCRIPTIONS - len(already) - pending)

    need = [t for t in wanted if t not in already]
    if not need:
        return

    # Free non-priority slots when priority names would otherwise be skipped.
    pri_need = [t for t in need if t in priority]
    if pri_need and room < len(pri_need):
        victims = [
            s for s in already
            if s not in priority and s not in seen
        ]
        # Prefer quiet rotation victims; order is arbitrary among non-priority.
        free_n = min(len(victims), len(pri_need) - room)
        for v in victims[:free_n]:
            _pending_unsubs.put(v)
            already.discard(v)
        room = max(0, MAX_WS_SUBSCRIPTIONS - len(already) - pending)

    queued = 0
    skipped: list[str] = []
    # Prefer priority names when room is still scarce.
    ordered = [t for t in need if t in priority] + [t for t in need if t not in priority]
    for t in ordered:
        if queued >= room:
            skipped.append(t)
            continue
        _pending_subs.put(t)
        queued += 1
    if skipped:
        sample = ",".join(skipped[:8])
        if len(skipped) > 8:
            sample += "…"
        FINNHUB_STATE.add_log(
            "WARN",
            f"subscribe cap {MAX_WS_SUBSCRIPTIONS}: skipped {len(skipped)} ({sample})",
        )


def request_unsubscribe(tickers: list, *, force: bool = False):
    """Queue tickers to unsubscribe (thread-safe). Frees a slot toward the
    free-tier ~50-symbol cap so rotating watchlists don't starve new movers.

    Watch/book/seed priority symbols are kept unless ``force=True`` — never
    drop a live book name to make room for desk noise.
    """
    priority = set() if force else get_subscribe_priority()
    for t in tickers or []:
        t = str(t or "").upper().strip()
        if not t:
            continue
        if t in priority:
            continue
        _pending_unsubs.put(t)


# ── WebSocket Stream ─────────────────────────────────────────

async def _drain_pending(ws):
    """Send any queued (un)subscriptions to the live WebSocket."""
    while not _pending_subs.empty():
        try:
            t = _pending_subs.get_nowait()
            with FINNHUB_STATE.lock:
                if t in FINNHUB_STATE.subscribed:
                    continue
                if len(FINNHUB_STATE.subscribed) >= MAX_WS_SUBSCRIPTIONS:
                    FINNHUB_STATE.add_log(
                        "WARN",
                        f"subscribe cap {MAX_WS_SUBSCRIPTIONS} hit draining; drop {t}",
                    )
                    continue
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
            # open_timeout 30s: on degraded Wi‑Fi the handshake often takes
            # 6–10s; the websockets default (~10s) then flaps connected=False
            # and the desk freezes on stale_quote with SOCKET DOWN.
            # ping_timeout 60s: same path was killing live sockets with
            # "keepalive ping timeout" while HTTPS still worked (2026-09-16).
            async with websockets.connect(
                url, ping_interval=30, ping_timeout=60, open_timeout=30,
            ) as ws:
                reconnect_attempts    = 0
                FINNHUB_STATE.connected = True
                # Re-subscribe the live set on EVERY connect. Engine starts
                # with tickers=[] and fills via the pending queue; a reconnect
                # that only used the original empty list left connected=True
                # with zero wire subscriptions (2026-09-16 stale_quote storm).
                with FINNHUB_STATE.lock:
                    want = {
                        str(t or "").upper().strip()
                        for t in list(FINNHUB_STATE.subscribed) + list(tickers or [])
                        if str(t or "").strip()
                    }
                    FINNHUB_STATE.subscribed = set()
                to_sub = sorted(want)[:MAX_WS_SUBSCRIPTIONS]
                FINNHUB_STATE.add_log(
                    "INFO", f"Finnhub connected — subscribing {len(to_sub)} tickers"
                )
                print(f"[FH] connected — subscribing {len(to_sub)} tickers", flush=True)

                for ticker in to_sub:
                    await ws.send(json.dumps({"type": "subscribe", "symbol": ticker}))
                    with FINNHUB_STATE.lock:
                        FINNHUB_STATE.subscribed.add(ticker)

                # Receive loop: 1-second timeout lets us drain pending subs.
                # Silence kill: flaky Wi‑Fi leaves a half-open socket that
                # still reports connected=True while no trades arrive — the
                # desk then freezes on stale_quote (2026-09-16: ages ~180s+
                # with subscribed=28, fresh=0). Break out and reconnect.
                last_msg_at = time.time()
                silence_limit = float(os.getenv("FINNHUB_WS_SILENCE_SEC", "45") or 45)
                while True:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        last_msg_at = time.time()
                    except asyncio.TimeoutError:
                        # Only silence-kill once we actually have subscriptions.
                        # Engine starts the socket with tickers=[] and enqueues
                        # names later — an empty socket is quiet by design.
                        with FINNHUB_STATE.lock:
                            n_sub = len(FINNHUB_STATE.subscribed)
                        if (
                            silence_limit > 0
                            and n_sub > 0
                            and (time.time() - last_msg_at) >= silence_limit
                        ):
                            raise RuntimeError(
                                f"Finnhub WS silent >{silence_limit:.0f}s "
                                f"with {n_sub} subs — reconnecting"
                            )
                        before = n_sub
                        await _drain_pending(ws)
                        # New subs deserve a fresh silence window — thin names
                        # may not print in the first seconds after subscribe.
                        with FINNHUB_STATE.lock:
                            after = len(FINNHUB_STATE.subscribed)
                        if after > before:
                            last_msg_at = time.time()
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
            msg = f"Finnhub error ({reconnect_attempts}/{max_reconnect}): {str(e)[:120]}"
            print(f"[FH] {msg}", flush=True)
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
                    f"{msg}. Reconnecting in {wait:.0f}s…"
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
    """Fetch current quote from Finnhub REST API (fallback for non-streamed tickers).

    ``t`` is the quote's OWN unix time in seconds, and dropping it was the
    root cause of every staleness guard on the desk failing open: with no
    trade time the price merge published ``price_age_sec = None``, and
    ai_positions._fresh_tape_px, ai_entry_watch._row_tape_stale and the
    blind-book flatten all treat unknown age as fresh. On 2026-08-26 that
    field was None on all nine live names and absent on 53% of RTH rows,
    and `stale_quote` had blocked 0 of 17,585 of them.

    Returned in seconds exactly as Finnhub sends it; callers convert. 0 when
    absent, which update_price already reads as "unknown" rather than epoch.
    """
    try:
        import urllib.request
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={api_key}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            return {
                "ok": True, "ticker": ticker,
                "c": data.get("c", 0), "h": data.get("h", 0),
                "l": data.get("l", 0), "o": data.get("o", 0),
                "pc": data.get("pc", 0), "t": data.get("t", 0),
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


def get_latest_print(ticker: str) -> "dict | None":
    """Return stream last with age for Phase B arm/stop/limit anchors.

    Keys: ``price``, ``trade_ts``, ``ts_unix``, ``age_sec``. Age prefers
    ``trade_ts`` (when the print happened) over ``ts_unix`` (when we learned
    it). None when the symbol has no stream price.
    """
    sym = str(ticker or "").upper().strip()
    if not sym:
        return None
    now = time.time()
    with FINNHUB_STATE.lock:
        data = FINNHUB_STATE.prices.get(sym)
        if data is None or not data.get("price"):
            return None
        try:
            px = float(data["price"])
        except (TypeError, ValueError):
            return None
        if px <= 0:
            return None
        trade_ts = data.get("trade_ts")
        ts_unix = data.get("ts_unix")
        basis = trade_ts if trade_ts is not None else ts_unix
        age = (now - float(basis)) if basis is not None else None
        return {
            "price": px,
            "trade_ts": trade_ts,
            "ts_unix": ts_unix,
            "age_sec": age,
        }


__all__ = [
    "FINNHUB_STATE",
    "MAX_WS_SUBSCRIPTIONS",
    "start_finnhub_stream",
    "request_subscribe",
    "request_unsubscribe",
    "set_subscribe_priority",
    "get_subscribe_priority",
    "fetch_realtime_quote",
    "get_latest_price",
    "get_latest_print",
]
