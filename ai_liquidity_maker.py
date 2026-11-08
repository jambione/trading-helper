#!/usr/bin/env python3
"""ai_liquidity_maker.py — Passive Liquidity Air-Pocket Maker module.

Provides passive liquidity into non-news panic sweeps ("air pockets") on liquid equities,
earning the spread as a maker rather than paying market taker friction.
Corrects the fatal flaw found in the research study: replaces the wide -100 bp stop
with an asymmetric 30 bp structural stop and 3-minute time exit.

Pre-registration: docs/studies/liquidity_shock_maker_prereg.json
Findings: docs/LIQUIDITY_SHOCK_MAKER.md (53.6% hit +40 bp target; corrected risk fixes net expectation).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Optional
import numpy as np

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# Default thresholds from pre-registration & optimization
DEFAULT_MIN_PRICE = 25.0
DEFAULT_MIN_DV = 20_000_000.0
DEFAULT_DROP_BP = 80.0
DEFAULT_VOL_MULT = 3.0
DEFAULT_TARGET_BP = 40.0
DEFAULT_STOP_BP = 30.0         # Asymmetric stop: 30 bp risk vs 40 bp reward (1:1.33)
DEFAULT_TIME_STOP_MIN = 3.0     # Time stop: cut after 3 minutes if not in profit
DEFAULT_ORDER_TTL_SEC = 60.0    # Cancel resting limit order if unfilled within 60s
DEFAULT_SPY_REGIME_MIN = -0.0030 # -30 bp max allowable SPY 15-min drop


def is_liquidity_maker_eligible(
    record: dict,
    cfg: dict,
    bars: list[dict] | None = None,
    now: float | None = None,
    spy_return_15m: float | None = None,
) -> tuple[bool, str, dict]:
    """Evaluates whether a stock has experienced a non-news liquidity air-pocket sweep.

    Args:
        record: Symbol record from watch board.
        cfg: Configuration dictionary (hot-reloaded from bot_config.json).
        bars: 1-minute bars for the symbol (at least 15 bars needed for volume baseline).
        now: Optional current timestamp (seconds since epoch).
        spy_return_15m: 15-minute return of SPY (market regime check).

    Returns:
        (is_eligible, reason_str, details_dict)
    """
    if not isinstance(record, dict):
        return False, "invalid_record", {}

    if not bool(cfg.get("ai_liquidity_maker_enabled", False)):
        return False, "liquidity_maker_disabled", {}

    # 1. Time Window Check (RTH 09:45:00 to 15:30:00 ET)
    ts = float(now if now is not None else datetime.now(timezone.utc).timestamp())
    dt_et = datetime.fromtimestamp(ts, tz=ET)

    if dt_et.weekday() > 4:
        return False, "weekend", {}

    min_from_midnight = dt_et.hour * 60 + dt_et.minute
    start_min = 9 * 60 + 45   # 09:45 ET
    end_min = 15 * 60 + 30    # 15:30 ET

    if min_from_midnight < start_min:
        return False, "pre_market_session_too_early", {}
    if min_from_midnight > end_min:
        return False, "market_session_too_late", {}

    # 2. Price Floor Check ($25+ liquid equity band)
    min_px = float(cfg.get("ai_liquidity_maker_min_price", DEFAULT_MIN_PRICE))
    price = float(record.get("price") or record.get("ask") or record.get("last_ask") or 0.0)
    if price < min_px:
        return False, f"price_${price:.2f}<${min_px:.2f}", {}

    # 3. Dollar Volume Check (ADV20 >= $20M)
    min_dv = float(cfg.get("ai_liquidity_maker_min_dv", DEFAULT_MIN_DV))
    dv = float(record.get("dollar_volume") or record.get("adv20") or 0.0)
    if dv > 0 and dv < min_dv:
        return False, f"dv_${dv/1e6:.1f}M<${min_dv/1e6:.1f}M", {}

    # 4. Market Regime Check (SPY not falling precipitously)
    spy_min = float(cfg.get("ai_liquidity_maker_spy_regime_min", DEFAULT_SPY_REGIME_MIN))
    if spy_return_15m is not None and spy_return_15m < spy_min:
        return False, f"spy_regime_{spy_return_15m*10000:.0f}bp<{spy_min*10000:.0f}bp", {}

    # 5. Air-Pocket Liquidation Sweep Detection
    drop_threshold_bp = float(cfg.get("ai_liquidity_maker_drop_bp", DEFAULT_DROP_BP))
    vol_mult_threshold = float(cfg.get("ai_liquidity_maker_vol_mult", DEFAULT_VOL_MULT))

    if not bars or len(bars) < 16:
        return False, "insufficient_bar_history", {}

    closes = np.array([float(b.get("c") or b.get("close") or 0.0) for b in bars])
    volumes = np.array([float(b.get("v") or b.get("volume") or 0.0) for b in bars])

    # 2-minute drop calculation: close[t] vs close[t-2]
    c_now = closes[-1]
    c_2bars_ago = closes[-3] if len(closes) >= 3 else closes[0]
    if c_2bars_ago <= 0:
        return False, "invalid_close_history", {}

    drop_bp = (c_2bars_ago - c_now) / c_2bars_ago * 10000.0
    if drop_bp < drop_threshold_bp:
        return False, f"drop_{drop_bp:.1f}bp<{drop_threshold_bp:.1f}bp", {}

    # Volume spike check on the panic bar vs rolling 15-bar mean
    recent_vol = volumes[-1]
    baseline_vol_mean = np.nanmean(volumes[-16:-1])
    if baseline_vol_mean <= 0:
        return False, "zero_baseline_volume", {}

    vol_ratio = recent_vol / baseline_vol_mean
    if vol_ratio < vol_mult_threshold:
        return False, f"vol_ratio_{vol_ratio:.1f}x<{vol_mult_threshold:.1f}x", {}

    # 6. Build Passive Resting Limit Order Spec
    bid_px = float(record.get("bid") or (price * (1.0 - 0.0002)))
    limit_px = round(bid_px, 2)

    target_bp = float(cfg.get("ai_liquidity_maker_target_bp", DEFAULT_TARGET_BP))
    stop_bp = float(cfg.get("ai_liquidity_maker_stop_bp", DEFAULT_STOP_BP))
    target_px = round(limit_px * (1.0 + target_bp / 10000.0), 2)
    stop_px = round(limit_px * (1.0 - stop_bp / 10000.0), 2)

    details = {
        "symbol": str(record.get("symbol") or "").upper(),
        "entry_price": limit_px,
        "limit_price": limit_px,
        "drop_bp": drop_bp,
        "vol_ratio": vol_ratio,
        "order_style": "passive_limit",
        "time_in_force": "day",
        "ttl_sec": float(cfg.get("ai_liquidity_maker_timeout_sec", DEFAULT_ORDER_TTL_SEC)),
        "target_price": target_px,
        "stop_price": stop_px,
        "target_bp": target_bp,
        "stop_bp": stop_bp,
        "max_hold_min": float(cfg.get("ai_liquidity_maker_time_stop_min", DEFAULT_TIME_STOP_MIN)),
        "maker_rebate": True,
    }

    return True, "liquidity_air_pocket", details


def compute_maker_asymmetric_exits(
    fill_price: float,
    cfg: dict,
) -> dict:
    """Computes profit target and tight asymmetric stop for filled passive maker orders."""
    if fill_price <= 0:
        return {}

    target_bp = float(cfg.get("ai_liquidity_maker_target_bp", DEFAULT_TARGET_BP))
    stop_bp = float(cfg.get("ai_liquidity_maker_stop_bp", DEFAULT_STOP_BP))
    target_px = round(fill_price * (1.0 + target_bp / 10000.0), 2)
    stop_px = round(fill_price * (1.0 - stop_bp / 10000.0), 2)

    return {
        "fill_price": fill_price,
        "target_price": target_px,
        "stop_price": stop_px,
        "target_bp": target_bp,
        "stop_bp": stop_bp,
        "time_stop_min": float(cfg.get("ai_liquidity_maker_time_stop_min", DEFAULT_TIME_STOP_MIN)),
    }
