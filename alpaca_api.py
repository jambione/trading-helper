from datetime import datetime, timedelta, timezone
from typing import Optional, Mapping, Any
import os
import random
import threading
import time
import logging

import pandas as pd

_SORT_WARNED = False

# Process-wide pacing for Alpaca data REST. Free-tier IEX 429s when many
# symbols warm/refresh bars in parallel; a floor between requests stops the
# stampede without needing paid SIP.
_ALPACA_MIN_INTERVAL_S = float(os.getenv("ALPACA_MIN_INTERVAL_S", "0.35"))
_throttle_lock = threading.Lock()
_throttle_next_ok = 0.0  # monotonic

# Coalesce identical 429 warnings so one storm is one line per backoff window.
_warn_lock = threading.Lock()
_last_429_warn_key = ""
_last_429_warn_mono = 0.0
_429_WARN_GAP_S = 5.0


def parse_retry_after(headers: Mapping[str, Any] | None) -> Optional[float]:
    """Seconds from a Retry-After header, or None if absent/unusable."""
    if not headers:
        return None
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        raw = None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def retry_after_from_exc(exc: BaseException) -> Optional[float]:
    """Best-effort Retry-After from an Alpaca/HTTP exception."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        headers = getattr(resp, "headers", None)
        wait = parse_retry_after(headers)
        if wait is not None:
            return wait
    # Some SDK errors only put the number in the message.
    msg = str(exc)
    low = msg.lower()
    if "retry-after" in low or "retry after" in low:
        import re
        m = re.search(r"retry[- ]after[=:\s]+(\d+(?:\.\d+)?)", low)
        if m:
            try:
                return max(0.0, float(m.group(1)))
            except ValueError:
                pass
    return None


def backoff_seconds(
    attempt: int,
    *,
    base_wait: float = 1.0,
    retry_after: Optional[float] = None,
    cap: float = 60.0,
) -> float:
    """Retry-After if present, else exponential backoff with full jitter."""
    if retry_after is not None and retry_after > 0:
        # Small jitter so concurrent waiters don't re-stampede on the same second.
        return min(cap, float(retry_after) + random.uniform(0.0, 0.25))
    exp = base_wait * (2 ** max(0, attempt))
    return min(cap, random.uniform(0.0, exp))


def warn_429(where: str, wait: float, detail: str = "") -> None:
    """Log a 429 at warning, coalesced so identical lines don't flood."""
    global _last_429_warn_key, _last_429_warn_mono
    key = f"{where}|{wait:.1f}|{detail[:40]}"
    now = time.monotonic()
    with _warn_lock:
        if key == _last_429_warn_key and (now - _last_429_warn_mono) < _429_WARN_GAP_S:
            return
        _last_429_warn_key = key
        _last_429_warn_mono = now
    extra = f" — {detail[:120]}" if detail else ""
    logging.warning("[ALPACA] 429 %s; backing off %.1fs%s", where, wait, extra)


def throttle_alpaca_request(min_interval: Optional[float] = None) -> float:
    """Block until the process-wide Alpaca request slot is free. Returns wait."""
    global _throttle_next_ok
    interval = _ALPACA_MIN_INTERVAL_S if min_interval is None else float(min_interval)
    if interval <= 0:
        return 0.0
    with _throttle_lock:
        now = time.monotonic()
        wait = _throttle_next_ok - now
        if wait > 0:
            _throttle_next_ok = _throttle_next_ok + interval
        else:
            wait = 0.0
            _throttle_next_ok = now + interval
    if wait > 0:
        time.sleep(wait)
    return wait


def reset_alpaca_throttle_for_tests() -> None:
    """Test helper — clear pacing / warn coalescing state."""
    global _throttle_next_ok, _last_429_warn_key, _last_429_warn_mono
    with _throttle_lock:
        _throttle_next_ok = 0.0
    with _warn_lock:
        _last_429_warn_key = ""
        _last_429_warn_mono = 0.0


def retry_with_backoff(max_retries: int = 3, base_wait: float = 1.0):
    """Decorator for Alpaca API calls — Retry-After / exponential+jitter on 429/503."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    throttle_alpaca_request()
                    return func(*args, **kwargs)
                except Exception as e:
                    err = str(e)
                    retryable = (
                        "429" in err
                        or "503" in err
                        or "rate" in err.lower()
                        or "service unavailable" in err.lower()
                    )
                    if retryable and attempt < max_retries - 1:
                        wait = backoff_seconds(
                            attempt,
                            base_wait=base_wait,
                            retry_after=retry_after_from_exc(e),
                        )
                        if "429" in err or "rate" in err.lower():
                            warn_429(func.__name__, wait, err)
                        else:
                            logging.warning(
                                "[ALPACA] %s retry %d/%d in %.1fs — %s",
                                func.__name__, attempt + 1, max_retries, wait, err[:80],
                            )
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


# Basic-plan SIP is historical only: anything newer than ~15 minutes 403s.
# Live paths stay on IEX via `_get_feed_arg`. Research tools use
# `research_bar_end` + `research_feed_rest` so delayed SIP is opt-in.
DELAYED_SIP_LAG = timedelta(minutes=15)


def _get_feed_arg(cfg: dict = None) -> dict:
    """IEX by default. ``alpaca_bar_feed=sip`` when the account has live SIP."""
    name = "iex"
    if isinstance(cfg, dict):
        raw = cfg.get("alpaca_bar_feed") or cfg.get("alpaca_data_feed") or "iex"
        name = str(raw).strip().lower()
    try:
        from alpaca.data.enums import DataFeed as _DF
        if name == "sip":
            return {"feed": _DF.SIP}
        return {"feed": _DF.IEX}
    except Exception:
        return {}


def normalize_research_feed(feed) -> str:
    """`iex` or `sip`. Live code must not call this for latest-trade/stream."""
    name = str(feed or "iex").strip().lower().replace("-", "_")
    if name in ("sip", "delayed_sip"):
        return "sip"
    if name in ("iex", ""):
        return "iex"
    raise ValueError(f"unknown research feed {feed!r} (use iex or sip)")


def research_feed_rest(feed) -> str:
    """Alpaca REST `feed=` value for historical bar requests."""
    return normalize_research_feed(feed)


def research_bar_end(feed, *, now=None, requested_end=None) -> datetime:
    """Latest UTC timestamp a historical request may use on this feed.

    SIP on the free plan is delayed ~15 minutes; IEX is real-time. If
    `requested_end` is set (a finished session, a day bound), the earlier of
    that and the feed cap is returned so today's SIP fetch does not 403.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    cap = now - DELAYED_SIP_LAG if normalize_research_feed(feed) == "sip" else now
    if requested_end is None:
        return cap
    end = requested_end
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    else:
        end = end.astimezone(timezone.utc)
    return min(end, cap)


def _get_sort_desc_arg() -> dict:
    """Newest-first request ordering, when the SDK exposes it.

    `limit` truncates from whichever end the sort starts at. Ascending — the
    API default — therefore returns the OLDEST `limit` bars in the lookback
    window, not the newest: with a 10-day start and limit=90, a 1Min request
    for AAPL came back holding 90 bars from 2026-08-03, eight days stale, and
    every caller consumed them as current. signal_engine.fetch_bars already
    carries this fix; this path did not.

    An SDK without Sort would silently resume serving week-old bars, which is
    the exact failure this exists to stop — so say so, once, rather than
    degrade quietly.
    """
    global _SORT_WARNED
    try:
        from alpaca.common.enums import Sort as _Sort
        return {"sort": _Sort.DESC}
    except Exception:
        if not _SORT_WARNED:
            _SORT_WARNED = True
            logging.error(
                "[ALPACA] SDK has no Sort enum — bar requests fall back to "
                "ascending, which returns the OLDEST bars in the window. "
                "Indicators and entry zones will be stale.")
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
            extended_hours=True,
            **_get_feed_arg(cfg),
            **_get_sort_desc_arg(),
        )
        bars = data_client.get_stock_bars(req).df
        if bars is None or bars.empty:
            return None
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(ticker, level="symbol")
        # Flipped back to oldest-first: every consumer downstream (.tail(),
        # rolling windows, indicator series) reads these chronologically.
        bars = bars.sort_index()
        bars = bars[["open", "high", "low", "close", "volume"]].copy()
        bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
        return bars.dropna(subset=["close"])

    try:
        return _fetch()
    except Exception:
        return None


def fetch_bars_batch(data_client, tickers: list, cfg: dict) -> dict:
    """NOTE: `limit` on a multi-symbol request caps the WHOLE batch, not each
    symbol — the SDK stops paginating once the total is reached, so the tail
    of `tickers` comes back empty (see rs_fetch.py's header). Newest-first
    ordering below at least makes the bars that do arrive current. Unused
    today; fix the limit before adopting it."""
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
            extended_hours=True,
            **_get_feed_arg(cfg),
            **_get_sort_desc_arg(),
        )
        bars = data_client.get_stock_bars(req).df
        if bars is None or bars.empty:
            return {}

        results = {}
        if isinstance(bars.index, pd.MultiIndex):
            for t in tickers:
                try:
                    df = bars.xs(t, level="symbol").sort_index()[
                        ["open", "high", "low", "close", "volume"]].copy()
                    df["close"] = pd.to_numeric(df["close"], errors="coerce")
                    df = df.dropna(subset=["close"])
                    if not df.empty:
                        results[t] = df
                except KeyError:
                    continue
        else:
            bars = bars.sort_index()[
                ["open", "high", "low", "close", "volume"]].copy()
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
        from alpaca.data.requests import StockLatestTradeRequest
        resp = data_client.get_stock_latest_trade(StockLatestTradeRequest(
            symbol_or_symbols=ticker,
            **_get_feed_arg(cfg)
        ))
        trade = resp.get(ticker)
        return float(trade.price) if trade else None

    try:
        return _fetch()
    except Exception:
        return None


def get_latest_trade_quotes(data_client, tickers: list, cfg: dict = None) -> dict:
    """{ticker: (price, trade_unix_ts)} for the latest trade on each symbol.

    Same request as get_latest_trade_prices, but keeps the trade's own
    timestamp. That is the real observation time — the moment the print
    happened — as opposed to when we fetched it, which is what a poll-time
    stamp records. Consumers that need to know how stale a price is cannot
    work without it.

    Falls back to 0.0 for the timestamp when the SDK does not supply one, so
    callers must treat 0.0 as "unknown age", never as "epoch".
    """
    cfg = cfg or {}
    if not tickers or data_client is None:
        return {}

    @retry_with_backoff(max_retries=3, base_wait=1.0)
    def _fetch():
        from alpaca.data.requests import StockLatestTradeRequest
        resp = data_client.get_stock_latest_trade(StockLatestTradeRequest(
            symbol_or_symbols=tickers,
            **_get_feed_arg(cfg)
        ))
        results = {}
        for t in tickers:
            trade = resp.get(t)
            price = getattr(trade, "price", None) if trade is not None else None
            if price is None:
                continue
            ts = getattr(trade, "timestamp", None)
            obs = getattr(ts, "timestamp", None)
            try:
                results[t] = (float(price), float(obs()) if obs else 0.0)
            except (TypeError, ValueError):
                continue
        return results

    try:
        return _fetch()
    except Exception as e:                                 # noqa: BLE001
        # Returning {} silently made a dead feed indistinguishable from a quiet
        # market: the panel kept showing whatever the other source last said,
        # with no error anywhere, so prices simply stopped moving and nothing
        # explained why. Same swallow that hid the minute-bar 401.
        logging.warning("[ALPACA] latest trades failed for %d symbol(s): %s",
                    len(tickers), " ".join(str(e).split())[:200])
        return {}


def get_latest_trade_prices(data_client, tickers: list, cfg: dict = None) -> dict:
    """{ticker: price} for the latest trade on each symbol.

    Thin projection of get_latest_trade_quotes so the request, the parsing and
    the error handling exist once. Prefer the quotes form: without the trade
    timestamp there is no way to tell a live print from a stale one.
    """
    return {t: p for t, (p, _) in
            get_latest_trade_quotes(data_client, tickers, cfg).items()}
