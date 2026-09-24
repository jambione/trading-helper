"""book_server — ranked Movers+Trending+Research book (shadow or live).

Replaces the soft-seed / momentum / Discord intake tangle when
``ai_book_server_mode`` is ``live``. Default is ``shadow``: builds its own
ranked queue, logs would-have-done decisions, places no orders.

Ranking (docs/RUNWAY_STUDY_2026-09-24.md + NAME_QUALITY_RERUN correction):

    runway_score ≈
        1.5 * z(day_chg_pct)          # mild; no hard kink (symbol-day n thin)
      + 1.0 * z(used_range_pct)
      + 1.0 * z(ema9_slope)
      + 1.0 * z(-dist_hod_pct)         # farther below HOD = better
      + 0.5 * z(-dist_swing30_pct)
      + 0.5 * (mins_open < 90)
      + 0.5 * (source == movers)
      + pace_term(rvol_pace_sip)       # soft-cap ~4x; ignored before ~09:46

    seat_priority = runway_score + w * closeness_to_−50

Pace is a ranking input only (``ai_watch_min_rvol_pace`` stays 0).
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("book_server")
ET = ZoneInfo("America/New_York")
_ROOT = Path(__file__).resolve().parent

# Sources the server accepts. Everything else is retired as intake when live.
SUPPLY_SOURCES = frozenset({"movers", "trending", "research", "agy", "grok"})

# Settings the live book-server path retires (documented for the night report).
RETIRED_WHEN_LIVE = (
    "ai_watch_soft_seed_enabled",
    "ai_watch_soft_seed_momentum",
    "ai_watch_soft_seed_trending",
    "ai_watch_soft_seed_movers",
    "ai_watch_soft_seed_research",
    "ai_watch_soft_seed_max",
    "ai_watch_heating_min_rvol",          # supply lane; ranking uses pace instead
    "ai_watch_heating_admit_max_tape_age_sec",
    "ai_watch_dead_seat_evict_sec",       # replaced by one-clock + ranked refill
    # Note: dead_seat_evict still used until live promotion; listed as intent.
)

_PACE_SOFT_CAP = 4.0
_PACE_FLOOR = 1.64
_CLOSENESS_W = 2.0
_MORNING_MINS = 90
_PACE_READY_MIN = 9 * 60 + 46  # ~09:46 ET — SIP pace not served earlier


def mode(cfg: dict | None) -> str:
    raw = str((cfg or {}).get("ai_book_server_mode") or "off").strip().lower()
    if raw in ("shadow", "live", "off"):
        return raw
    return "off"


def is_shadow(cfg: dict | None) -> bool:
    return mode(cfg) == "shadow"


def is_live(cfg: dict | None) -> bool:
    return mode(cfg) == "live"


def _f(x: Any, default: float | None = None) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _mins_open(now: float | None = None) -> float:
    dt = datetime.fromtimestamp(float(now if now is not None else time.time()), ET)
    return (dt.hour * 60 + dt.minute + dt.second / 60.0) - (9 * 60 + 30)


def _et_hhmm_min(now: float | None = None) -> int:
    dt = datetime.fromtimestamp(float(now if now is not None else time.time()), ET)
    return dt.hour * 60 + dt.minute


def _tanh_z(x: float | None, scale: float) -> float:
    """Bounded soft-z: tanh(x/scale). Missing → 0."""
    if x is None or scale <= 0:
        return 0.0
    return math.tanh(float(x) / float(scale))


def pace_term(rvol_pace: float | None) -> float:
    """Soft-cap pace contribution near 4x; lift starts around 1.64."""
    if rvol_pace is None:
        return 0.0
    p = max(0.0, float(rvol_pace))
    # Map [0, soft_cap] → [0, 1] with floor emphasis around 1.64.
    capped = min(p, _PACE_SOFT_CAP)
    if capped < _PACE_FLOOR:
        return 0.4 * (capped / _PACE_FLOOR)  # weak credit below the study floor
    # 1.64 → 4.0 maps to 0.4 → 1.0
    return 0.4 + 0.6 * ((capped - _PACE_FLOOR) / (_PACE_SOFT_CAP - _PACE_FLOOR))


def closeness_to_cross(fast_pctr: float | None, level: float = -50.0) -> float:
    """Higher as fast %R approaches ``level`` from below and is rising-ready.

    Below level: score rises as we near it (distance shrinks).
    Above level: already crossed — small residual credit that fades.
    """
    if fast_pctr is None:
        return 0.0
    f = float(fast_pctr)
    if f <= level:
        # −100 → 0, −50 → 1
        span = max(1.0, level - (-100.0))
        return max(0.0, min(1.0, (f - (-100.0)) / span))
    # Past the cross: fade from 1 toward 0 over the next 20 pts.
    return max(0.0, 1.0 - (f - level) / 20.0)


def runway_score(
    *,
    day_chg_pct: float | None = None,
    used_range_pct: float | None = None,
    ema9_slope: float | None = None,
    dist_hod_pct: float | None = None,
    dist_swing30_pct: float | None = None,
    source: str = "",
    rvol_pace: float | None = None,
    now: float | None = None,
    include_pace: bool | None = None,
) -> float:
    """Higher = more runway. No hard day-change kink (see name-quality correction)."""
    src = str(source or "").strip().lower()
    mins = _mins_open(now)
    if include_pace is None:
        include_pace = _et_hhmm_min(now) >= _PACE_READY_MIN

    score = 0.0
    score += 1.5 * _tanh_z(day_chg_pct, 8.0)
    score += 1.0 * _tanh_z(used_range_pct, 50.0)
    score += 1.0 * _tanh_z(ema9_slope, 0.5)
    # Farther below HOD / swing high = better (negative dist → positive).
    if dist_hod_pct is not None:
        score += 1.0 * _tanh_z(-float(dist_hod_pct), 5.0)
    if dist_swing30_pct is not None:
        score += 0.5 * _tanh_z(-float(dist_swing30_pct), 3.0)
    if mins < _MORNING_MINS:
        score += 0.5
    if src == "movers":
        score += 0.5
    if include_pace:
        score += pace_term(rvol_pace)
    return float(score)


def seat_priority(
    row: dict,
    *,
    now: float | None = None,
    mid_rise_level: float = -50.0,
    closeness_w: float = _CLOSENESS_W,
) -> float:
    """Combine runway with proximity to the −50 cross."""
    src = str(row.get("source") or row.get("src") or "").strip().lower()
    ind = row.get("indicator") if isinstance(row.get("indicator"), dict) else {}
    fast = _f(ind.get("pctr") if ind else row.get("pctr"))
    day_chg = _f(row.get("day_chg_pct") or row.get("pct") or row.get("change_pct"))
    used = _f(row.get("used_range_pct") or row.get("range_pos_pct"))
    slope = _f(row.get("ema9_slope") or (ind.get("ema9_slope") if ind else None))
    dist_hod = _f(row.get("dist_hod_pct") or row.get("pct_from_hod"))
    dist_sw = _f(row.get("dist_swing30_pct"))
    pace = _f(row.get("rvol_pace_sip") or row.get("rvol_pace") or row.get("rvol"))
    rs = runway_score(
        day_chg_pct=day_chg,
        used_range_pct=used,
        ema9_slope=slope,
        dist_hod_pct=dist_hod,
        dist_swing30_pct=dist_sw,
        source=src,
        rvol_pace=pace,
        now=now,
    )
    return rs + float(closeness_w) * closeness_to_cross(fast, mid_rise_level)


def filter_supply(rows: list[dict]) -> list[dict]:
    """Keep Movers / Trending / Research only."""
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        src = str(r.get("source") or r.get("src") or "").strip().lower()
        if src in SUPPLY_SOURCES or any(
            c in SUPPLY_SOURCES for c in (r.get("criteria") or [])
            if isinstance(c, str)
        ):
            out.append(r)
    return out


def rank_candidates(
    rows: list[dict],
    *,
    cfg: dict | None = None,
    now: float | None = None,
    limit: int = 40,
) -> list[dict]:
    """Return supply rows sorted by seat_priority (desc), capped."""
    cfg = cfg or {}
    try:
        level = float(cfg.get("ai_watch_mid_rise_level", -50.0) or -50.0)
    except (TypeError, ValueError):
        level = -50.0
    scored = []
    for r in filter_supply(rows):
        pri = seat_priority(r, now=now, mid_rise_level=level)
        out = dict(r)
        out["_book_server_priority"] = round(pri, 4)
        out["_book_server_runway"] = round(
            pri - _CLOSENESS_W * closeness_to_cross(
                _f((r.get("indicator") or {}).get("pctr") if isinstance(r.get("indicator"), dict)
                   else r.get("pctr")),
                level,
            ),
            4,
        )
        scored.append(out)
    scored.sort(key=lambda x: float(x.get("_book_server_priority") or 0), reverse=True)
    return scored[: max(0, int(limit))]


def _shadow_path(day: str | None = None) -> Path:
    if day is None:
        day = datetime.now(ET).strftime("%Y-%m-%d")
    d = _ROOT / "ai_reports" / "book_server_shadow"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{day}.jsonl"


def log_shadow(decision: dict) -> None:
    """Append a would-have-done row. Never writes live watch state."""
    try:
        path = _shadow_path()
        decision = dict(decision)
        decision.setdefault("ts", time.time())
        decision.setdefault(
            "et", datetime.fromtimestamp(decision["ts"], ET).strftime("%H:%M:%S"))
        with open(path, "a") as f:
            f.write(json.dumps(decision, default=str) + "\n")
    except Exception as e:  # noqa: BLE001
        log.debug("[BOOK_SERVER] shadow log failed: %s", e)


def shadow_tick(
    candidates: list[dict],
    *,
    cfg: dict | None = None,
    now: float | None = None,
    live_book: list[str] | None = None,
    max_seats: int = 12,
) -> list[dict]:
    """Build a ranked would-be book and log the diff vs live. Returns ranked seats."""
    cfg = cfg or {}
    now = float(now if now is not None else time.time())
    ranked = rank_candidates(candidates, cfg=cfg, now=now, limit=max_seats * 2)
    would_seat = ranked[:max_seats]
    live = {str(s).upper() for s in (live_book or [])}
    would = {str(r.get("symbol") or r.get("ticker") or "").upper() for r in would_seat}
    would.discard("")
    log_shadow({
        "kind": "shadow_book",
        "n_candidates": len(candidates),
        "n_ranked": len(ranked),
        "n_would_seat": len(would_seat),
        "would_symbols": sorted(would),
        "live_symbols": sorted(live),
        "add": sorted(would - live),
        "drop": sorted(live - would),
        "top": [
            {
                "symbol": str(r.get("symbol") or r.get("ticker") or "").upper(),
                "source": r.get("source") or r.get("src"),
                "priority": r.get("_book_server_priority"),
                "runway": r.get("_book_server_runway"),
                "pace": r.get("rvol_pace_sip") or r.get("rvol_pace") or r.get("rvol"),
                "day_chg": r.get("day_chg_pct") or r.get("pct"),
            }
            for r in would_seat[:12]
        ],
    })
    return would_seat
