"""
realtime_bars.py — Build live OHLCV bars from the Finnhub trade stream.

The signal engine fetches historical bars from Alpaca for indicator warmup, but
those are delayed and only "complete" when the minute closes. TradingView, by
contrast, updates the *forming* candle tick-by-tick — which is why its
indicators move in real time and the engine's lag behind.

This aggregator closes that gap. The Finnhub WebSocket already streams trades
(price, volume, timestamp) — finnhub_stream.py currently collapses them to a
last price. Feed those same trades here and it maintains, per ticker:

  • a rolling buffer of sealed 1-minute bars (seeded from Alpaca history), and
  • a live forming bar with true open/high/low/close/volume from the ticks.

get_bars() returns history + the forming bar as one DataFrame, ready to drop
into the same indicator functions — so the engine can compute CM RSI-2 / %R /
MACD on a candle that updates every tick, like the chart you watch.

Thread-safe: the Finnhub stream runs on its own thread.
"""

from __future__ import annotations

import threading
import time

import pandas as pd

# Default rolling history kept per ticker (bars). 300 ≫ the slowest indicator
# warmup (%R slow = 112), so indicators stay stable.
DEFAULT_MAXLEN = 300
# Longest silence a ticker's history may span and still be one series.
DEFAULT_MAX_GAP_S = 900.0


def _epoch_minute(ts_ms: int) -> int:
    """Floor a millisecond epoch timestamp to its minute bucket."""
    return int(ts_ms) // 60_000


def _iso_minute(minute: int) -> str:
    return pd.Timestamp(minute * 60, unit="s", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_age_sec(stamp, now_s: float | None = None) -> float | None:
    """Seconds from a bar row's start time to now; None when unreadable."""
    if stamp is None or stamp == "":
        return None
    try:
        t = pd.Timestamp(stamp)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        now = time.time() if now_s is None else float(now_s)
        return max(0.0, now - t.timestamp())
    except (ValueError, TypeError):
        return None


class _Bar:
    __slots__ = ("minute", "open", "high", "low", "close", "volume")

    def __init__(self, minute: int, price: float, volume: float):
        self.minute = minute
        self.open = self.high = self.low = self.close = price
        self.volume = volume

    def update(self, price: float, volume: float):
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        self.close = price
        self.volume += volume

    def as_row(self) -> dict:
        return {
            "time": _iso_minute(self.minute),
            "open": self.open, "high": self.high,
            "low": self.low, "close": self.close,
            "volume": self.volume,
        }


class RealtimeBarAggregator:
    """
    Per-ticker realtime 1-minute bar builder.

    Typical use:
        agg = RealtimeBarAggregator()
        agg.seed("NVDA", historical_df)        # warmup from Alpaca
        agg.on_trade("NVDA", 120.5, 100, ts)   # called from the Finnhub handler
        df = agg.get_bars("NVDA")              # sealed bars + live forming bar
    """

    def __init__(self, maxlen: int = DEFAULT_MAXLEN, max_gap_sec: float | None = DEFAULT_MAX_GAP_S):
        self._maxlen = maxlen
        # A trade arriving this long after the last bar we hold cannot extend
        # that history: the minutes between were never seen, and the rows are
        # counted, not clocked. The old rows are dropped so the caller
        # re-seeds from fresh bars. None keeps the old splice-anything rule.
        self._max_gap_sec = max_gap_sec
        self._lock = threading.Lock()
        self._sealed: dict[str, list[dict]] = {}   # ticker → list of bar rows (oldest→newest)
        self._forming: dict[str, _Bar] = {}        # ticker → current forming bar
        # ts_ms of the newest trade folded in, per ticker. Without this the
        # aggregator cannot tell a live stream from a dead one: sealed history
        # and the forming bar both survive a disconnect, so get_bars() keeps
        # returning a full-looking frame whose newest bar is silently ageing.
        self._last_ts: dict[str, int] = {}
        # The price of that same newest trade. Kept beside the timestamp, and
        # written only when the timestamp actually advances, so the two are
        # always the same event. `last_trade()` hands them out as one object
        # for exactly that reason: the desk publishes a price for the merge to
        # date, and dating a price with a clock taken from somewhere else is
        # how a frozen quote comes to look 0.3s old.
        self._last_px: dict[str, float] = {}

    # ── Seeding ───────────────────────────────────────────────────────────────

    def seed(self, ticker: str, df: pd.DataFrame):
        """
        Seed a ticker's sealed history from an Alpaca/Massive bar DataFrame
        (columns: time, open, high, low, close, volume). Idempotent-ish: replaces
        any existing sealed history but preserves a live forming bar.
        """
        if df is None or len(df) == 0:
            return
        rows = []
        for _, r in df.tail(self._maxlen).iterrows():
            rows.append({
                "time":  r.get("time", ""),
                "open":  float(r["open"]), "high": float(r["high"]),
                "low":   float(r["low"]),  "close": float(r["close"]),
                "volume": float(r.get("volume", 0.0)),
            })
        with self._lock:
            self._sealed[ticker] = rows

    def sealed_count(self, ticker: str) -> int:
        """Number of sealed (closed) bars for ticker — excludes forming."""
        with self._lock:
            return len(self._sealed.get(ticker, []))

    def is_seeded(self, ticker: str, *, min_bars: int = 1,
                  max_gap_sec: float | None = None, now_s: float | None = None) -> bool:
        """True when sealed history has at least ``min_bars`` bars.

        Default min_bars=1 preserves the historical "any seed" meaning.
        Callers that need MACD-stable warmup pass min_bars=MACD_SLOW+MACD_SIG+5
        (40) so a truncated seed is treated as not ready and can be re-seeded.

        With ``max_gap_sec``, the newest sealed bar must also be that recent.
        Sealed history is a list of rows, not a clock: a ticker dropped at
        08:52 and re-added at 12:13 kept its 08:37 seed, the new minutes were
        appended after it, and "%R(112)" read yesterday's bars plus three new
        ones (FLY 2026-09-30: -9.7 where the tape said -71). Stale history is
        not a seed; the caller re-seeds from fresh bars.
        """
        need = max(1, int(min_bars or 1))
        with self._lock:
            rows = self._sealed.get(ticker, [])
            if len(rows) < need:
                return False
            newest = rows[-1].get("time") if rows else None
        if max_gap_sec is None:
            return True
        age = _row_age_sec(newest, now_s)
        return age is not None and age <= float(max_gap_sec)

    # ── Live trades ─────────────────────────────────────────────────────────────

    def on_trade(self, ticker: str, price: float, volume: float, ts_ms: int):
        """Fold one trade into the forming bar, sealing the prior bar on rollover."""
        if price <= 0:
            return
        minute = _epoch_minute(ts_ms)
        with self._lock:
            # Newest wins: an out-of-order print from an already-sealed minute
            # is dropped below, but it still proves the stream is alive, and it
            # must never drag the freshness clock backwards.
            if int(ts_ms) >= self._last_ts.get(ticker, -1):
                self._last_ts[ticker] = int(ts_ms)
                self._last_px[ticker] = float(price)
            cur = self._forming.get(ticker)
            if self._max_gap_sec is not None:
                last_min = cur.minute if cur is not None else None
                if last_min is None:
                    rows = self._sealed.get(ticker)
                    age = _row_age_sec(rows[-1].get("time"), minute * 60) if rows else None
                    last_min = None if age is None else minute - age / 60.0
                if last_min is not None and (minute - last_min) * 60 > self._max_gap_sec:
                    self._sealed[ticker] = []        # discontinuous: never splice
                    self._forming[ticker] = _Bar(minute, price, volume)
                    return
            if cur is None:
                self._forming[ticker] = _Bar(minute, price, volume)
                return
            if minute > cur.minute:
                # New minute → seal the old forming bar into history.
                self._sealed.setdefault(ticker, []).append(cur.as_row())
                if len(self._sealed[ticker]) > self._maxlen:
                    self._sealed[ticker] = self._sealed[ticker][-self._maxlen:]
                self._forming[ticker] = _Bar(minute, price, volume)
            elif minute == cur.minute:
                cur.update(price, volume)
            # trades from an already-sealed earlier minute are ignored (out of order)

    # ── Read ────────────────────────────────────────────────────────────────────

    def get_bars(self, ticker: str, include_forming: bool = True) -> pd.DataFrame | None:
        """
        Return sealed bars (+ the live forming bar) as a DataFrame ready for the
        indicator functions, or None if the ticker is unknown.
        """
        with self._lock:
            sealed = list(self._sealed.get(ticker, []))
            forming = self._forming.get(ticker)
            rows = sealed
            if include_forming and forming is not None:
                # Don't duplicate if the forming bar's minute already sealed.
                if not rows or rows[-1]["time"] != _iso_minute(forming.minute):
                    rows = rows + [forming.as_row()]
        if not rows:
            return None
        return pd.DataFrame(rows).reset_index(drop=True)

    def forming_bar(self, ticker: str) -> dict | None:
        with self._lock:
            b = self._forming.get(ticker)
            return b.as_row() if b else None

    # ── Freshness ───────────────────────────────────────────────────────────────

    def last_trade_ms(self, ticker: str) -> int | None:
        """ts_ms of the newest trade folded in, or None if never fed."""
        with self._lock:
            return self._last_ts.get(ticker)

    def last_trade(self, ticker: str) -> tuple[float, int] | None:
        """(price, ts_ms) of the newest trade, or None if never fed.

        One lock, one event. This exists so a consumer cannot pair a price
        from here with an age from anywhere else: the desk publishes both to
        the dashboard's price merge, and that merge decides which feed wins on
        recency. A price carrying a borrowed clock wins races it should lose.
        """
        with self._lock:
            ts = self._last_ts.get(ticker)
            px = self._last_px.get(ticker)
        if ts is None or px is None:
            return None
        return px, ts

    def age_seconds(self, ticker: str, now_ms: float | None = None) -> float | None:
        """Seconds since this ticker's newest trade, or None if never fed.

        None means "no realtime data at all" — a seeded-but-unfed ticker, or one
        that has never traded. Callers must treat None as unusable, not fresh:
        seed() fills sealed history, so get_bars() returns a healthy-looking
        frame for a ticker no trade has ever touched.

        Measured from the trade's own timestamp rather than its arrival time, so
        this also catches a stream that is connected but serving stale prints.
        """
        with self._lock:
            last = self._last_ts.get(ticker)
        if last is None:
            return None
        now_ms = time.time() * 1000.0 if now_ms is None else float(now_ms)
        return max(0.0, (now_ms - last) / 1000.0)
