"""market_tape.py — the market's own day move, for market-relative admission (operator 11/17).

The desk only buys names going up, and every supply source admits a name only when it is up at least N% on the
day (movers, tight, the momentum open seed). On a red tape that starves the book: on 11/17 (SPY -1%) the tight
screener found 16-21 of 400 liquid names up 1.5%, and research picks that never set up filled the seats.

`ai_relative_strength_admit` (default OFF) judges "up" against SPY instead: a name qualifies when its day move
beats SPY's by the same N%. Only red days change: the bar drops by SPY's decline (never rises on green days),
so a +0.6% name on a -1% day clears a 1.5% bar. The ENTRY rule (the presquare %R setup) is untouched.

    effective = base + min(0, spy_day_pct)        # flag on and SPY known
    effective = base                              # flag off, or SPY unknown (never guess)
"""
from __future__ import annotations

import time

SPY_TTL_SEC = 60.0
SPY_MAX_TRADE_AGE_SEC = 300.0
_cache: dict = {"ts": 0.0, "val": None}


def enabled(cfg: dict | None) -> bool:
    return bool((cfg or {}).get("ai_relative_strength_admit", False))


def effective_min_pct(cfg: dict | None, base: float, spy_pct: float | None) -> float:
    """The day-change bar a name must clear. Lower than `base` only on a red day with the flag on."""
    if not enabled(cfg) or spy_pct is None:
        return float(base)
    return float(base) + min(0.0, float(spy_pct))


def spy_day_pct(client=None, *, now: float | None = None, ttl: float = SPY_TTL_SEC) -> float | None:
    """SPY's day change in % (last trade vs prior close), cached `ttl` seconds. None when it cannot be read or
    the last trade is older than SPY_MAX_TRADE_AGE_SEC - a stale market reading is not used."""
    t = time.time() if now is None else float(now)
    if t - float(_cache["ts"]) < ttl:
        return _cache["val"]
    val = None
    try:
        if client is None:
            import ai_entry_watch as ew
            client = ew._data_client()
        import alpaca_api
        from alpaca.data.requests import StockSnapshotRequest
        got = client.get_stock_snapshot(StockSnapshotRequest(
            symbol_or_symbols="SPY", feed=alpaca_api.live_data_feed()))
        v = got["SPY"] if isinstance(got, dict) else got
        px = float(v.latest_trade.price)
        pc = float(v.previous_daily_bar.close)
        age = t - v.latest_trade.timestamp.timestamp()
        if px > 0 and pc > 0 and age <= SPY_MAX_TRADE_AGE_SEC:
            val = round((px / pc - 1.0) * 100.0, 3)
    except Exception:  # noqa: BLE001
        val = None
    _cache.update(ts=t, val=val)
    return val


def market_min_pct(cfg: dict | None, base: float, client=None) -> float:
    """effective_min_pct with SPY read only when the flag is on (no API call otherwise)."""
    if not enabled(cfg):
        return float(base)
    return effective_min_pct(cfg, base, spy_day_pct(client))
