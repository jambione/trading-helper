"""Map raw ``arm_why`` strings to ~8 veto buckets (Package 1, observe-only).

Behavior-neutral: does not change ``should_arm_buy`` or any knobs. Used by the
decision ledger so "why didn't X arm?" is answerable from one file.
"""
from __future__ import annotations

from typing import Any

# Canonical bucket names — keep this list short and stable.
BUCKETS = (
    "readiness",
    "exh",
    "rsi",
    "macd_dir",
    "heat",
    "spread",
    "zone",
    "other",
)

# Exact matches first (fast path for the densest live reasons).
_EXACT: dict[str, str] = {
    # readiness — tape / stream / indicator data not ready to decide
    "tape_only": "readiness",
    "stale_quote": "readiness",
    "need_stream": "readiness",
    "stream_required": "readiness",
    "await_stream": "readiness",
    "no_quote": "readiness",
    "no_quote_age": "readiness",
    "no_macd_data": "readiness",
    "no_rsi_data": "readiness",
    "no_indicators": "readiness",
    "macd_src_unknown": "readiness",
    "macd_stale_bars": "readiness",
    "macd_sep_unknown": "readiness",
    "bars_missing": "readiness",
    "ind_stale": "readiness",
    "stale_data": "readiness",
    # exh
    "exh_falling": "exh",
    "exh_not_rising": "exh",
    "exh_rising_required": "exh",
    "last_exhaustion_off": "exh",
    "last_no_exhaustion_fallback": "exh",
    "no_exhaustion_data": "exh",
    "no_exhaustion_fallback": "exh",
    # rsi
    "rsi_extended": "rsi",
    "rsi_not_rising": "rsi",
    "rsi_below_band": "rsi",
    # macd_dir — direction / gap geometry (not missing-data)
    "macd_bearish": "macd_dir",
    "macd_gap_narrowing": "macd_dir",
    "macd_gap_too_close": "macd_dir",
    "macd_gap_insufficient": "macd_dir",
    "macd_gap_dir_unknown": "macd_dir",
    "macd_no_recent_cross": "macd_dir",
    # heat
    "mistimed_heat": "heat",
    "soft_ob": "heat",
    "late_heat": "heat",
    "cheap_ob_band": "heat",
    "heat_extended": "heat",
    "last_heating": "heat",
    "last_overbought": "heat",
    "heating_too_low": "heat",
    "extended_cheap": "heat",
    "not_rising_overbought": "heat",
    "not_rising_cooling": "heat",
    "not_rising_flat": "heat",
    "overbought": "heat",
    # spread
    "spread": "spread",
    "wide_spread": "spread",
    # zone / structure
    "above_zone": "zone",
    "below_zone": "zone",
    "in_zone": "zone",
    "offset_zone": "zone",
    "no_structure": "zone",
    "wait_setup": "zone",
    "hard_no": "zone",
    "prefilter_far": "zone",
    "reward_risk": "zone",
}


def arm_bucket(arm_why: Any) -> str:
    """Map a raw ``arm_why`` / block reason to one of ``BUCKETS``.

    Empty / missing → ``other``. Unknown strings → ``other`` (raw kept on the
    ledger row). Prefix rules cover live families like ``macd_not_realtime*``.
    """
    w = str(arm_why or "").strip().lower()
    if not w:
        return "other"
    hit = _EXACT.get(w)
    if hit is not None:
        return hit

    # Prefix / family rules (order matters: more specific first).
    if w.startswith("macd_not_realtime") or w.startswith("rsi_not_realtime"):
        return "readiness"
    if w.startswith("stale_") or w.startswith("no_quote"):
        return "readiness"
    if w.startswith("await_") or w.startswith("need_"):
        return "readiness"
    if w.startswith("no_macd") or w.startswith("no_rsi") or w.startswith("no_exh"):
        return "readiness"
    if w.startswith("no_exhaustion") or w.startswith("exh_"):
        return "exh"
    if w.startswith("cm_rsi") or w.startswith("rsi_"):
        return "rsi"
    if w.startswith("macd_gap_") or w.startswith("macd_"):
        # Remaining macd_* after not_realtime / no_macd → direction family.
        return "macd_dir"
    if (
        w.startswith("cheap_ob")
        or w.startswith("mistimed")
        or w.startswith("soft_ob")
        or w.startswith("heat")
        or w.startswith("last_heat")
        or w.startswith("not_rising")
    ):
        return "heat"
    if w.startswith("spread") or w.endswith("_spread"):
        return "spread"
    if (
        w.endswith("_zone")
        or w.startswith("above_")
        or w.startswith("below_")
        or w.startswith("wait_")
    ):
        return "zone"
    return "other"


def bucket_label(bucket: str) -> str:
    """Short human label for CLI / notes."""
    return {
        "readiness": "tape/data not ready",
        "exh": "exhaustion / %R rising",
        "rsi": "CM RSI band / rising",
        "macd_dir": "MACD direction / gap",
        "heat": "mistimed / soft OB / cheap heat",
        "spread": "spread too wide",
        "zone": "zone / structure",
        "other": "other / unclassified",
    }.get(str(bucket or ""), str(bucket or "other"))


__all__ = ["BUCKETS", "arm_bucket", "bucket_label"]
