#!/usr/bin/env python3
"""ai_catalyst_thrust.py — Strategy module for 09:30 Catalyst Open Thrust.

Captures high-magnitude momentum at inception on verified fundamental catalyst gappers
(earnings, guidance, FDA, buyout) during the 09:31-09:33 ET opening drive.
Enters at Bar 1 open immediately following a confirmed green Bar 0 (09:30-09:31 ET).
Trails risk via 1-minute prior-low ratchets without profit target capping.
Enforces a 15-minute maximum hold time stop.

Pre-registration: docs/studies/catalyst_thrust_family_prereg.json
Findings: docs/CATALYST_THRUST_FAMILY.md (+17.50 bp net, PF 1.26, median MFE +133.4 bp).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Optional

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# Default parameter bounds
DEFAULT_MIN_GAP_PCT = 8.0
DEFAULT_MIN_PRICE = 20.0
DEFAULT_MAX_LOSS_PCT = 0.020  # 2.0% hard catastrophic stop
DEFAULT_MAX_HOLD_BARS = 15     # Max 15-minute hold (900 seconds)


def compute_open_thrust_initial_stop(
    entry_price: float,
    bar0_low: float | None = None,
    max_loss_pct: float = DEFAULT_MAX_LOSS_PCT,
) -> float:
    """Computes the initial thrust stop at entry:
    Initial stop is Bar 0's low, capped at a maximum loss floor of entry_price * (1 - max_loss_pct).
    """
    if entry_price <= 0:
        return 0.0
    floor = round(entry_price * (1.0 - float(max_loss_pct)), 2)
    if bar0_low is not None and float(bar0_low) > 0:
        return round(max(float(bar0_low), floor), 2)
    return floor


def compute_open_thrust_ratchet_stop(
    entry_price: float,
    current_stop: float | None,
    recent_bars: list[dict],
    bar0_low: float | None = None,
    max_loss_pct: float = DEFAULT_MAX_LOSS_PCT,
    entry_time: float | None = None,
) -> float:
    """Computes the 1-minute prior-low trailing ratchet stop.

    Initial stop starts at max(bar0_low, entry_price * (1 - max_loss_pct)).
    On each completed 1-minute bar after entry, the stop ratchets up to max(stop, bar['low']).
    Never ratchets downward.
    """
    if entry_price <= 0:
        return 0.0

    hard_stop_floor = compute_open_thrust_initial_stop(entry_price, bar0_low, max_loss_pct)
    ratchet_stop = float(current_stop) if current_stop is not None else hard_stop_floor

    if recent_bars:
        now_ts = float(time.time())
        has_ts = any(bool(b.get("t") or b.get("ts") or b.get("time")) for b in recent_bars)
        if has_ts:
            for b in recent_bars:
                b_ts = float(b.get("t") or b.get("ts") or b.get("time") or 0.0)
                if b_ts > 0 and (b_ts + 60.0 > now_ts):
                    continue  # still forming
                if entry_time is not None and b_ts > 0 and (b_ts + 60.0 < entry_time):
                    continue  # closed before entry
                low = float(b.get("l") or b.get("low") or 0.0)
                if low > 0:
                    ratchet_stop = max(ratchet_stop, low)
        else:
            # Index-based: all bars except the active forming bar (last) are completed
            completed = recent_bars[:-1] if len(recent_bars) > 1 else recent_bars
            for b in completed:
                low = float(b.get("l") or b.get("low") or 0.0)
                if low > 0:
                    ratchet_stop = max(ratchet_stop, low)

    return round(max(ratchet_stop, hard_stop_floor), 2)


def is_catalyst_thrust_eligible(
    record: dict,
    cfg: dict,
    now: float | None = None,
    bars: list[dict] | None = None,
) -> tuple[bool, str, dict]:
    """Evaluates whether a candidate symbol qualifies for the 09:30 Open Thrust arm.

    Args:
        record: Symbol record from watch board / screener.
        cfg: Configuration dictionary (hot-reloaded from bot_config.json).
        now: Optional current timestamp (seconds since epoch).
        bars: Optional recent 1-minute bars for the symbol.

    Returns:
        (is_eligible, reason_str, details_dict)
    """
    if not isinstance(record, dict):
        return False, "invalid_record", {}

    if not bool(cfg.get("ai_catalyst_thrust_enabled", False)):
        return False, "catalyst_thrust_disabled", {}

    # 1. Time Window Check (RTH 09:31:00 to 09:33:00 ET: Bar 1 open, no late chasing)
    ts = float(now if now is not None else datetime.now(timezone.utc).timestamp())
    dt_et = datetime.fromtimestamp(ts, tz=ET)

    # Check weekday (Monday=0 ... Friday=4)
    if dt_et.weekday() > 4:
        return False, "weekend", {}

    min_from_midnight = dt_et.hour * 60 + dt_et.minute
    rth_open_min = 9 * 60 + 30     # 09:30 ET
    thrust_open_min = 9 * 60 + 31  # 09:31 ET (Bar 0 closed)
    thrust_max_min = 9 * 60 + 33   # 09:33 ET (Max entry window)

    if min_from_midnight < rth_open_min:
        return False, "pre_open", {}
    if min_from_midnight < thrust_open_min:
        return False, "bar0_forming", {}
    if min_from_midnight > thrust_max_min:
        return False, "window_expired", {}

    # 2. Price Floor Check ($20+ by default)
    min_px = float(cfg.get("ai_catalyst_thrust_min_price", DEFAULT_MIN_PRICE))
    price = float(record.get("price") or record.get("ask") or record.get("last_ask") or 0.0)
    if price < min_px:
        return False, f"price_${price:.2f}<${min_px:.2f}", {}

    # 3. Gap Threshold Check (8%+ by default, strictly premarket gap, never intraday pct_change)
    min_gap = float(cfg.get("ai_catalyst_thrust_min_gap_pct", DEFAULT_MIN_GAP_PCT))
    gap = None
    for k in ("gap_pct", "premarket_pct", "admit_pct_change", "pct_change", "change_pct", "pctr_gap"):
        v = record.get(k)
        if v is not None:
            try:
                gap = float(v)
                break
            except (ValueError, TypeError):
                continue

    if gap is None and isinstance(record.get("indicator"), dict):
        v = record["indicator"].get("pctr_gap")
        if v is not None:
            try:
                gap = float(v)
            except (ValueError, TypeError):
                pass

    if gap is None:
        open_px = float(record.get("open") or record.get("session_open") or 0.0)
        prev_close = float(record.get("prev_close") or record.get("previous_close") or 0.0)
        if open_px > 0 and prev_close > 0:
            gap = ((open_px - prev_close) / prev_close) * 100.0

    sym = str(record.get("symbol") or "").upper().strip()
    if gap is None and sym:
        try:
            from ai_entry_watch import open_gap_pct
            og = open_gap_pct(sym, now=now)
            if og is not None:
                gap = float(og)
        except Exception:
            pass

    if gap is None:
        return False, "gap_unknown", {}
    if gap < min_gap:
        return False, f"gap_{gap:.1f}%<{min_gap:.1f}%", {}

    # 4. Fundamental Catalyst Verification (< 09:25:00 ET timestamp)
    src = str(record.get("source") or "").lower().strip()
    is_catalyst = (
        bool(record.get("catalyst_verified"))
        or bool(record.get("has_catalyst"))
        or src in ("ai_catalyst", "catalyst")
    )

    if not is_catalyst:
        # Check point-in-time news cache for bullish fundamental news strictly before 09:25:00 ET
        try:
            import news_feed
            cache = news_feed.load_cache()
            items = cache.get(sym) or []
            for it in items:
                t_it = it.get("ts")
                if t_it is None:
                    continue
                dt_news = datetime.fromtimestamp(float(t_it), tz=ET)
                # Same calendar date and strictly before 09:25:00 ET, or prior evening
                is_pre_0925 = (
                    (dt_news.date() == dt_et.date() and (dt_news.hour * 60 + dt_news.minute < 9 * 60 + 25))
                    or (dt_news.date() < dt_et.date() and (ts - float(t_it) <= 18 * 3600))
                )
                if is_pre_0925:
                    hl = str(it.get("headline") or "")
                    if news_feed.BULLISH.search(hl) and not news_feed.BEARISH.search(hl):
                        is_catalyst = True
                        break
        except Exception as _ne:
            log.debug("News cache lookup error for %s: %s", sym, _ne)

    if not is_catalyst:
        return False, "no_verified_premarket_catalyst", {}

    # 5. Opening Drive Green Confirmation (Bar 0 Strictly Green: Close > Open, Low > 0)
    bar0 = None
    if bars and len(bars) > 0:
        bar0 = bars[0]
    else:
        # Check ob_observe store for today's 09:30:00 ET candle
        today_s = dt_et.strftime("%Y-%m-%d")
        try:
            import ob_observe
            ob_bars = ob_observe.bars(sym)
            if ob_bars:
                for b in ob_bars:
                    b_dt = datetime.fromtimestamp(b[0], tz=ET)
                    if b_dt.strftime("%Y-%m-%d") == today_s and b_dt.hour == 9 and b_dt.minute == 30:
                        bar0 = {"o": b[1], "h": b[2], "l": b[3], "c": b[4]}
                        break
        except Exception:
            pass

        # Fallback to Alpaca IEX data feed for 09:30 minute bar
        if not bar0:
            try:
                from ai_entry_watch import _data_client
                from alpaca.data.requests import StockBarsRequest
                from alpaca.data.timeframe import TimeFrame
                from alpaca.data.enums import DataFeed
                client = _data_client()
                today_d = dt_et.date()
                req = StockBarsRequest(
                    symbol_or_symbols=sym,
                    timeframe=TimeFrame.Minute,
                    start=datetime(today_d.year, today_d.month, today_d.day, 9, 30, tzinfo=ET),
                    feed=DataFeed.IEX,
                    limit=5,
                )
                barset = client.get_stock_bars(req)
                b_list = barset.get(sym, []) if hasattr(barset, "get") else barset[sym]
                for b in b_list:
                    b_et = b.timestamp.astimezone(ET)
                    if b_et.strftime("%Y-%m-%d") == today_s and b_et.hour == 9 and b_et.minute == 30:
                        bar0 = {"o": float(b.open), "h": float(b.high), "l": float(b.low), "c": float(b.close)}
                        break
            except Exception:
                pass

    if not bar0:
        # Fall back to record session open
        open_px = float(record.get("open") or record.get("session_open") or 0.0)
        if open_px > 0 and price < open_px:
            return False, "bar0_red", {}
        if open_px > 0 and price == open_px:
            return False, "bar0_doji", {}
        b0_low = float(record.get("low") or record.get("session_low") or (open_px * 0.99 if open_px > 0 else price * 0.99))
    else:
        b0_open = float(bar0.get("o") or bar0.get("open") or 0.0)
        b0_close = float(bar0.get("c") or bar0.get("close") or 0.0)
        b0_low = float(bar0.get("l") or bar0.get("low") or 0.0)
        b0_high = float(bar0.get("h") or bar0.get("high") or b0_close)
        if b0_open <= 0 or b0_close < b0_open:
            return False, "bar0_red", {}
        if b0_close == b0_open:
            return False, "bar0_doji", {}
        if b0_low <= 0:
            b0_low = b0_open * 0.99

        # Condition 1: Require solid candle body / close near high (no upper blow-off wick)
        rng = b0_high - b0_low
        if rng > 0:
            close_pos = (b0_close - b0_low) / rng
            min_close_pos = float(cfg.get("ai_catalyst_thrust_min_close_pos", 0.65) or 0.65)
            if close_pos < min_close_pos:
                return False, f"bar0_wick_rejection_{close_pos:.2f}<{min_close_pos:.2f}", {}

    initial_stop = compute_open_thrust_initial_stop(
        entry_price=price,
        bar0_low=b0_low,
        max_loss_pct=float(cfg.get("ai_catalyst_thrust_max_loss_pct", DEFAULT_MAX_LOSS_PCT)),
    )

    details = {
        "symbol": sym,
        "price": price,
        "entry_low": price,
        "entry_high": price,
        "initial_stop": initial_stop,
        "stop_price": initial_stop,
        "bar0_low": b0_low,
        "gap_pct": gap,
        "source": src,
        "trigger_time_et": dt_et.strftime("%H:%M:%S"),
        "order_style": "marketable_limit",
        "cap_bp": float(cfg.get("ai_marketable_limit_cap_bp", 5.0)),
        "exit_style": "prior_low_ratchet",
        "strategy": "thrust",
        "trade_style": "thrust",
        "zone_kind": "catalyst_thrust",
        "decision": "BUY",
        "synthetic": True,
        "skip_zone": True,
        "target_1": None,
        "max_hold_sec": 900.0,
    }

    return True, "catalyst_open_thrust", details
