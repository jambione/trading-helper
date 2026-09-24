"""legacy_arms — square / heating / triangle / MACD-gap entry lanes.

Live config runs only the mid_rise −50 cross arm
(``ai_watch_exh_mid_rise_arm=true``). ``exhaustion_allows_buy`` returns that
lane first and never consults the helpers below while mid_rise is on, so the
live decision path does not import this module.

This module exists so a future cleanup can move the square / heating /
oversold-triangle / MACD-gap / TV-exh+RSI function bodies here without
changing mid_rise behavior. Tonight it documents the retired settings and
provides ``legacy_arm_settings()`` for the night report.

Do not enable these arms in live bot_config without a replay that beats
mid_rise on the concurrency + P/L pass bars.
"""
from __future__ import annotations

# Settings owned by switched-off arms (still in config.py defaults / bot_config
# for rollback and tests; live mid_rise path does not read them for the buy
# decision once ai_watch_exh_mid_rise_arm is true).
LEGACY_ARM_SETTINGS = (
    # Square arm
    "ai_watch_exh_square_arm",
    "ai_watch_square_max_age_sec",
    "ai_watch_admit_prefer_square",
    # Heating arm / seating relief tied to heating arm quality
    "ai_watch_exh_heating_with_square",
    "ai_watch_heating_price_rise_sec",
    # Oversold triangle arm (entry — dual_tranche triangle EXIT stays live)
    "ai_watch_exh_oversold_triangle_arm",
    "ai_watch_os_triangle_max_age_sec",
    # MACD-gap second arm
    "ai_watch_macd_gap_arm",
    "ai_watch_macd_gap_min_pct",
    "ai_watch_macd_gap_rsi_max",
    "ai_watch_macd_max_age_sec",
    # TV exh+RSI mode
    "ai_watch_tv_exh_rsi",
)

# Removed from live bot_config on 2026-09-24 (nothing read them).
REMOVED_UNREAD_SETTINGS = (
    "ai_watch_momentum_require_flag",
    "swing_min_eps_growth",
    "swing_max_eps_growth",
    "tv_chart_url",
)


def legacy_arm_settings() -> tuple[str, ...]:
    return LEGACY_ARM_SETTINGS


def removed_unread_settings() -> tuple[str, ...]:
    return REMOVED_UNREAD_SETTINGS
