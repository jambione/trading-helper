#!/usr/bin/env python3
"""ai_catalyst_thrust.py — Strategy module for 09:30 Catalyst Open Thrust.

Captures high-magnitude momentum at inception on verified fundamental catalyst gappers
(earnings, guidance, FDA) during the 09:30-09:35 ET opening drive.
Replaces the 15-minute %R delay with immediate execution on green opening drive.

Pre-registration: docs/studies/catalyst_thrust_family_prereg.json
Findings: docs/CATALYST_THRUST_FAMILY.md (+19.68 bp gross lift vs control, +6.67 bp net).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Optional

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# Default parameter bounds
DEFAULT_MIN_GAP_PCT = 8.0
DEFAULT_MIN_PRICE = 20.0
DEFAULT_MAX_LOSS_PCT = 0.020  # 2.0% hard catastrophic stop
DEFAULT_MAX_HOLD_BARS = 15     # Max 15-minute hold


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

    # 1. Time Window Check (RTH 09:30:00 to 09:35:00 ET)
    ts = float(now if now is not None else datetime.now(timezone.utc).timestamp())
    dt_et = datetime.fromtimestamp(ts, tz=ET)
    
    # Check weekday (Monday=0 ... Friday=4)
    if dt_et.weekday() > 4:
        return False, "weekend", {}

    min_from_midnight = dt_et.hour * 60 + dt_et.minute
    rth_open_min = 9 * 60 + 30  # 09:30 ET
    max_open_min = 9 * 60 + 35  # 09:35 ET

    if min_from_midnight < rth_open_min:
        return False, "pre_open", {}
    if min_from_midnight > max_open_min:
        return False, "window_expired", {}

    # 2. Price Floor Check
    min_px = float(cfg.get("ai_catalyst_thrust_min_price", DEFAULT_MIN_PRICE))
    price = float(record.get("price") or record.get("ask") or record.get("last_ask") or 0.0)
    if price < min_px:
        return False, f"price_${price:.2f}<${min_px:.2f}", {}

    # 3. Gap / Move Threshold Check
    min_gap = float(cfg.get("ai_catalyst_thrust_min_gap_pct", DEFAULT_MIN_GAP_PCT))
    gap = float(
        record.get("gap_pct")
        if record.get("gap_pct") is not None
        else (record.get("pct_change") or record.get("premarket_pct") or 0.0)
    )
    if gap < min_gap:
        return False, f"gap_{gap:.1f}%<{min_gap:.1f}%", {}

    # 4. Source & Catalyst Verification Check
    src = str(record.get("source") or "").lower().strip()
    is_catalyst = (
        src in ("ai_catalyst", "catalyst", "movers", "discord")
        or bool(record.get("has_catalyst"))
        or bool(record.get("catalyst_verified"))
    )
    if not is_catalyst:
        return False, f"source_{src}_not_catalyst", {}

    # 5. Opening Drive Green Confirmation (Bar 0 Green: Close > Open)
    open_px = float(record.get("open") or record.get("session_open") or 0.0)
    if bars and len(bars) > 0:
        bar0 = bars[0]
        b0_open = float(bar0.get("o") or bar0.get("open") or 0.0)
        b0_close = float(bar0.get("c") or bar0.get("close") or 0.0)
        if b0_open > 0 and b0_close < b0_open:
            return False, "bar0_red", {}
    elif open_px > 0 and price < open_px:
        return False, "day_open_red", {}

    details = {
        "symbol": str(record.get("symbol") or "").upper(),
        "price": price,
        "gap_pct": gap,
        "source": src,
        "trigger_time_et": dt_et.strftime("%H:%M:%S"),
        "order_style": "marketable_limit",
        "cap_bp": float(cfg.get("ai_marketable_limit_cap_bp", 5.0)),
        "exit_style": "prior_low_ratchet",
    }

    return True, "catalyst_open_thrust", details


def compute_open_thrust_ratchet_stop(
    entry_price: float,
    current_stop: float | None,
    recent_bars: list[dict],
    max_loss_pct: float = DEFAULT_MAX_LOSS_PCT,
) -> float:
    """Computes the 1-minute prior-low trailing ratchet stop.

    Ratchets stop up to the low of the most recently closed 1-minute bar (low[t-1]),
    never ratcheting downward. Hard initial stop floor is entry_price * (1 - max_loss_pct).
    """
    if entry_price <= 0:
        return 0.0

    hard_stop_floor = round(entry_price * (1.0 - float(max_loss_pct)), 2)
    ratchet_stop = current_stop if current_stop is not None else hard_stop_floor

    if recent_bars and len(recent_bars) >= 2:
        # Most recently completed bar is at index -2 (index -1 is active bar)
        closed_bar = recent_bars[-2]
        prior_low = float(closed_bar.get("l") or closed_bar.get("low") or 0.0)
        if prior_low > 0:
            ratchet_stop = max(ratchet_stop, prior_low)

    # Ensure stop never falls below initial floor
    return max(ratchet_stop, hard_stop_floor)
