"""Phase B — premarket session extension (Hybrid C).

Paper/dry plumbing for trading ~04:00–09:28 ET on Finnhub stream-last +
Alpaca extended-hours DAY limits. Default OFF / dry. Isolated from RTH
Plan A arms, seats, and elite pins.

Design freeze: ``docs/PHASE_B_PREMARKET.md``.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Admit priority for Phase B book (lower index = higher priority).
SOURCE_PRIORITY = (
    "momentum",
    "movers",
    "mention_burst",
    "burst",
    "trending",
)
# Explicitly off for v1 Phase B book (RTH morning flood stays separate).
SOURCE_OFF_V1 = frozenset({"research", "seed_rank"})
# WP1 default allow-list when ai_phase_b_sources_allow is unset/empty in older
# configs — momentum+movers only (SIP-computable preference).
SOURCES_ALLOW_V1_DEFAULT = ("momentum", "movers")

_BOOK_LOCK = threading.Lock()
_BOOK_OVERRIDE: Path | None = None
_BOOK: dict[str, dict[str, Any]] = {}
# ET day → max concurrent Phase B opens observed this process.
_PEAK_OPENS_BY_DAY: dict[str, int] = {}
# (symbol, prior_day) → dollar volume or None (negative cache).
_PRIOR_DVOL_CACHE: dict[tuple[str, str], float | None] = {}


def _cfg(cfg: dict | None = None) -> dict:
    if isinstance(cfg, dict):
        return cfg
    try:
        from config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _parse_hhmm(raw: str | None, default: tuple[int, int]) -> tuple[int, int]:
    s = str(raw or "").strip()
    if not s or ":" not in s:
        return default
    try:
        h_s, m_s = s.split(":", 1)
        h, m = int(h_s), int(m_s)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (TypeError, ValueError):
        pass
    return default


def _et_dt(now: float | None = None) -> datetime:
    t = float(now if now is not None else time.time())
    return datetime.fromtimestamp(t, tz=ET)


def _mins(dt: datetime) -> int:
    return int(dt.hour) * 60 + int(dt.minute)


def _hhmm_default(default_hhmm: str) -> tuple[int, int]:
    return _parse_hhmm(default_hhmm, (0, 0))


def _window_mins(cfg: dict, key: str, default_hhmm: str) -> int:
    h, m = _parse_hhmm(str(cfg.get(key) or default_hhmm), _hhmm_default(default_hhmm))
    return h * 60 + m


def enabled(cfg: dict | None = None) -> bool:
    """Master switch — no Phase B buys when false."""
    return bool(_cfg(cfg).get("ai_phase_b_enabled", False))


def dry_run(cfg: dict | None = None) -> bool:
    """When enabled+dry: log/score path, place nothing on the broker."""
    return bool(_cfg(cfg).get("ai_phase_b_dry_run", True))


def ledger_enabled(cfg: dict | None = None) -> bool:
    return bool(_cfg(cfg).get("ai_phase_b_ledger_enabled", True))


def no_rth_handoff(cfg: dict | None = None) -> bool:
    return bool(_cfg(cfg).get("ai_phase_b_no_rth_handoff", True))


def working_sell_on(cfg: dict | None = None) -> bool:
    """Phase-B-scoped working sell (does not flip global ai_premarket_working_sell)."""
    return bool(_cfg(cfg).get("ai_phase_b_working_sell", True))


def min_price(cfg: dict | None = None) -> float:
    c = _cfg(cfg)
    raw = c.get("ai_phase_b_min_price", None)
    if raw is None or raw == "":
        try:
            return float(c.get("ai_watch_min_price", 2.0) or 2.0)
        except (TypeError, ValueError):
            return 2.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 2.0


def min_prior_dollar_vol(cfg: dict | None = None) -> float:
    """Prior RTH-day close×volume floor. ≤0 disables the liquidity gate."""
    try:
        return float(_cfg(cfg).get("ai_phase_b_min_prior_dollar_vol", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def sources_allow(cfg: dict | None = None) -> frozenset[str] | None:
    """Allowed admit sources, or None when unrestricted (full SOURCE_PRIORITY).

    ``DEFAULT_CONFIG`` sets ``momentum,movers`` (WP1). Explicit empty / ``*`` /
    ``all`` restores mention/trending. Key absent on a partial cfg → None so
    unit tests that omit the knob keep legacy priority behavior.
    Research/seed_rank stay off via SOURCE_OFF_V1 either way.
    """
    c = _cfg(cfg)
    if "ai_phase_b_sources_allow" not in c:
        return None
    raw = c.get("ai_phase_b_sources_allow")
    if raw is None:
        return frozenset(SOURCES_ALLOW_V1_DEFAULT)
    if isinstance(raw, (list, tuple, set, frozenset)):
        parts = [str(x).strip().lower() for x in raw if str(x).strip()]
    else:
        s = str(raw).strip().lower()
        if not s or s in ("*", "all", "any"):
            return None
        parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return None
    return frozenset(parts)


def source_allowed(source: str | None, cfg: dict | None = None) -> bool:
    src = str(source or "").strip().lower()
    if not src or src in SOURCE_OFF_V1:
        return False
    allow = sources_allow(cfg)
    if allow is None:
        return True
    if src in allow:
        return True
    # Soft-seed tags sometimes embed the board name ("soft_seed_momentum").
    return any(a in src or src.endswith(a) for a in allow)


def _prior_weekday(day: str) -> str:
    from datetime import timedelta
    d = datetime.fromisoformat(day).date()
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def prior_day_dollar_vol(
    symbol: str,
    *,
    day: str | None = None,
    now: float | None = None,
    client=None,
) -> float | None:
    """Prior RTH weekday close × volume via delayed SIP daily bars (research).

    Returns None when unknown (caller fail-closes when floor > 0). Cached per
    (symbol, prior_day) for the process lifetime.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None
    if day is None:
        day = _et_dt(now).strftime("%Y-%m-%d")
    prior = _prior_weekday(day)
    key = (sym, prior)
    if key in _PRIOR_DVOL_CACHE:
        return _PRIOR_DVOL_CACHE[key]

    dvol: float | None = None
    try:
        from datetime import timedelta, timezone
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed

        if client is None:
            try:
                import sys
                tools = str(Path(__file__).resolve().parent / "tools")
                if tools not in sys.path:
                    sys.path.insert(0, tools)
                import bars as _bars
                client = _bars.client()
            except Exception:
                client = None
        if client is None:
            _PRIOR_DVOL_CACHE[key] = None
            return None

        start = datetime.fromisoformat(prior).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=ET,
        )
        end = start + timedelta(days=1)
        df = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Day,
            start=start.astimezone(timezone.utc),
            end=end.astimezone(timezone.utc),
            limit=5,
            feed=DataFeed.SIP,
        )).df
        if df is not None and not getattr(df, "empty", True):
            if getattr(df.index, "nlevels", 1) > 1:
                df = df.xs(sym, level="symbol")
            row = df.iloc[-1]
            dvol = float(row["close"]) * float(row["volume"])
            if dvol < 0 or dvol != dvol:  # NaN
                dvol = None
    except Exception:
        dvol = None
    _PRIOR_DVOL_CACHE[key] = dvol
    return dvol


def clear_prior_dvol_cache() -> None:
    _PRIOR_DVOL_CACHE.clear()


def liquidity_ok(
    row: dict | None = None,
    cfg: dict | None = None,
    *,
    symbol: str | None = None,
    dollar_vol: float | None = None,
    day: str | None = None,
    now: float | None = None,
) -> tuple[bool, str]:
    """Price floor + prior-day dollar-volume floor. Fail closed on unknown dvol."""
    c = _cfg(cfg)
    row = row if isinstance(row, dict) else {}
    sym = str(symbol or row.get("symbol") or "").upper().strip()
    px = row.get("price")
    if not price_ok_for_book(px, c):
        return False, "phase_b_min_price"

    floor = min_prior_dollar_vol(c)
    if floor <= 0:
        return True, "ok"

    dvol = dollar_vol
    if dvol is None and row.get("prior_dollar_vol") is not None:
        try:
            dvol = float(row.get("prior_dollar_vol"))
        except (TypeError, ValueError):
            dvol = None
    if dvol is None and row.get("dollar_volume") is not None:
        try:
            dvol = float(row.get("dollar_volume"))
        except (TypeError, ValueError):
            dvol = None
    if dvol is None and sym:
        dvol = prior_day_dollar_vol(sym, day=day, now=now)

    if dvol is None:
        return False, "phase_b_liquidity_unknown"
    if float(dvol) < float(floor):
        return False, "phase_b_liquidity"
    return True, "ok"


def max_seats(cfg: dict | None = None) -> int:
    try:
        return max(1, int(_cfg(cfg).get("ai_phase_b_max_seats", 6) or 6))
    except (TypeError, ValueError):
        return 6


def max_open(cfg: dict | None = None) -> int:
    try:
        return max(1, int(_cfg(cfg).get("ai_phase_b_max_open", 2) or 2))
    except (TypeError, ValueError):
        return 2


def print_max_age_sec(cfg: dict | None = None) -> float:
    try:
        return max(0.0, float(_cfg(cfg).get("ai_phase_b_print_max_age_sec", 15.0) or 15.0))
    except (TypeError, ValueError):
        return 15.0


def entry_limit_ttl_sec(cfg: dict | None = None) -> float:
    try:
        return max(0.0, float(_cfg(cfg).get("ai_phase_b_entry_limit_ttl_sec", 45.0) or 45.0))
    except (TypeError, ValueError):
        return 45.0


def hard_stop_pct(cfg: dict | None = None) -> float:
    try:
        return max(0.0, float(_cfg(cfg).get("ai_phase_b_hard_stop_pct", 5.0) or 5.0))
    except (TypeError, ValueError):
        return 5.0


def chase_step_sec(cfg: dict | None = None) -> float:
    try:
        return max(0.0, float(_cfg(cfg).get("ai_phase_b_chase_step_sec", 2.5) or 2.5))
    except (TypeError, ValueError):
        return 2.5


def max_exit_slip_r(cfg: dict | None = None) -> float:
    try:
        return max(0.0, float(_cfg(cfg).get("ai_phase_b_max_exit_slip_r", 0.25) or 0.25))
    except (TypeError, ValueError):
        return 0.25


def seat_stale_sec(cfg: dict | None = None) -> float:
    """Demote if no fresh last for this many seconds after seat (~60–90)."""
    try:
        return max(0.0, float(_cfg(cfg).get("ai_phase_b_seat_stale_sec", 75.0) or 75.0))
    except (TypeError, ValueError):
        return 75.0


# ── Clock gates ──────────────────────────────────────────────────────────


def phase_b_session_active(now: float | None = None, cfg: dict | None = None) -> bool:
    """True on weekdays from start through flat deadline (exclusive of RTH).

    Arms/entries are further gated by ``phase_b_allow_entries``. Session
    stays active through flatten so exits can run.
    """
    c = _cfg(cfg)
    if not enabled(c):
        return False
    dt = _et_dt(now)
    if dt.weekday() >= 5:
        return False
    mins = _mins(dt)
    start = _window_mins(c, "ai_phase_b_start_time", "04:00")
    flat = _window_mins(c, "ai_phase_b_flat_deadline", "09:28")
    return start <= mins < flat


def phase_b_allow_entries(now: float | None = None, cfg: dict | None = None) -> bool:
    """True 04:00–09:20 ET when Phase B is enabled (new entry limits only)."""
    c = _cfg(cfg)
    if not enabled(c):
        return False
    dt = _et_dt(now)
    if dt.weekday() >= 5:
        return False
    mins = _mins(dt)
    start = _window_mins(c, "ai_phase_b_start_time", "04:00")
    cutoff = _window_mins(c, "ai_phase_b_entry_cutoff", "09:20")
    return start <= mins < cutoff


def phase_b_flatten_active(now: float | None = None, cfg: dict | None = None) -> bool:
    """True 09:25–09:28 ET (flatten window)."""
    c = _cfg(cfg)
    if not enabled(c):
        return False
    dt = _et_dt(now)
    if dt.weekday() >= 5:
        return False
    mins = _mins(dt)
    start = _window_mins(c, "ai_phase_b_flatten_start", "09:25")
    flat = _window_mins(c, "ai_phase_b_flat_deadline", "09:28")
    return start <= mins < flat


def phase_b_past_flat_deadline(now: float | None = None, cfg: dict | None = None) -> bool:
    """True at/after 09:28 ET on a weekday (must be flat)."""
    c = _cfg(cfg)
    dt = _et_dt(now)
    if dt.weekday() >= 5:
        return False
    mins = _mins(dt)
    flat = _window_mins(c, "ai_phase_b_flat_deadline", "09:28")
    return mins >= flat


def phase_b_drain_only(now: float | None = None, cfg: dict | None = None) -> bool:
    """After flatten start: no new admits."""
    c = _cfg(cfg)
    if not enabled(c):
        return False
    dt = _et_dt(now)
    if dt.weekday() >= 5:
        return False
    mins = _mins(dt)
    start = _window_mins(c, "ai_phase_b_flatten_start", "09:25")
    return mins >= start


# ── Finnhub-last price path ──────────────────────────────────────────────


def stream_last_print(
    ticker: str,
    *,
    now: float | None = None,
    cfg: dict | None = None,
) -> dict[str, Any] | None:
    """Finnhub stream last + age. None if missing.

    Prefers ``get_latest_print`` when present; falls back to price + ts_unix.
    """
    sym = str(ticker or "").upper().strip()
    if not sym:
        return None
    t0 = float(now if now is not None else time.time())
    try:
        import finnhub_stream as fh
        if hasattr(fh, "get_latest_print"):
            got = fh.get_latest_print(sym)
            if isinstance(got, dict) and got.get("price"):
                age = got.get("age_sec")
                if age is None:
                    ts = got.get("trade_ts") or got.get("ts_unix")
                    age = (t0 - float(ts)) if ts else None
                return {
                    "price": float(got["price"]),
                    "trade_ts": got.get("trade_ts"),
                    "ts_unix": got.get("ts_unix"),
                    "age_sec": float(age) if age is not None else None,
                }
        with fh.FINNHUB_STATE.lock:
            data = fh.FINNHUB_STATE.prices.get(sym)
            if not data or not data.get("price"):
                return None
            px = float(data["price"])
            trade_ts = data.get("trade_ts")
            ts_unix = data.get("ts_unix")
            basis = trade_ts if trade_ts is not None else ts_unix
            age = (t0 - float(basis)) if basis is not None else None
            return {
                "price": px,
                "trade_ts": trade_ts,
                "ts_unix": ts_unix,
                "age_sec": age,
            }
    except Exception:
        return None


def fresh_stream_last(
    ticker: str,
    *,
    now: float | None = None,
    cfg: dict | None = None,
) -> tuple[float | None, str | None]:
    """Return (last, None) when fresh, else (None, reason_token)."""
    c = _cfg(cfg)
    got = stream_last_print(ticker, now=now, cfg=c)
    if got is None or not got.get("price"):
        return None, "phase_b_missing_print"
    age = got.get("age_sec")
    max_age = print_max_age_sec(c)
    if age is None or float(age) > max_age:
        return None, "phase_b_stale_print"
    return float(got["price"]), None


def entry_limit_price(
    last: float,
    *,
    cfg: dict | None = None,
) -> float | None:
    """Limit = last * (1+pad) capped by pad_max_px. Anchored on stream last."""
    try:
        px = float(last)
    except (TypeError, ValueError):
        return None
    if px <= 0:
        return None
    c = _cfg(cfg)
    try:
        pad = max(0.0, float(c.get("ai_phase_b_entry_limit_pad_pct", 0.15) or 0.0)) / 100.0
    except (TypeError, ValueError):
        pad = 0.0015
    try:
        cap = float(c.get("ai_phase_b_entry_limit_pad_max_px", 0.05) or 0.0)
    except (TypeError, ValueError):
        cap = 0.05
    lim = px * (1.0 + pad)
    if cap > 0:
        lim = min(lim, px + cap)
    lim = round(lim, 2)
    return lim if lim > 0 else None


# ── Arm strip (Phase B only) ─────────────────────────────────────────────


def _ind(record: dict | None) -> dict:
    if not isinstance(record, dict):
        return {}
    ind = record.get("indicator")
    return ind if isinstance(ind, dict) else {}


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _legacy_arm_allows(record: dict, c: dict) -> tuple[bool, str]:
    """The pre-2026-09-18 Phase B arm: 40-70 exhaustion band + direction RSI.

    Kept behind ``ai_phase_b_legacy_arm`` so the change is reversible without
    a deploy. Note what it does NOT do, which is why it conflicted with the
    square: it refuses exhaustion above ai_phase_b_exh_max (70) outright, and
    its RSI leg lets a FLAT reading pass — it blocks only on actively falling.
    RTH treats flat as not-rising.
    """
    ind = _ind(record)
    try:
        exh_min = float(c.get("ai_phase_b_exh_min", 40.0) or 40.0)
    except (TypeError, ValueError):
        exh_min = 40.0
    try:
        exh_max = float(c.get("ai_phase_b_exh_max", 70.0) or 70.0)
    except (TypeError, ValueError):
        exh_max = 70.0
    exh = _f(ind.get("exhaustion"))
    if exh is None and isinstance(record, dict):
        exh = _f(record.get("exhaustion"))
    if exh is None:
        raw = _f(ind.get("pctr"))
        if raw is None and isinstance(record, dict):
            raw = _f(record.get("pctr"))
        if raw is not None:
            exh = (100.0 + float(raw)) if float(raw) <= 0.0 else float(raw)
    if exh is None:
        return False, "phase_b_no_exh"
    if exh < exh_min or exh > exh_max:
        return False, "phase_b_exh_band"

    if bool(c.get("ai_phase_b_require_exh_rising", True)):
        if ind.get("pctr_falling"):
            return False, "phase_b_exh_falling"
        if not ind.get("pctr_rising"):
            return False, "phase_b_exh_not_rising"

    try:
        rsi_block = float(c.get("ai_phase_b_rsi_block_falling_above", 10.0) or 10.0)
    except (TypeError, ValueError):
        rsi_block = 10.0
    rsi = _f(ind.get("cm_rsi"))
    if rsi is None and isinstance(record, dict):
        rsi = _f(record.get("cm_rsi"))
    rsi_rising = ind.get("cm_rsi_rising")
    rsi_falling = ind.get("cm_rsi_falling")
    if rsi_falling is None and rsi_rising is False:
        rsi_falling = True
    if rsi is not None and rsi_falling and float(rsi) > rsi_block:
        return False, "phase_b_rsi_falling"
    return True, "phase_b_arm_legacy"


def phase_b_arm_allows(
    record: dict,
    *,
    cfg: dict | None = None,
    now: float | None = None,
    last: float | None = None,
) -> tuple[bool, str]:
    """Session mechanics here; the indicator arm is RTH's, shared not copied.

    Phase B used to carry its own exhaustion band (ai_phase_b_exh_min/max,
    40-70) and its own direction-only RSI rule. That put the two lanes in
    direct contradiction once the square arm shipped: RTH enters on the dual-%R
    red square, which needs BOTH lines overbought (exhaustion >= 100-
    rte_threshold, i.e. 80 by default), and Phase B refused anything over 70 as
    phase_b_exh_band. Premarket was gated to reject exactly the state the
    thesis says to buy.

    So the indicator legs now call the same functions the RTH arm calls —
    exhaustion_allows_buy (square/triangle) and cm_rsi_allows_buy (direction)
    — rather than reimplementing them. Shared, so the lanes cannot drift apart
    again: a change to the square is a change to both.

    What stays Phase B's own is session mechanics, which are genuinely
    different premarket: the entry clock, print freshness, seat caps and the
    confirm-tick streak.

    ``ai_phase_b_legacy_arm`` restores the old band + direction-only RSI for
    rollback without a code change.

    Returns ``(True, reason)`` or ``(False, reason_token)``.
    """
    c = _cfg(cfg)
    if not enabled(c):
        return False, "phase_b_disabled"
    if not phase_b_allow_entries(now, c):
        return False, "phase_b_outside_entries"

    sym = ""
    if isinstance(record, dict):
        sym = str(record.get("symbol") or "").upper().strip()

    px = last
    if px is None:
        px, why = fresh_stream_last(sym, now=now, cfg=c)
        if px is None:
            return False, why or "phase_b_missing_print"
    else:
        # Caller supplied last — still refuse if stream is stale when we can check.
        _, why = fresh_stream_last(sym, now=now, cfg=c) if sym else (px, None)
        if sym and why:
            return False, why

    if not bool(c.get("ai_phase_b_legacy_arm", False)):
        # Shared with RTH. exhaustion_allows_buy routes to the square arm when
        # ai_watch_exh_square_arm is on (it is), so premarket and RTH agree on
        # what a good entry looks like by construction rather than by two
        # sets of knobs that happen to match.
        try:
            import ai_entry_watch as ew
        except Exception as e:  # noqa: BLE001
            return False, f"phase_b_arm_unavailable:{e}"

        ok_exh, why_exh = ew.exhaustion_allows_buy(record, c)
        if not ok_exh:
            return False, why_exh

        ok_rsi, why_rsi = ew.cm_rsi_allows_buy(record, c)
        if not ok_rsi:
            return False, why_rsi
    else:
        ok_legacy, why_legacy = _legacy_arm_allows(record, c)
        if not ok_legacy:
            return False, why_legacy

    # Confirm ticks (default 1) — one fresh last is enough when ticks==1.
    try:
        need = max(1, int(c.get("ai_phase_b_arm_confirm_ticks", 1) or 1))
    except (TypeError, ValueError):
        need = 1
    if need > 1 and isinstance(record, dict):
        streak = int(record.get("phase_b_confirm_ticks") or 0)
        if streak < need:
            record["phase_b_confirm_ticks"] = streak + 1
            return False, "phase_b_confirm"
        record["phase_b_confirm_ticks"] = 0

    return True, "phase_b_arm"


# ── Universe / book ──────────────────────────────────────────────────────


def source_rank(source: str | None) -> int:
    s = str(source or "").strip().lower()
    if s in SOURCE_OFF_V1:
        return 10_000
    for i, name in enumerate(SOURCE_PRIORITY):
        if s == name or s.endswith(name) or name in s:
            return i
    return len(SOURCE_PRIORITY)


def price_ok_for_book(price: float | None, cfg: dict | None = None) -> bool:
    """Sub-$5 allowed; floor is ai_phase_b_min_price or ai_watch_min_price."""
    floor = min_price(cfg)
    try:
        px = float(price) if price is not None else None
    except (TypeError, ValueError):
        px = None
    if px is None:
        return True  # unknown price — do not invent a $5 reject
    return px >= floor


def sort_candidates(rows: list[dict], cfg: dict | None = None) -> list[dict]:
    """Momentum → movers (WP1 allow-list); drop research/seed_rank.

    Prior-day $-vol is enforced in ``admit_candidate`` (may hit the daily-bar
    cache). Sort only applies cheap source + price filters so soft-seed scans
    stay free of per-row REST calls.
    """
    c = _cfg(cfg)
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        src = str(r.get("source") or "").strip().lower()
        if not source_allowed(src, c):
            continue
        if not price_ok_for_book(r.get("price"), c):
            continue
        out.append(r)
    out.sort(key=lambda r: (
        source_rank(r.get("source")),
        -float(r.get("score") or r.get("pct_change") or 0.0),
        str(r.get("symbol") or ""),
    ))
    return out


def admit_candidate(
    row: dict,
    book: dict[str, dict],
    *,
    open_count: int = 0,
    cfg: dict | None = None,
    now: float | None = None,
) -> tuple[bool, str]:
    """Admit into Phase B book. Returns (ok, reason)."""
    c = _cfg(cfg)
    if not enabled(c):
        return False, "phase_b_disabled"
    if phase_b_drain_only(now, c):
        return False, "phase_b_drain_only"
    # Admits only while session is live and before flatten drain.
    if not phase_b_session_active(now, c):
        return False, "phase_b_outside_session"
    sym = str(row.get("symbol") or "").upper().strip()
    if not sym:
        return False, "no_symbol"
    src = str(row.get("source") or "").strip().lower()
    if src in SOURCE_OFF_V1 or not source_allowed(src, c):
        return False, "phase_b_source_off"
    ok_liq, why_liq = liquidity_ok(row, c, symbol=sym, now=now)
    if not ok_liq:
        return False, why_liq
    if sym in book:
        return False, "already_seated"
    seats = max_seats(c)
    if len(book) >= seats:
        # Tight seats: only admit if higher priority than worst seated.
        worst = max(
            book.values(),
            key=lambda r: (source_rank(r.get("source")), str(r.get("symbol") or "")),
        )
        if source_rank(src) >= source_rank(worst.get("source")):
            return False, "phase_b_seats_full"
    if open_count >= max_open(c):
        return False, "phase_b_max_open"
    return True, "ok"


def can_open_another(open_count: int, cfg: dict | None = None) -> tuple[bool, str]:
    if open_count >= max_open(cfg):
        return False, "phase_b_max_open"
    return True, "ok"


# ── Book state ───────────────────────────────────────────────────────────


def _book_path() -> Path:
    if _BOOK_OVERRIDE is not None:
        return _BOOK_OVERRIDE
    try:
        from ai_paths import resolve_report_dir
        return resolve_report_dir() / "phase_b_book.json"
    except Exception:
        return Path(__file__).resolve().parent / "ai_reports" / "phase_b_book.json"


def set_book_path_for_tests(path: Path | None) -> None:
    global _BOOK_OVERRIDE, _BOOK
    _BOOK_OVERRIDE = path
    _BOOK = {}


def load_book() -> dict[str, dict[str, Any]]:
    with _BOOK_LOCK:
        if _BOOK:
            return {k: dict(v) for k, v in _BOOK.items()}
        path = _book_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _BOOK.clear()
                for k, v in raw.items():
                    if isinstance(v, dict):
                        _BOOK[str(k).upper()] = dict(v)
        except Exception:
            pass
        return {k: dict(v) for k, v in _BOOK.items()}


def save_book(book: dict[str, dict[str, Any]]) -> None:
    with _BOOK_LOCK:
        _BOOK.clear()
        for k, v in (book or {}).items():
            if isinstance(v, dict):
                _BOOK[str(k).upper()] = dict(v)
        path = _book_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(_BOOK, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass


def seat_symbol(
    row: dict,
    *,
    cfg: dict | None = None,
    now: float | None = None,
    open_count: int = 0,
) -> tuple[bool, str]:
    """Admit one candidate into the Phase B book."""
    book = load_book()
    ok, why = admit_candidate(row, book, open_count=open_count, cfg=cfg, now=now)
    if not ok:
        return False, why
    t0 = float(now if now is not None else time.time())
    sym = str(row.get("symbol") or "").upper().strip()
    rec = {
        "symbol": sym,
        "source": str(row.get("source") or "").strip().lower(),
        "price": row.get("price"),
        "score": row.get("score"),
        "pct_change": row.get("pct_change"),
        "admit_ts": t0,
        "last_print_ts": t0,
        "phase_b": True,
        "status": "watching",
        "indicator": row.get("indicator") if isinstance(row.get("indicator"), dict) else {},
    }
    # Evict worst if over capacity after higher-priority admit.
    seats = max_seats(cfg)
    if len(book) >= seats:
        worst_sym = max(
            book.keys(),
            key=lambda s: (
                source_rank(book[s].get("source")),
                str(s),
            ),
        )
        book.pop(worst_sym, None)
    book[sym] = rec
    save_book(book)
    try:
        import phase_b_ledger as pbl
        pbl.log_event("admit", symbol=sym, source=rec["source"], cfg=cfg, ts=t0)
    except Exception:
        pass
    return True, "admitted"


def refresh_seat_indicators(
    book: dict[str, dict[str, Any]] | None = None,
    *,
    cfg: dict | None = None,
    now: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Stamp live EXH/RSI onto seated Phase B symbols (admit-time ind is often {}).

    Sources (first hit wins fields): engine indicator map, then
    ``ai_entry_watch.load_watch`` row, then ``live_exhaustion`` on stream last.
    Mutates and returns the book dict.
    """
    c = _cfg(cfg)
    t0 = float(now if now is not None else time.time())
    book = book if isinstance(book, dict) else load_book()
    if not book:
        return book

    eng: dict[str, dict] = {}
    try:
        import ai_entry_watch as ew
        eng = ew._engine_indicator_map() or {}
    except Exception:
        eng = {}

    watch: dict[str, dict] = {}
    try:
        import ai_entry_watch as ew
        if hasattr(ew, "load_watch"):
            raw = ew.load_watch() or {}
            if isinstance(raw, dict):
                nested = raw.get("watch") or raw.get("records")
                src = nested if isinstance(nested, dict) else raw
                for k, v in src.items():
                    if isinstance(v, dict):
                        watch[str(k).upper()] = v
    except Exception:
        watch = {}

    changed = False
    for sym, rec in list(book.items()):
        if not isinstance(rec, dict):
            continue
        ind = dict(_ind(rec))
        before = dict(ind)

        e = eng.get(sym) if isinstance(eng.get(sym), dict) else {}
        wrec = watch.get(sym) if isinstance(watch.get(sym), dict) else {}
        wind = wrec.get("indicator") if isinstance(wrec.get("indicator"), dict) else {}

        for src in (e, wind):
            for k, v in src.items():
                if v is not None and (k not in ind or ind.get(k) is None):
                    ind[k] = v
                elif k in ("pctr", "pctr_rising", "pctr_falling", "cm_rsi",
                           "cm_rsi_rising", "cm_rsi_falling", "cm_rsi_src",
                           "pctr_src", "exhaustion"):
                    if v is not None:
                        ind[k] = v

        # Live recompute against stream last when still missing EXH direction.
        last, _why = fresh_stream_last(sym, now=t0, cfg=c)
        if last is not None:
            try:
                import ai_entry_watch as ew
                got = ew.live_exhaustion(sym, float(last), c, t0)
                if got:
                    pctr_w, exh, rising, falling = got
                    ind["pctr_williams"] = pctr_w
                    ind["exhaustion"] = exh
                    # Phase B arm band reads exhaustion; keep rising bits honest.
                    ind["pctr_rising"] = bool(rising)
                    ind["pctr_falling"] = bool(falling)
                    # Also expose 0–100 on pctr for seats that only look there.
                    if exh is not None:
                        ind["pctr"] = float(exh)
            except Exception:
                pass
            rec["price"] = float(last)
            rec["last_print_ts"] = t0

        # Normalize: if only Williams pctr present, derive exhaustion.
        if ind.get("exhaustion") is None:
            raw = _f(ind.get("pctr"))
            if raw is not None and float(raw) <= 0.0:
                ind["exhaustion"] = max(0.0, min(100.0, 100.0 + float(raw)))

        rec["indicator"] = ind
        rec["indicator_ts"] = t0
        book[sym] = rec
        if ind != before:
            changed = True

    if changed or book:
        save_book(book)
    return book


def demote_stale_seats(
    *,
    now: float | None = None,
    cfg: dict | None = None,
) -> list[str]:
    """Drop seats with no fresh last for seat_stale_sec."""
    c = _cfg(cfg)
    t0 = float(now if now is not None else time.time())
    stale_lim = seat_stale_sec(c)
    if stale_lim <= 0:
        return []
    book = load_book()
    dropped: list[str] = []
    for sym, rec in list(book.items()):
        px, why = fresh_stream_last(sym, now=t0, cfg=c)
        if px is not None:
            rec["last_print_ts"] = t0
            rec["price"] = px
            book[sym] = rec
            continue
        last_ts = _f(rec.get("last_print_ts")) or _f(rec.get("admit_ts")) or 0.0
        if (t0 - float(last_ts)) >= stale_lim:
            book.pop(sym, None)
            dropped.append(sym)
            try:
                import phase_b_ledger as pbl
                pbl.log_event(
                    "demote", symbol=sym, reason=why or "phase_b_seat_stale",
                    cfg=c, ts=t0,
                )
            except Exception:
                pass
    if dropped:
        save_book(book)
    else:
        save_book(book)
    return dropped


# ── Entry / exit plumbing ────────────────────────────────────────────────


def is_phase_b_position(pos: dict | None) -> bool:
    if not isinstance(pos, dict):
        return False
    if pos.get("phase_b") is True:
        return True
    if str(pos.get("entry_path") or "").strip().lower() == "phase_b":
        return True
    if str(pos.get("session") or "").strip().lower() == "phase_b":
        return True
    return False


def build_entry_decision(
    record: dict,
    *,
    last: float,
    cfg: dict | None = None,
) -> dict[str, Any]:
    """Minimal decision dict for place_scaled_entry / dry entry."""
    c = _cfg(cfg)
    stop_pct = hard_stop_pct(c) / 100.0
    stop = round(float(last) * (1.0 - stop_pct), 4)
    # Soft target for sizing contract (working sell owns the real exit).
    target = round(float(last) * 1.02, 4)
    lim = entry_limit_price(last, cfg=c)
    return {
        "phase_b": True,
        "session": "phase_b",
        "entry_path": "phase_b",
        "skip_zone": True,
        "entry_low": float(last),
        "entry_high": float(lim or last),
        "stop_price": stop,
        "target_1": target,
        "scale_out_pct": 0.0,
        "synthetic": True,
        "strategy": "phase_b",
        "source": record.get("source") or "phase_b",
        "duel_source": record.get("source") or "phase_b",
        "zone_kind": "phase_b",
        "entry_limit_price": lim,
        "arm_last": float(last),
    }


def try_phase_b_entry(
    record: dict,
    *,
    equity: float,
    cfg: dict | None = None,
    now: float | None = None,
    open_count: int = 0,
    place_fn=None,
) -> dict[str, Any]:
    """Arm strip → limit entry. Dry run logs only; never places when dry.

    ``place_fn`` injectable for tests: ``(sym, decision, equity, **kw) -> dict``.
    """
    c = _cfg(cfg)
    t0 = float(now if now is not None else time.time())
    sym = str(record.get("symbol") or "").upper().strip()
    out: dict[str, Any] = {"ok": False, "symbol": sym, "dry_run": dry_run(c)}

    if not enabled(c):
        out["error"] = "phase_b_disabled"
        return out
    if not phase_b_allow_entries(t0, c):
        out["error"] = "phase_b_outside_entries"
        return out
    ok_open, why_open = can_open_another(open_count, c)
    if not ok_open:
        out["error"] = why_open
        return out

    ok_arm, why_arm = phase_b_arm_allows(record, cfg=c, now=t0)
    if not ok_arm:
        out["error"] = why_arm
        try:
            import phase_b_ledger as pbl
            pbl.log_event("arm_refuse", symbol=sym, reason=why_arm, cfg=c, ts=t0)
        except Exception:
            pass
        return out

    last, why_px = fresh_stream_last(sym, now=t0, cfg=c)
    if last is None:
        out["error"] = why_px or "phase_b_missing_print"
        return out

    decision = build_entry_decision(record, last=last, cfg=c)
    lim = decision.get("entry_limit_price")
    out["arm_last"] = last
    out["limit_px"] = lim
    out["reason"] = why_arm

    try:
        import phase_b_ledger as pbl
        pbl.log_event(
            "arm",
            symbol=sym,
            reason=why_arm,
            last=last,
            limit_px=lim,
            source=record.get("source"),
            cfg=c,
            ts=t0,
        )
        pbl.log_event(
            "entry_limit",
            symbol=sym,
            last=last,
            limit_px=lim,
            dry_run=dry_run(c),
            ttl_sec=entry_limit_ttl_sec(c),
            cfg=c,
            ts=t0,
        )
    except Exception:
        pass

    if dry_run(c):
        out["ok"] = True
        out["status"] = "dry_shadow"
        out["broker"] = False
        return out

    # Live path (still gated by ai_phase_b_enabled; default remains off).
    decision["extended_hours"] = True
    decision["time_in_force"] = "day"
    if place_fn is None:
        try:
            import ai_positions as cp
            place_fn = cp.place_scaled_entry
        except Exception as e:
            out["error"] = f"place_unavailable:{e}"
            return out
    try:
        result = place_fn(
            sym, decision, float(equity),
            risk_pct=float(c.get("ai_risk_pct", 1.0) or 1.0),
            current_ask=float(last),
            current_bid=None,
            duel_source=str(record.get("source") or "phase_b"),
        ) or {}
    except Exception as e:
        out["error"] = str(e)[:200]
        return out
    out.update(result if isinstance(result, dict) else {"ok": False})
    out["broker"] = True
    return out


def hard_stop_hit(
    entry: float,
    last: float,
    *,
    cfg: dict | None = None,
) -> bool:
    try:
        e = float(entry)
        p = float(last)
    except (TypeError, ValueError):
        return False
    if e <= 0 or p <= 0:
        return False
    pct = hard_stop_pct(cfg) / 100.0
    return p <= e * (1.0 - pct)


def mark_unfilled(
    symbol: str,
    *,
    cfg: dict | None = None,
    now: float | None = None,
    reason: str = "phase_b_unfilled",
) -> None:
    try:
        import phase_b_ledger as pbl
        pbl.log_event(
            "unfilled",
            symbol=str(symbol or "").upper(),
            reason=reason,
            cfg=cfg,
            ts=now,
        )
    except Exception:
        pass


def cancel_entry_on_ttl(
    symbol: str,
    submitted_ts: float,
    *,
    now: float | None = None,
    cfg: dict | None = None,
    cancel_fn=None,
) -> bool:
    """Cancel resting Phase B entry after TTL; ledger phase_b_unfilled."""
    c = _cfg(cfg)
    t0 = float(now if now is not None else time.time())
    ttl = entry_limit_ttl_sec(c)
    if ttl <= 0:
        return False
    if (t0 - float(submitted_ts)) < ttl:
        return False
    if dry_run(c):
        mark_unfilled(symbol, cfg=c, now=t0, reason="phase_b_unfilled")
        return True
    if cancel_fn is not None:
        try:
            cancel_fn(symbol)
        except Exception:
            pass
    else:
        try:
            import alpaca_trader
            alpaca_trader.cancel_open_orders(str(symbol).upper())
        except Exception:
            pass
    mark_unfilled(symbol, cfg=c, now=t0, reason="phase_b_unfilled")
    return True


def should_handoff_to_rth(pos: dict, cfg: dict | None = None) -> bool:
    """False for Phase B lots when no_rth_handoff (v1 default)."""
    if not is_phase_b_position(pos):
        return True  # non-Phase-B: caller decides
    return not no_rth_handoff(cfg)


def working_sell_cfg_overlay(pos: dict, cfg: dict | None = None) -> dict:
    """Return chase/slip knobs for a Phase B position (scoped, not global)."""
    c = dict(_cfg(cfg))
    if not is_phase_b_position(pos):
        return c
    c["ai_premarket_chase_step_sec"] = chase_step_sec(c)
    c["ai_premarket_max_exit_slip_r"] = max_exit_slip_r(c)
    # During flatten, allow one-step wider slip to meet flat deadline.
    return c


def flatten_slip_r(pos: dict, cfg: dict | None = None, *, now: float | None = None) -> float:
    base = max_exit_slip_r(cfg)
    if phase_b_flatten_active(now, cfg):
        return base + 0.25  # one-step wider in flatten window
    return base


# ── Tick orchestration (dry-safe) ────────────────────────────────────────


def gather_candidates(cfg: dict | None = None) -> list[dict]:
    """Build momentum-first candidate list from soft-seed sources when available."""
    c = _cfg(cfg)
    rows: list[dict] = []
    try:
        import ai_entry_watch as ew
        if hasattr(ew, "_soft_seed_source_rows"):
            raw = ew._soft_seed_source_rows(c) or []
            for r in raw:
                if not isinstance(r, dict):
                    continue
                src = str(r.get("source") or "").strip().lower()
                # Soft-seed order is trending→movers→momentum; we re-sort.
                if src in SOURCE_OFF_V1:
                    continue
                # Mention burst may appear as criteria.
                crit = r.get("criteria") or []
                if isinstance(crit, (list, tuple)) and any(
                    "burst" in str(x).lower() or "mention" in str(x).lower()
                    for x in crit
                ):
                    if "momentum" not in src and "mover" not in src:
                        r = dict(r)
                        r["source"] = "mention_burst"
                # WP1 allow-list drops mention/trending before sort when locked.
                if not source_allowed(r.get("source"), c):
                    continue
                rows.append(r)
    except Exception:
        pass
    return sort_candidates(rows, c)


def sync_book(
    *,
    cfg: dict | None = None,
    now: float | None = None,
    open_count: int = 0,
    candidates: list[dict] | None = None,
) -> dict[str, Any]:
    """Refresh Phase B book: demote stale, admit by priority up to seats."""
    c = _cfg(cfg)
    t0 = float(now if now is not None else time.time())
    summary: dict[str, Any] = {"admitted": [], "demoted": [], "refused": []}
    if not enabled(c):
        return summary
    if not phase_b_session_active(t0, c) and not phase_b_allow_entries(t0, c):
        return summary

    demoted = demote_stale_seats(now=t0, cfg=c)
    summary["demoted"] = demoted

    if phase_b_drain_only(t0, c):
        return summary

    cands = candidates if candidates is not None else gather_candidates(c)
    book = load_book()
    for row in cands:
        if len(book) >= max_seats(c):
            # Still try higher-priority replaces via seat_symbol.
            pass
        ok, why = seat_symbol(row, cfg=c, now=t0, open_count=open_count)
        if ok:
            summary["admitted"].append(str(row.get("symbol") or "").upper())
            book = load_book()
        else:
            if why not in ("already_seated",):
                sym_u = str(row.get("symbol") or "").upper().strip()
                print_age = None
                try:
                    got = stream_last_print(sym_u, now=t0, cfg=c) if sym_u else None
                    if got:
                        print_age = got.get("age_sec")
                except Exception:
                    print_age = None
                summary["refused"].append(
                    {
                        "symbol": sym_u or row.get("symbol"),
                        "reason": why,
                        "print_age_sec": print_age,
                        "source": row.get("source"),
                    }
                )
                try:
                    import phase_b_ledger as pbl
                    pbl.log_event(
                        "admit_refuse",
                        symbol=sym_u or None,
                        reason=str(why),
                        print_age_sec=print_age,
                        source=row.get("source"),
                        cfg=c,
                        ts=t0,
                    )
                except Exception:
                    pass
    return summary


# Statuses that may attempt arm/entry this tick. dry_armed/dry_shadow included
# so flipping dry_run off (or a prior dry one-shot) does not permanently park.
_REARM_STATUSES = frozenset({
    "", "watching", "armed", "dry_armed", "dry_shadow",
})


def _config_fingerprint(cfg: dict | None = None) -> dict[str, Any]:
    c = _cfg(cfg)
    keys = (
        "ai_phase_b_enabled", "ai_phase_b_dry_run", "ai_phase_b_ledger_enabled",
        "ai_phase_b_exh_min", "ai_phase_b_exh_max", "ai_phase_b_require_exh_rising",
        "ai_phase_b_rsi_block_falling_above", "ai_phase_b_max_seats",
        "ai_phase_b_max_open", "ai_phase_b_entry_limit_ttl_sec",
        "ai_phase_b_print_max_age_sec", "ai_phase_b_hard_stop_pct",
        "ai_phase_b_no_rth_handoff", "ai_phase_b_working_sell",
        "ai_phase_b_min_prior_dollar_vol", "ai_phase_b_sources_allow",
    )
    return {k: c.get(k) for k in keys}


def _maybe_log_daily_fingerprint(
    *,
    cfg: dict | None = None,
    now: float | None = None,
) -> None:
    """Once per ET day, stamp config fingerprint into the Phase B ledger."""
    c = _cfg(cfg)
    t0 = float(now if now is not None else time.time())
    day = datetime.fromtimestamp(t0, tz=ET).strftime("%Y-%m-%d")
    try:
        import phase_b_ledger as pbl
        rows = pbl.read_day(day)
        if any(str(r.get("kind") or "") == "config_fingerprint" for r in rows):
            return
        pbl.log_event(
            "config_fingerprint",
            cfg=c,
            ts=t0,
            fingerprint=_config_fingerprint(c),
        )
    except Exception:
        pass


def tick(
    *,
    cfg: dict | None = None,
    now: float | None = None,
    equity: float = 0.0,
    open_positions: dict | None = None,
) -> list[dict]:
    """One Phase B maintenance pass. Safe when disabled (no-op).

    Does not place broker orders when dry_run. Does not touch RTH Plan A.
    """
    c = _cfg(cfg)
    events: list[dict] = []
    if not enabled(c):
        return events
    t0 = float(now if now is not None else time.time())
    _maybe_log_daily_fingerprint(cfg=c, now=t0)

    open_pos = open_positions if isinstance(open_positions, dict) else {}
    phase_b_opens = {
        k: v for k, v in open_pos.items() if is_phase_b_position(v)
    }
    open_count = len(phase_b_opens)

    # Peak concurrent opens — scoreboard bar (log only on new high).
    try:
        day = datetime.fromtimestamp(t0, tz=ET).strftime("%Y-%m-%d")
        prev = int(_PEAK_OPENS_BY_DAY.get(day) or 0)
        if open_count > prev:
            _PEAK_OPENS_BY_DAY[day] = open_count
            import phase_b_ledger as pbl
            pbl.log_event(
                "peak_opens",
                open_count=open_count,
                peak_opens=open_count,
                cfg=c,
                ts=t0,
            )
    except Exception:
        pass

    # Cancel resting entries at cutoff / TTL.
    if not phase_b_allow_entries(t0, c):
        book = load_book()
        for sym, rec in list(book.items()):
            if str(rec.get("status") or "") == "entry_pending":
                sub_ts = _f(rec.get("entry_submitted_ts")) or 0.0
                cancel_entry_on_ttl(sym, sub_ts or 0.0, now=t0, cfg=c)
                rec["status"] = "watching"
                book[sym] = rec
                events.append({"kind": "entry_cancel_cutoff", "symbol": sym})
        save_book(book)

    sync_summary = sync_book(cfg=c, now=t0, open_count=open_count)
    events.append({"kind": "book_sync", **sync_summary})

    # Per-tick live indicators for seated symbols (fixes admit-time {}).
    book = refresh_seat_indicators(cfg=c, now=t0)
    events.append({
        "kind": "indicators_refreshed",
        "n": len(book),
    })

    # Arm / entry attempts during entry window.
    if phase_b_allow_entries(t0, c):
        book = load_book()
        for sym, rec in list(book.items()):
            if str(rec.get("status") or "") not in _REARM_STATUSES:
                continue
            if open_count >= max_open(c):
                break
            result = try_phase_b_entry(
                rec, equity=equity, cfg=c, now=t0, open_count=open_count,
            )
            events.append({"kind": "entry_attempt", **result})
            if result.get("ok") and result.get("dry_run"):
                rec["status"] = "dry_armed"
                rec["last_arm_ts"] = t0
                book[sym] = rec
            elif result.get("ok") and result.get("broker"):
                rec["status"] = "entry_pending"
                rec["entry_submitted_ts"] = t0
                rec["arm_last"] = result.get("arm_last")
                rec["limit_px"] = result.get("limit_px")
                book[sym] = rec
                open_count += 1
            elif not result.get("ok") and str(rec.get("status") or "") in (
                "dry_armed", "dry_shadow",
            ):
                # Stay re-armable next tick when dry→live or refuse was transient.
                rec["status"] = "watching"
                book[sym] = rec
        save_book(book)

    # Hard stop + flatten hooks for open Phase B lots.
    for sym, pos in phase_b_opens.items():
        last, _why = fresh_stream_last(sym, now=t0, cfg=c)
        entry = _f(pos.get("entry_price"))
        if last is not None and entry is not None and hard_stop_hit(entry, last, cfg=c):
            events.append({
                "kind": "hard_stop",
                "symbol": sym,
                "last": last,
                "entry": entry,
            })
            try:
                import phase_b_ledger as pbl
                pbl.log_event(
                    "hard_stop", symbol=sym, last=last, entry=entry,
                    cfg=c, ts=t0,
                )
            except Exception:
                pass
            if not dry_run(c) and working_sell_on(c):
                try:
                    import ai_positions as cp
                    if hasattr(cp, "_rest_working_sell"):
                        # Aggressive flatten mark — use last as sell px.
                        cp._rest_working_sell(sym, pos, float(last), "flatten")
                except Exception:
                    pass

        if phase_b_flatten_active(t0, c) or phase_b_past_flat_deadline(t0, c):
            events.append({"kind": "flatten", "symbol": sym, "last": last})
            try:
                import phase_b_ledger as pbl
                pbl.log_event(
                    "flatten",
                    symbol=sym,
                    last=last,
                    past_deadline=phase_b_past_flat_deadline(t0, c),
                    cfg=c,
                    ts=t0,
                )
            except Exception:
                pass

    if phase_b_past_flat_deadline(t0, c) and phase_b_opens:
        for sym in phase_b_opens:
            try:
                import phase_b_ledger as pbl
                pbl.log_event(
                    "flat_deadline_miss",
                    symbol=sym,
                    cfg=c,
                    ts=t0,
                )
            except Exception:
                pass
            events.append({"kind": "flat_deadline_miss", "symbol": sym})

    return events


__all__ = [
    "SOURCE_PRIORITY",
    "admit_candidate",
    "build_entry_decision",
    "can_open_another",
    "cancel_entry_on_ttl",
    "chase_step_sec",
    "demote_stale_seats",
    "dry_run",
    "enabled",
    "entry_limit_price",
    "entry_limit_ttl_sec",
    "flatten_slip_r",
    "fresh_stream_last",
    "gather_candidates",
    "hard_stop_hit",
    "hard_stop_pct",
    "is_phase_b_position",
    "load_book",
    "mark_unfilled",
    "max_open",
    "max_seats",
    "min_price",
    "no_rth_handoff",
    "phase_b_allow_entries",
    "phase_b_arm_allows",
    "phase_b_drain_only",
    "phase_b_flatten_active",
    "phase_b_past_flat_deadline",
    "phase_b_session_active",
    "price_ok_for_book",
    "print_max_age_sec",
    "refresh_seat_indicators",
    "save_book",
    "seat_symbol",
    "set_book_path_for_tests",
    "should_handoff_to_rth",
    "sort_candidates",
    "source_rank",
    "stream_last_print",
    "sync_book",
    "tick",
    "try_phase_b_entry",
    "working_sell_cfg_overlay",
    "working_sell_on",
]
