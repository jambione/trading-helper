#!/usr/bin/env python3
"""
rs_fetch.py — split-adjusted daily bars from Alpaca, paced for the free tier.

The only module in the RS screener that talks to Alpaca. rs_cache.py stores what
this returns; rs_core.py does the math; rs_screener.py owns the policy.

TWO SDK TRAPS THIS MODULE CODES AROUND
--------------------------------------
1. `adjustment` defaults to None, which the API serves as RAW.

   Measured against live data (300 symbols, 400 days, both ways): 7 of 300 —
   2.3% — had a single-day raw gap above 1.8x that split adjustment removed.
   GVH shows a 189x one-day raw move that is 3.96x adjusted. Extrapolated over
   ~13k symbols that is roughly 300 names whose 12-month return would be wildly
   wrong, and they land at the extremes of the percentile: the top of the
   ranked output. Adjustment.SPLIT is not optional here.

   Not Adjustment.ALL, tempting though it is: every dividend also restates all
   prior bars, so an ALL cache needs repair on every ex-div date across the
   whole universe — a permanent daily re-full of a large slice of it. SPLIT
   restates only on splits, a handful a day. IBD measures price performance
   anyway; the cost is that these are price returns, not total returns.

2. NEVER set `limit` on a multi-symbol bars request.

   alpaca/common/rest.py:383-390 only paginates while `limit` is unset:

       if limit:
           actual_limit = min(int(limit) - total_items, page_limit)
           if actual_limit < 1:
               break

   So `limit=10000` does not mean "10,000 bars per symbol", it means the whole
   batch stops at 10,000 bars total — the first ~40 of 100 symbols, silently.
   With `limit=None` the SDK pages until next_page_token runs out.
   (alpaca_api.fetch_bars_batch:100-107 has exactly this bug today. It is
   currently unused, so it is a separate ticket, not this change.)

RATE LIMITS
-----------
The free tier allows 200 requests/min, but the SDK auto-paginates inside one
`get_stock_bars` call, so one "request" from here can be eight HTTP pages.
Measured: 77,762 rows came back in 2.0 s, about 4 pages/sec — an unthrottled
back-to-back run sustains ~240/min and would start collecting 429s.

We budget 100/min by default, not 200, because the ceiling is per ACCOUNT and
signal_engine.py, dashboard.py and trade_bridge share the key. A backfill at
full tilt can starve the live trading engine of price data.

Measured on a 1,505-symbol slice: a cold backfill cost 63 pages in 22 s, a warm
daily refresh 7 pages in 8 s. Projected to the full ~13,291-symbol universe that
is ~560 pages (6-10 min, throttle-bound) cold and ~45 requests (~1 min) warm.
"""

from __future__ import annotations

import collections
import logging
import math
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

import alpaca_api

log = logging.getLogger("rs.fetch")

# alpaca/common/constants.py:3 — DATA_V2_MAX_LIMIT
PAGE_CAP = 10_000

DEFAULT_CHUNK = 100
DEFAULT_MAX_PER_MIN = 100


class FetchFailed(Exception):
    """One batch failed. Its symbols are unknown this run, NOT known-empty.

    The distinction matters: a symbol with no bars is legitimately excluded from
    the population, but a symbol we simply could not reach must be recorded as
    unfetched. Treating a 429 as "no history" quietly shrinks the population the
    percentile is computed over, and every surviving rating inflates.
    """


# ── Pure helpers ──────────────────────────────────────────────────────────────

def pages_used(bar_count: int, page_cap: int = PAGE_CAP) -> int:
    """HTTP pages the SDK actually spent to return `bar_count` bars.

    Charged to the rate budget after the fact, so pacing reflects what was
    really used rather than a guess made before the call. Always at least 1 —
    an empty response still cost a round trip.
    """
    if bar_count <= 0:
        return 1
    return max(1, math.ceil(bar_count / float(page_cap)))


def chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def chunk_size_for(sessions: int, symbols_cap: int = DEFAULT_CHUNK) -> int:
    """Symbols per request, given how many sessions each one will return.

    Note this does NOT shrink a backfill: the page cap counts bars, not symbols,
    so 13k symbols x 275 sessions is ~330 pages however it is sliced. It does cut
    the incremental path linearly, because six sessions x N symbols stays inside
    one page until N gets large.
    """
    sessions = max(1, int(sessions))
    return max(1, min(int(symbols_cap), PAGE_CAP // sessions))


# ── Rate budget ───────────────────────────────────────────────────────────────

class RateBudget:
    """Token bucket over a rolling 60-second window, in HTTP pages.

    Same deque-and-lock shape as swing_screener._finnhub_throttle:100, but
    charging a variable number of tokens after each call instead of one before,
    because a single get_stock_bars can page several times internally.
    """

    def __init__(self, max_per_min: int = DEFAULT_MAX_PER_MIN, clock=time.monotonic,
                 sleep=time.sleep):
        self.max_per_min = max(1, int(max_per_min))
        self._clock = clock
        self._sleep = sleep
        self._times: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()
        self.pages = 0

    def _prune(self, now: float) -> None:
        while self._times and now - self._times[0] >= 60.0:
            self._times.popleft()

    def wait(self) -> float:
        """Block until there is room for at least one more page. Returns the wait."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._times) < self.max_per_min:
                return 0.0
            delay = 60.0 - (now - self._times[0])
        if delay > 0:
            self._sleep(delay)
        return max(0.0, delay)

    def charge(self, pages: int) -> None:
        with self._lock:
            now = self._clock()
            self._prune(now)
            for _ in range(max(1, int(pages))):
                self._times.append(now)
            self.pages += max(1, int(pages))


# ── Client ────────────────────────────────────────────────────────────────────

_CLIENT = None


def data_client(cfg: dict):
    """Shared StockHistoricalDataClient, built once per process."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = alpaca_api.connect_data_client(cfg)
    return _CLIENT


def reset_client() -> None:
    """Tests and --rebuild drop the cached client."""
    global _CLIENT
    _CLIENT = None


def build_bars_request(symbols: list[str], start: datetime, cfg: dict | None = None,
                       adjustment: str = "split"):
    """The StockBarsRequest this module sends. Factored out so a test can assert
    on it without a network call — see the two traps in the module docstring."""
    from alpaca.data.enums import Adjustment
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    adj = {"split": Adjustment.SPLIT, "all": Adjustment.ALL}.get(str(adjustment).lower())
    if adj is None:
        # RAW is refused rather than defaulted: it is the setting that makes a
        # 12-month return meaningless, and it is also the API's default, so a
        # silent fallback would be indistinguishable from forgetting the flag.
        raise ValueError(f"refusing adjustment {adjustment!r}; use 'split' or 'all'")

    return StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=start,
        adjustment=adj,
        # limit is deliberately unset — see trap 2.
        **alpaca_api._get_feed_arg(cfg or {}),
    )


# ── Bars ──────────────────────────────────────────────────────────────────────

def _split_response(frame: pd.DataFrame, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Explode Alpaca's MultiIndex response into {symbol → dated OHLCV}.

    The DatetimeIndex is kept. swing_screener._clean_bars:189 ends in
    reset_index(drop=True) and throws it away, which is why that helper cannot
    be reused here: without dates there is no way to align a gappy symbol to
    the benchmark calendar, and every anchor silently shifts.
    """
    wanted = ["close", "high", "low", "volume"]
    out: dict[str, pd.DataFrame] = {}
    if frame is None or frame.empty:
        return out

    if isinstance(frame.index, pd.MultiIndex):
        for symbol in symbols:
            try:
                sub = frame.xs(symbol, level="symbol")
            except KeyError:
                continue
            cleaned = _clean(sub, wanted)
            if not cleaned.empty:
                out[symbol] = cleaned
    else:
        cleaned = _clean(frame, wanted)
        if not cleaned.empty:
            out[symbols[0]] = cleaned
    return out


def _clean(frame: pd.DataFrame, wanted: list[str]) -> pd.DataFrame:
    cols = [c for c in wanted if c in frame.columns]
    out = frame[cols].copy()
    out["close"] = pd.to_numeric(out.get("close"), errors="coerce")
    out = out.dropna(subset=["close"])
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.DatetimeIndex(pd.to_datetime(out.index, utc=True))
    return out.sort_index()


def fetch_chunk(symbols: list[str], start: datetime, cfg: dict,
                budget: RateBudget | None = None,
                adjustment: str = "split", client=None) -> dict[str, pd.DataFrame]:
    """One batched request. Raises FetchFailed so the caller can mark the
    symbols unfetched rather than mistaking them for having no history."""
    if not symbols:
        return {}
    client = client or data_client(cfg)
    request = build_bars_request(symbols, start, cfg, adjustment)

    if budget is not None:
        budget.wait()

    @alpaca_api.retry_with_backoff(max_retries=3, base_wait=2.0)
    def _call():
        return client.get_stock_bars(request).df

    try:
        frame = _call()
    except Exception as exc:                               # noqa: BLE001
        raise FetchFailed(f"{len(symbols)} symbols from {symbols[0]}: {exc}") from exc

    rows = 0 if frame is None else len(frame)
    if budget is not None:
        budget.charge(pages_used(rows))
    return _split_response(frame, symbols)


def fetch_daily_bars(symbols: list[str], start: date | datetime, cfg: dict,
                     budget: RateBudget | None = None, adjustment: str = "split",
                     chunk: int = DEFAULT_CHUNK, client=None,
                     on_progress=None) -> tuple[dict[str, pd.DataFrame], set[str]]:
    """Split-adjusted daily bars for many symbols.

    Returns (bars_by_symbol, unfetched). `unfetched` holds the symbols whose
    batch errored — NOT the ones that simply have no bars. The caller excludes
    unfetched symbols from the ranking population; conflating the two lets a
    429 storm shrink the population and inflate every surviving rating.
    """
    if not symbols:
        return {}, set()

    start_dt = _as_utc(start)
    out: dict[str, pd.DataFrame] = {}
    unfetched: set[str] = set()

    batches = list(chunks(list(symbols), max(1, int(chunk))))
    for i, batch in enumerate(batches, 1):
        try:
            out.update(fetch_chunk(batch, start_dt, cfg, budget, adjustment, client))
        except FetchFailed as exc:
            log.warning("[rs] batch %d/%d failed — %s", i, len(batches), exc)
            unfetched.update(batch)
        if on_progress is not None:
            try:
                on_progress(i, len(batches))
            except Exception:                              # noqa: BLE001
                pass
    return out, unfetched


def _as_utc(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def lookback_start(calendar_days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=int(calendar_days))


# ── Universe ──────────────────────────────────────────────────────────────────

def tradable_universe(cfg: dict) -> list[str]:
    """Every active, tradable US equity symbol Alpaca knows (~13,290).

    Delegates to swing_screener.all_tradable_symbols:202 rather than
    reimplementing it — same call, same process-lifetime cache. Imported lazily
    so this module does not drag in the swing screener's Finnhub throttle and
    client singleton just to be imported.

    Be honest about what comes back: Alpaca's Asset model has no is_etf flag
    (alpaca/trading/models.py:70 has only `attributes`, for PTP exceptions), so
    this list includes ETFs, leveraged ETPs, ADRs, preferreds, units and rights
    alongside common stock. rs_screener labels the population accordingly and
    strips ETPs from the served list, not from the ranking.
    """
    try:
        from swing_screener import all_tradable_symbols
        return all_tradable_symbols(cfg)
    except Exception as exc:                               # noqa: BLE001
        log.warning("[rs] tradable universe unavailable: %s", exc)
        return []
