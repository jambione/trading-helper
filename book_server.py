"""book_server — ranked seed book hunting names entering the square zone.

Replaces the soft-seed / momentum / Discord intake tangle when
``ai_book_server_mode`` is ``live``. Default is ``shadow``: builds its own
ranked queue, logs would-have-done decisions, places no orders.

Square/triangle product ranking (enter dual-OB+tight ■, exit leave-OB ▼):

    runway_score =
        1.5 * tanh(day_chg_pct / 8)
      + 0.5 * (mins_open < 90)
      + 0.5 * (source == movers)
      + pace_term(volume pace)         # soft-cap ~4x; ignored before ~09:46

    seat_priority = runway_score + 2.5 * closeness_to_square(fast, slow)

``closeness_to_square`` peaks on dual-OB+tight squares, then pre-square
(approach band + tight + rising), then names warming toward the zone.
Supply covers movers / trending / research / momentum (and AI seed tags).

Rows with both %R lines known rank ahead of rows missing a line: the square
arm cannot fire without dual readings.

Volume pace is SIP day volume vs the name's own 20-day SIP normal when
available (``rvol_pace_sip`` or movers ``rvol``). Trending IEX rvol is not used.
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("book_server")
ET = ZoneInfo("America/New_York")
_ROOT = Path(__file__).resolve().parent

# Sources the server accepts across desk seeds when hunting square setups.
SUPPLY_SOURCES = frozenset({
    "movers", "trending", "research", "agy", "xai", "grok",
    "momentum", "mom", "mom_open", "bb_live", "bro", "stocktwits", "st",
})

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
_CLOSENESS_W = 2.5
_MORNING_MINS = 90
_PACE_READY_MIN = 9 * 60 + 46  # ~09:46 ET — SIP pace not served earlier
_SHADOW_HEARTBEAT_SEC = 60.0
_last_shadow: dict[str, Any] = {"key": None, "ts": 0.0}


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


def _first(row: dict, *keys: str) -> float | None:
    """First key that holds a finite number. 0.0 is a value, not a miss."""
    for k in keys:
        v = _f(row.get(k))
        if v is not None:
            return v
    return None


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
    """Legacy −50 proximity (kept for tests / mid-rise rollback ranking)."""
    if fast_pctr is None:
        return 0.0
    f = float(fast_pctr)
    if f <= level:
        span = max(1.0, level - (-100.0))
        return max(0.0, min(1.0, (f - (-100.0)) / span))
    return max(0.0, 1.0 - (f - level) / 20.0)


def closeness_to_square(
    fast_pctr: float | None,
    slow_pctr: float | None = None,
    *,
    thr: float = 20.0,
    pre_thr: float = 35.0,
    tight_max: float = 15.0,
    rising: bool | None = None,
    slow_rising: bool | None = None,
) -> float:
    """Higher as dual %R enters the square / pre-square zone.

    1.0 = dual OB + tight (square ■)
    ~0.5–0.95 = pre-square (both in approach band, tight, rising)
    lower = warming toward the zone or wide-gap heaters
    """
    if fast_pctr is None:
        return 0.0
    f = float(fast_pctr)
    s = float(slow_pctr) if slow_pctr is not None else None
    thr = max(1.0, float(thr))
    pre_thr = max(thr, float(pre_thr))
    tight_max = max(0.0, float(tight_max))
    if s is None:
        # Fast-only: weak credit as it nears the OB band.
        if f >= -thr:
            return 0.35
        span = max(1.0, 100.0 - thr)
        return 0.25 * max(0.0, min(1.0, (f - (-100.0)) / span))
    gap = abs(f - s)
    tight = gap <= tight_max + 1e-9
    both_ob = f >= -thr and s >= -thr
    if both_ob and tight:
        return 1.0
    if both_ob and not tight:
        return 0.55  # in OB band but wide (RKLB-class) — seat, don't prefer
    both_pre = f >= -pre_thr and s >= -pre_thr
    rising_ok = (rising is True) or (slow_rising is True) or (rising is None and slow_rising is None)
    if both_pre and tight and rising_ok:
        lo = min(f, s)
        span = max(1.0, pre_thr - thr)
        t = (lo - (-pre_thr)) / span  # 0 at pre edge → 1 at OB edge
        return 0.55 + 0.40 * max(0.0, min(1.0, t))
    if both_pre and not tight:
        return 0.40
    lo = min(f, s)
    if lo < -pre_thr:
        span = max(1.0, 100.0 - pre_thr)
        return 0.35 * max(0.0, min(1.0, (lo - (-100.0)) / span))
    return 0.15


def closeness_to_oversold(
    fast_pctr: float | None,
    slow_pctr: float | None = None,
    *,
    thr: float = 20.0,
    pre_thr: float = 35.0,
    tight_max: float = 15.0,
    rising: bool | None = None,
    slow_rising: bool | None = None,
) -> float:
    """Higher as dual %R enters the oversold triangle / leave band.

    Mirror of ``closeness_to_square``. 1.0 = both ≤ −(100−thr) and tight.
    High EXH near 0 scores low so an oversold-only book does not seat heaters.
    """
    if fast_pctr is None:
        return 0.0
    f = float(fast_pctr)
    s = float(slow_pctr) if slow_pctr is not None else None
    thr = max(1.0, float(thr))
    pre_thr = max(thr, float(pre_thr))
    tight_max = max(0.0, float(tight_max))
    os_lvl = -100.0 + thr
    pre_lvl = -100.0 + pre_thr
    if s is None:
        if f <= os_lvl:
            return 0.35
        span = max(1.0, 100.0 - thr)
        return 0.25 * max(0.0, min(1.0, ((-f) - thr) / span))
    gap = abs(f - s)
    tight = gap <= tight_max + 1e-9
    both_os = f <= os_lvl and s <= os_lvl
    if both_os and tight:
        return 1.0
    if both_os and not tight:
        return 0.55
    both_pre = f <= pre_lvl and s <= pre_lvl
    rising_ok = (
        (rising is True) or (slow_rising is True)
        or (rising is None and slow_rising is None)
    )
    if both_pre and not both_os and tight and rising_ok:
        # Shallower line: 0 at the −65 edge, 1 as it reaches −80.
        shallower = max(f, s)
        span = max(1.0, pre_lvl - os_lvl)
        t = (pre_lvl - shallower) / span
        return 0.55 + 0.40 * max(0.0, min(1.0, t))
    if both_pre and not tight:
        return 0.40
    # Above the low band (high EXH): small credit, fading toward 0.
    shallower = max(f, s)
    if shallower > pre_lvl:
        span = max(1.0, 0.0 - pre_lvl)
        t = max(0.0, min(1.0, (shallower - pre_lvl) / span))
        return 0.20 * (1.0 - t)
    return 0.15


def zone_closeness(got: dict, cfg: dict | None = None, *,
                   mid_rise_level: float = -50.0) -> float:
    """Closeness the active arms actually trade.

    Missing ``ai_watch_exh_oversold_arm`` keeps today's square closeness so
    partial test cfgs do not start preferring −90 over a pre-square.
    Oversold on → max(square, oversold) when the square arm is also on,
    or oversold alone when square is explicitly off.
    Mid-rise −50 ranking only when it is the exclusive arm.
    """
    cfg = cfg or {}
    thr, pre, tight = _square_params(cfg)
    os_on = bool(cfg.get("ai_watch_exh_oversold_arm", False))
    sq_on = bool(cfg.get("ai_watch_exh_square_arm", True))
    mid_on = bool(cfg.get("ai_watch_exh_mid_rise_arm", False))
    if mid_on and not sq_on and not os_on:
        return closeness_to_cross(got.get("pctr"), mid_rise_level)
    sq = closeness_to_square(
        got.get("pctr"), got.get("pctr_slow"),
        thr=thr, pre_thr=pre, tight_max=tight,
        rising=got.get("pctr_rising"),
        slow_rising=got.get("pctr_slow_rising"),
    )
    if not os_on:
        return sq
    os_c = closeness_to_oversold(
        got.get("pctr"), got.get("pctr_slow"),
        thr=thr, pre_thr=pre, tight_max=tight,
        rising=got.get("pctr_rising"),
        slow_rising=got.get("pctr_slow_rising"),
    )
    if sq_on:
        return max(sq, os_c)
    return os_c


def runway_score(
    *,
    day_chg_pct: float | None = None,
    source: str = "",
    rvol_pace: float | None = None,
    now: float | None = None,
    include_pace: bool | None = None,
) -> float:
    """Higher = more runway. No hard day-change kink (see name-quality correction)."""
    src = str(source or "").strip().lower()
    if include_pace is None:
        include_pace = _et_hhmm_min(now) >= _PACE_READY_MIN
    score = 1.5 * _tanh_z(day_chg_pct, 8.0)
    if _mins_open(now) < _MORNING_MINS:
        score += 0.5
    if src == "movers":
        score += 0.5
    if include_pace:
        score += pace_term(rvol_pace)
    return float(score)


def _source(row: dict) -> str:
    return str(row.get("source") or row.get("src") or "").strip().lower()


def _symbol(row: dict) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").upper().strip()


def row_inputs(
    row: dict,
    *,
    indicators: dict[str, dict] | None = None,
    paces: dict[str, float] | None = None,
) -> dict:
    """The ranking inputs this row really has. Missing stays None."""
    sym = _symbol(row)
    ind = row.get("indicator") if isinstance(row.get("indicator"), dict) else None
    if ind is None and indicators:
        ind = indicators.get(sym) if isinstance(indicators.get(sym), dict) else None
    pctr = _f(ind.get("pctr")) if ind else _f(row.get("pctr"))
    pctr_slow = _f(ind.get("pctr_slow")) if ind else _f(row.get("pctr_slow"))
    rising = None
    slow_rising = None
    if ind:
        if "pctr_rising" in ind:
            rising = bool(ind.get("pctr_rising"))
        if "pctr_slow_rising" in ind:
            slow_rising = bool(ind.get("pctr_slow_rising"))
    pace, pace_src = _first(row, "rvol_pace_sip"), "pace_sip"
    if pace is None and paces and sym in paces:
        pace, pace_src = _f(paces.get(sym)), "pace_sip"
    if pace is None and _source(row) == "movers":
        # movers_screener's rvol: SIP day volume vs the name's own 20-day SIP
        # average, time-adjusted to the delayed bar — the study's statistic.
        pace, pace_src = _f(row.get("rvol")), "movers_sip"
    return {
        "chg": _first(row, "day_chg_pct", "pct_change", "pct", "change_pct"),
        "pace": pace,
        "pace_src": pace_src if pace is not None else None,
        "pctr": pctr,
        "pctr_slow": pctr_slow,
        "pctr_rising": rising,
        "pctr_slow_rising": slow_rising,
    }


def _square_params(cfg: dict | None = None) -> tuple[float, float, float]:
    cfg = cfg or {}
    try:
        thr = float(cfg.get("rte_threshold", 20) or 20)
    except (TypeError, ValueError):
        thr = 20.0
    try:
        pre = float(cfg.get("ai_watch_exh_pre_thr", 35.0) or 35.0)
    except (TypeError, ValueError):
        pre = 35.0
    try:
        tight = float(cfg.get("rte_confluence_max", 15.0) or 15.0)
    except (TypeError, ValueError):
        tight = 15.0
    return thr, pre, tight


def seat_priority(
    row: dict,
    *,
    now: float | None = None,
    mid_rise_level: float = -50.0,
    closeness_w: float = _CLOSENESS_W,
    inputs: dict | None = None,
    cfg: dict | None = None,
) -> float:
    """Combine runway with proximity to the dual-%R square zone."""
    got = inputs if inputs is not None else row_inputs(row)
    rs = runway_score(
        day_chg_pct=got["chg"], source=_source(row), rvol_pace=got["pace"], now=now)
    close = zone_closeness(got, cfg, mid_rise_level=mid_rise_level)
    return rs + float(closeness_w) * close


def filter_supply(rows: list[dict]) -> list[dict]:
    """Keep Movers / Trending / Research only."""
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if _source(r) in SUPPLY_SOURCES or any(
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
    indicators: dict[str, dict] | None = None,
    paces: dict[str, float] | None = None,
) -> list[dict]:
    """Supply rows sorted by (fast %R known, seat_priority), capped.

    Returns copies; the caller's rows are never mutated (they also feed the
    live inclusion gate).
    """
    cfg = cfg or {}
    try:
        level = float(cfg.get("ai_watch_mid_rise_level", -50.0) or -50.0)
    except (TypeError, ValueError):
        level = -50.0
    scored = []
    seen: set[str] = set()
    for r in filter_supply(rows):
        sym = _symbol(r)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        got = row_inputs(r, indicators=indicators, paces=paces)
        pri = seat_priority(
            r, now=now, mid_rise_level=level, inputs=got, cfg=cfg)
        close = zone_closeness(got, cfg, mid_rise_level=level)
        out = dict(r)
        out["_book_server_priority"] = round(pri, 4)
        out["_book_server_runway"] = round(pri - _CLOSENESS_W * close, 4)
        out["_book_server_square_close"] = round(close, 4)
        out["_book_server_inputs"] = got
        scored.append(out)
    # Prefer dual-%R known, then priority. Square arm needs both lines.
    scored.sort(key=lambda x: (
        x["_book_server_inputs"]["pctr"] is not None
        and x["_book_server_inputs"].get("pctr_slow") is not None,
        x["_book_server_inputs"]["pctr"] is not None,
        float(x.get("_book_server_priority") or 0),
    ), reverse=True)
    return scored[: max(0, int(limit))]


def _shadow_path(day: str | None = None) -> Path:
    if day is None:
        day = datetime.now(ET).strftime("%Y-%m-%d")
    try:
        import ai_paths
        d = ai_paths.resolve_report_dir() / "book_server_shadow"
    except Exception:  # noqa: BLE001
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
    indicators: dict[str, dict] | None = None,
    paces: dict[str, float] | None = None,
) -> list[dict]:
    """Build a ranked would-be book and log the diff vs live. Returns ranked seats.

    Logs when the would-be or live set changes, else once a minute (the book
    rebuilds every few seconds).
    """
    cfg = cfg or {}
    now = float(now if now is not None else time.time())
    ranked = rank_candidates(candidates, cfg=cfg, now=now, limit=max_seats * 2,
                             indicators=indicators, paces=paces)
    would_seat = ranked[:max_seats]
    live = {str(s).upper() for s in (live_book or [])}
    would = {_symbol(r) for r in would_seat}
    would.discard("")
    key = (tuple(sorted(would)), tuple(sorted(live)))
    if key == _last_shadow["key"] and now - float(_last_shadow["ts"]) < _SHADOW_HEARTBEAT_SEC:
        return would_seat
    _last_shadow["key"], _last_shadow["ts"] = key, now

    def _n(field: str) -> int:
        return sum(1 for r in ranked if r["_book_server_inputs"][field] is not None)

    log_shadow({
        "ts": now,
        "kind": "shadow_book",
        "n_candidates": len(candidates),
        "n_ranked": len(ranked),
        "n_would_seat": len(would_seat),
        # How much of the ranking stood on real numbers this tick.
        "coverage": {"pctr": _n("pctr"), "pace": _n("pace"), "chg": _n("chg")},
        "would_symbols": sorted(would),
        "live_symbols": sorted(live),
        "add": sorted(would - live),
        "drop": sorted(live - would),
        "top": [
            {
                "symbol": _symbol(r),
                "source": r.get("source") or r.get("src"),
                "priority": r.get("_book_server_priority"),
                "runway": r.get("_book_server_runway"),
                "square_close": r.get("_book_server_square_close"),
                **{k: v for k, v in (r.get("_book_server_inputs") or {}).items()
                   if k in ("chg", "pace", "pace_src", "pctr", "pctr_slow")},
            }
            for r in would_seat[:12]
        ],
    })
    return would_seat
