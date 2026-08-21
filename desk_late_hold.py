"""Last-hour hold paper experiment — not a daytime overlay.

Gate 1 (thesis_screen) and gate 2 (optimize_rstop --admit-tod 14:00-15:30
--arm-at-admit) found one candidate: buy names *admitted* between 14:00 and
15:30 ET, hard 2% stop, no 0.10R working shelf, 30-minute dead-trade,
flatten 15:50.

When ``ai_late_hold_paper`` is on:
  • auto-arm is refused outside 14:00–15:30 (daytime scalp does not eat slots)
  • auto-arm is refused for names whose admit_ts is not in that window
  • a fill is stamped ``late_hold`` so trail ratchet never attaches
Default is off. Live knobs (heat, 5% 1R, trail) stay on disk for the
daytime path; they simply do not fire while this experiment is running.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

DEFAULT_START = "14:00"
DEFAULT_END = "15:30"
DEFAULT_STOP_PCT = 2.0
DEFAULT_DEAD_MIN = 30.0


def parse_hhmm(spec: str, fallback: str) -> int:
    raw = (spec or fallback or "0:00").strip()
    try:
        hh, mm = raw.split(":", 1)
        return int(hh) * 60 + int(mm)
    except (TypeError, ValueError):
        hh, mm = fallback.split(":", 1)
        return int(hh) * 60 + int(mm)


def et_minutes(ts: float | None) -> int | None:
    if ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return dt.hour * 60 + dt.minute


def enabled(cfg: dict | None) -> bool:
    return bool((cfg or {}).get("ai_late_hold_paper"))


def window_bounds(cfg: dict | None) -> tuple[int, int]:
    cfg = cfg or {}
    lo = parse_hhmm(str(cfg.get("ai_late_hold_start") or ""), DEFAULT_START)
    hi = parse_hhmm(str(cfg.get("ai_late_hold_end") or ""), DEFAULT_END)
    return lo, hi


def in_window(cfg: dict | None, ts: float | None) -> bool:
    m = et_minutes(ts)
    if m is None:
        return False
    lo, hi = window_bounds(cfg)
    return lo <= m < hi


def stop_pct(cfg: dict | None) -> float:
    try:
        v = float((cfg or {}).get("ai_late_hold_stop_pct", DEFAULT_STOP_PCT) or DEFAULT_STOP_PCT)
    except (TypeError, ValueError):
        v = DEFAULT_STOP_PCT
    return v if v > 0 else DEFAULT_STOP_PCT


def dead_trade_min(cfg: dict | None) -> float:
    try:
        v = float((cfg or {}).get("ai_late_hold_dead_trade_min", DEFAULT_DEAD_MIN)
                  or DEFAULT_DEAD_MIN)
    except (TypeError, ValueError):
        v = DEFAULT_DEAD_MIN
    return v


def arm_why(cfg: dict | None, now: float | None, admit_ts: float | None) -> str | None:
    """None = this tick may late-hold arm. Else a should_arm_buy reason.

    Distinguishes "experiment off" (None, daytime path runs) from
    "experiment on and this name/clock is not the candidate" (a reason).
    """
    if not enabled(cfg):
        return None
    if not in_window(cfg, now):
        return "late_hold_closed"
    if not in_window(cfg, admit_ts):
        return "late_hold_not_late_admit"
    return None


def is_late_hold_pos(pos: dict | None) -> bool:
    if not isinstance(pos, dict):
        return False
    return bool(pos.get("late_hold")) or str(pos.get("strategy") or "") == "late_hold"


def stamp_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Mark a place_scaled_entry decision as the late-hold bundle."""
    d = dict(decision)
    d["late_hold"] = True
    d["strategy"] = "late_hold"
    d["scale_out_pct"] = 0.0
    d["entry_path"] = "late_hold"
    return d
