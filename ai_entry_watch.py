"""Agreement-based session watch queue for AI paper entries.

Research (slow clock) upserts symbols that clear the agreement gate.
The poller arms/buys from stored structure; this module owns load/save,
upsert/invalidation, zone/spread arming, rate-limited structure refresh,
and poll_once paper entry placement.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import desk_auth  # noqa: E402
import desk_core  # noqa: E402
from ai_paths import resolve_report_dir  # noqa: E402
from ticker_filters import is_levered_etp  # noqa: E402

REPORT_DIR = resolve_report_dir()
WATCH_STATE_PATH = REPORT_DIR / "entry_watch_state.json"
# Close-edge latch for watch expiry. On disk for the same reason the EOD and
# SOD liquidate stamps are: it has to survive a restart. See
# load_watch_close_state.
WATCH_CLOSE_STATE_PATH = REPORT_DIR / "watch_close_state.json"

_EMPTY_RECORD_DEFAULTS: dict[str, Any] = {
    "structure": None,
    "structure_ts": 0.0,
    "last_poll_ts": 0.0,
    "last_ask": None,
}

_ARMABLE_STATUSES = frozenset({"watching", "armed"})
_TERMINAL_STATUSES = frozenset({
    "filled", "submitted", "invalidated", "expired",
})

# Below-zone arm window in R (zone floor − stop). 1.0 = all the way to the stop.
DEFAULT_ARM_BELOW_MAX_R = 1.0


def arm_below_max_r(cfg: dict | None = None) -> float:
    """Configured max overshoot below the zone floor, in R (default 1.0 = to stop)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(
            0.0,
            float(cfg.get("ai_watch_arm_below_zone_max_r", DEFAULT_ARM_BELOW_MAX_R)
                  or 0.0),
        )
    except (TypeError, ValueError):
        return DEFAULT_ARM_BELOW_MAX_R


def arm_at_last(cfg: dict | None = None) -> bool:
    """True when the book buys the tape instead of waiting for a pullback.

    Missing / unknown ``ai_watch_arm_mode`` stays on the zone path so unit
    tests that omit the key keep their old geometry. Live default is zone.
    """
    mode = str((cfg or {}).get("ai_watch_arm_mode") or "").lower().strip()
    return mode in ("last", "at_last", "tape", "market", "no_zone")


# Cheap names already up this far on the day are a blow-off, not a dip.
# WCT/BYSI/HCTI/BQ (2026-08-12/13) were +60–110% and -$100 each.
_CHEAP_BLOWOFF_PCT = 15.0

# Serializes every load -> mutate -> save of the watch file.
#
# Two threads in ai_trader touch it: the book thread runs
# sync_watch_from_source_panels every 2s, and a daemon thread runs poll_once
# every 20s. Both did read-modify-write on the *whole* dict, so last writer won
# the entire file. A sync that read before poll_once saved silently reverted the
# re-anchored zone, last_ask, block_code — and status="submitted" back to
# "watching", which re-armed a symbol that already had a live order. That is how
# one symbol took 13 entry_ok events in 93 minutes on 2026-08-04.
#
# Re-entrant because poll_once holds it across helpers that also load/save.
_WATCH_LOCK = threading.RLock()

# Ring of structure LLM call timestamps (module-level budget window).
_structure_call_ts: list[float] = []
_STRUCTURE_BUDGET_WINDOW_SEC = 3600.0

# Per-symbol "do not re-arm until" stamps after wash-trade rejects.
# 2026-08-11: without this, entry_fail → status=watching → re-place every poll
# produced 39 BUY_ERRORs and a close_out thrash on QMCO/AIFA.
_wash_cooldown_until: dict[str, float] = {}
_WASH_COOLDOWN_SEC = 1800.0  # 30 minutes

# After a stale_timeout drop, refuse re-seed until this clock — but only while
# the tape is still dead. A young stream print clears the cool immediately
# (see _stale_timeout_blocked). 30m cool starved selection on 2026-09-04.
_STALE_TIMEOUT_UNTIL: dict[str, float] = {}
_STALE_TIMEOUT_DEFAULT_SEC = 180.0  # mid-session 2026-09-18 (was 360)
_STALE_TIMEOUT_RESEED_DEFAULT_SEC = 300.0  # 5 min (was 30m — too hungry)
_STALE_TIMEOUT_GRACE_DEFAULT_SEC = 90.0  # don't count until on-book this long

# Same-day no_stream_trade strike demote (A2 occupancy hygiene).
# Module dict keyed by ET YYYY-MM-DD → {SYM: count}. v1 is process-local:
# dashboard restart clears strikes (a name may take 2 more drops before demote).
# Young stream does NOT clear strikes — that revolving door is the bug.
# Only a new ET calendar day resets. Persist later if restart mid-day matters.
_NO_STREAM_STRIKES: dict[str, dict[str, int]] = {}
_NO_STREAM_STRIKE_LIMIT_DEFAULT = 2
_NO_STREAM_STRIKE_REASONS_DEFAULT = ("no_stream_trade",)
# Last ensure_watch_stream wall time per symbol — strike grace window.
_STREAM_ENSURED_AT: dict[str, float] = {}
# Last no_stream_grace log ts per symbol (once per ensure window).
_NO_STREAM_GRACE_LOGGED_AT: dict[str, float] = {}
_NO_STREAM_STRIKE_GRACE_DEFAULT_SEC = 60.0
# Stale-stay restream: once per dead-tape episode, ensure_watch_stream and
# defer drop for ai_watch_stale_restream_grace_sec (dig 2026-09-15 B1).
_STALE_RESTREAM_AT: dict[str, float] = {}
_STALE_RESTREAM_LOGGED: set[str] = set()
_STALE_RESTREAM_GRACE_DEFAULT_SEC = 60.0

# symbol -> the quote's OWN unix time, from the last provable pricing.
# Deliberately module-level rather than a record field: poll_once rebuilds the
# full watch record every cycle, so a stamp written onto the record is gone
# before the next publish reads it. That is why rows kept publishing
# last_ask_age_sec None while the arm gate, running immediately after pricing,
# saw real ages — and why storing the timestamp per-record fixed nothing.
# Only ever written from the same measurement that produced the price, and
# removed when an age is unprovable: a clock that outlives its price is the
# bug this exists to prevent, not a feature.
_LAST_QUOTE_TS: dict[str, float] = {}


def _set_quote_ts(sym: str, ts: float) -> None:
    """The one writer of the price clock the poll's freshness check reads.

    Nine paths restamp it — the sync, the poll, and the book paint that runs
    beside the poll every 2-3 s (2026-09-25 audit, after the paint was found
    advancing the mid-rise latch). Jumps of 2 s or more are recorded with the
    writer's name so a session shows which path moves the clock.
    """
    old = _LAST_QUOTE_TS.get(sym)
    _LAST_QUOTE_TS[sym] = ts
    try:
        if old is None or abs(float(ts) - float(old)) >= 2.0:
            import session_recorder as _rec
            _rec.record_input(
                "clock_restamp", sym,
                None if old is None else round(float(ts) - float(old), 2),
                ts=time.time(), fn=sys._getframe(1).f_code.co_name,
                thread=threading.current_thread().name)
    except Exception:  # noqa: BLE001
        pass

# Machine code → short operator label for the AI Watch "Blocker" column.
_BLOCKER_LABELS: dict[str, str] = {
    "above_zone": "above zone",
    "below_zone": "below zone",
    "recheck_above_zone": "left zone",
    "recheck_below_zone": "left zone",
    "recheck_spread": "left zone",
    "spread": "wide spread",
    "wait_setup": "wait setup",
    "hard_no": "hard no",
    "no_structure": "no zone",
    "no_quote": "no quote",
    "reward_risk": "R:R low",
    "not_trading_hours": "hours closed",
    "above_max_price": "over max $",
    "max_positions": "max positions",
    "buy_cap": "buy cap",
    "already_held": "already held",
    "no_equity": "no equity",
    "duel_blocked": "duel only",
    "indicators_faded": "setup faded",
    "sell_signal": "sell signal",
    "no_indicators": "no signal",
    "daily_loss_limit": "day loss cap",
    "open_risk_cap": "risk cap",
    "dollar_volume": "too thin",
    "already_managed": "managed",
    "reentry_cooldown": "cooldown",
    "arm_confirming": "confirming",
    "attempt_cap": "3 strikes",
    "float_too_big": "float",
    # Two different failures wearing one label until now. "stale quote" means
    # the print is provably old; "no quote age" means it cannot be timed at
    # all, which is a plumbing fault rather than a quiet tape and has to be
    # legible as one — decision_price returns an age on demand while the
    # record carries None.
    "no_quote_age": "no quote age",
    "wash_trade": "wash trade",
    "wash_cooldown": "wash cool",
    "already_holding": "held",
    "no_buying_power": "no BP",
    "risk_gate": "risk gate",
    "trader_not_ready": "trader off",
    "not_watching": "not watching",
    "placing": "placing…",
    # Armable paint — not an open. "buy" made State look like a fill.
    "in_zone": "ready",
    "at_last": "ready",
    "last_exhaustion_off": "ready",
    "last_no_exhaustion_fallback": "ready",
    "last_overbought_hot": "ready",
    "last_overbought": "ready",
    "last_heating": "ready",
    "last_mid_rise": "ready",
    "mid_rise": "ready",
    "wait_mid_rise": "wait -50 cross",
    "mid_rise_stale": "-50 cross stale",
    "mid_rise_lost": "back under -50",
    "engine_stale": "engine stale",
    "spread_wide": "spread wide",
    "spread_unknown": "spread ?",
    "gapped_down": "gapped down",
    "near_hod": "near day high",
    "hod_unknown": "day high unknown",
    "gap_unknown": "gap ?",
    "rvol_pace_low": "volume pace low",
    "rvol_pace_unknown": "volume pace ?",
    "last_in_zone_fade_ok": "ready",
    "last_late_hold": "late hold",
    "late_hold_closed": "late hold wait",
    "late_hold_not_late_admit": "not late admit",
    "offset_zone": "no shelf",
    "stop_too_tight": "stop too tight",
    "cheap_ob_band": "cheap OB band",
    "extended_cheap": "blow-off",
    # Exhaustion gate (ai_watch_exhaustion_rules) — UI must name these or
    # in-zone names look "ready" while the poll refuses on missing %R.
    "no_exhaustion_data": "no %R",
    # %R blanked because the desk had no trade to close it on — only an ask,
    # which would have pinned the reading to EXH 100. See indicator_price.
    "pctr_not_live_no_trade_price": "no trade px",
    # Exhaustion / continuation arm refusals.
    "heating_too_low": "heat low",
    "price_falling": "price falling",
    "price_trend_unknown": "price trend ?",
    "slow_not_rising": "slow %R flat",
    "not_both_rising": "lines not rising",
    "already_extended": "extended",
    # Soft OB + elevated RSI (still <= hard RSI max). HPE-class chase.
    "late_heat": "late heat",
    "soft_overbought": "late heat",
    # Heating-band chase: RSI already mid/high while EXH is only heating
    # (GTLB 2026-09-04). Soft OB does not cover this — it needs overbought.
    "mistimed_heat": "mistimed heat",
    "heat_extended": "mistimed heat",
    "wait_exh": "wait EXH",
    "wait_rsi": "wait RSI",
    "exh_not_tight": "EXH wide",
    "stale_square": "stale ■",
    "exh_rsi": "ready",
    "last_exh_rsi": "ready",
    "in_zone_fade_ok": "in zone",
    "overbought_hot": "OB hot",
    "dead_reentry": "dead today",
    "loser_reentry": "dead today",
    "thin_rvol": "rvol low",
    # Not "too hot" — the number itself is not believable, so nothing was
    # measured. See ai_watch_arm_rvol_sane_max.
    "rvol_implausible": "rvol bad",
    "look_wash": "WASH",
    "not_heating_cooling": "cooling",
    "not_heating_flat": "flat",
    "not_heating_heating": "heating",
    "not_heating_unknown": "no %R",
    "not_overbought_cooling": "cooling",
    "not_overbought_flat": "flat",
    "not_overbought_heating": "heating",
    "not_overbought_unknown": "no %R",
    "not_rising_cooling": "cooling",
    "not_rising_flat": "flat",
    "not_rising_overbought": "OB fade",
    "not_rising_heating": "not rising",
    "not_rising_unknown": "no %R",
    # Explicit EXH direction refuses (ai_watch_require_exh_rising).
    "exh_falling": "EXH falling",
    "exh_not_rising": "EXH flat",
    "exh_rising_required": "need EXH↑",
    "not_continuation_cooling": "cooling",
    "not_continuation_flat": "flat",
    "not_continuation_unknown": "no %R",
    "overbought": "overbought",
    "heating": "heating",
    "rsi_deep_os_exh_heating": "RSI OS+EXH↑",
    "rsi_turning_up": "RSI↑",
    "rsi_not_rising": "RSI↓",
    "rsi_extended": "RSI high",
    "stale_quote": "stale quote",
    # Entry-only: ai_watch_arm_require_stream_price refused a non-stream print.
    "stream_required": "need stream",
    # Post-admit Finnhub subscribe grace — not the same as a dead tape.
    "await_stream": "await stream",
    "stale_timeout": "stale timeout",
    "stale_timeout_reseed_block": "stale reseed",
    "stale_tape_cap": "stale seat cap",
    "unarmable_steal": "unarmable steal",
    "preheat_steal": "preheat steal",
    "far_exh_steal_for_pre_square": "far→pre_square steal",
    "far_exh_steal_for_square": "far→square steal",
    "far_exh": "far exh evict",
    "never_armable": "never armable",
    "far_exh": "far EXH",
    "never_square": "far EXH",
    "scout_ttl": "scout TTL",
    "arm_ready_stale": "not arm-ready",
    "arm_ready_rsi_not_rising": "not arm-ready",
    "arm_ready_rsi_extended": "not arm-ready",
    "arm_ready_exh_falling": "not arm-ready",
    "arm_ready_exh_too_low": "not arm-ready",
    "arm_ready_above_max_price": "not arm-ready",
    "arm_ready_chg_band": "CHG% band",
    "arm_ready_no_rsi_data": "not arm-ready",
    "arm_ready_exh_rising_required": "not arm-ready",
    "stale_tape_admit": "tape too old",
    "no_tape": "no tape",
    "no_stream_trade": "no stream trade",
    "no_stream_strike_demote": "no-stream demote",
    "levered_etp": "levered ETP",
    # Atomic confirm→submit (Package B): send ask moved / tape died after pass.
    "confirm_slip": "confirm slip",
    "confirm_stale": "confirm stale",
    # MACD momentum validation refusals
    "no_macd_data": "no MACD",
    # Provenance refusals — the reading exists but was not drawn on
    # the live tape, or cannot say what it was drawn on.
    "macd_not_realtime_alpaca": "MACD not live",
    "macd_src_unknown": "MACD src?",
    "macd_stale_bars": "MACD stale",
    "macd_bearish": "MACD bear",
    "macd_gap_too_close": "MACD narrow",
    "macd_gap_insufficient": "MACD gap low",
    "macd_no_recent_cross": "wait cross",
    # Direction, not size: the lines are far enough apart but coming back
    # together, so the momentum this entry is meant to ride is already over.
    "macd_gap_narrowing": "MACD closing",
    "macd_gap_dir_unknown": "no MACD dir",
    "macd_sep_unknown": "no MACD sep",
    # Passed on confluence rather than on gap size: MACD opening while
    # EXH is at or past the threshold.
    "macd_exh_confluence": "ready (EXH)",
    "macd_bullish_gap": "ready",
    "macd_gap_arm_off": "MACD fill off",
    "macd_not_bull": "MACD not bull",
    "macd_gap_not_rising": "MACD gap flat",
    "macd_gap_too_small": "MACD gap small",
    "macd_rsi_hot": "RSI too high",
    "in_square": "in square",
    "last_macd_gap": "MACD gap",
}


def format_blocker(code: str | None, *, detail: str | None = None) -> str | None:
    """Short human label for the AI Watch blocker column."""
    if not code and not detail:
        return None
    detail_s = str(detail or "").strip()
    if str(code or "").strip().lower() == "look_wash":
        return "WASH"
    if "wash" in detail_s.lower() or "wash" in str(code or "").lower():
        return "wash trade"
    raw = str(code or "").strip()
    low = raw.lower()
    if low.startswith("recheck_"):
        base = _BLOCKER_LABELS.get(low) or _BLOCKER_LABELS.get(low[8:]) or low[8:].replace("_", " ")
    elif low.startswith("gate_error:"):
        base = "broker gate"
    elif low.startswith("tranche") or "rolled_back" in low:
        base = "order failed"
    elif low in _BLOCKER_LABELS:
        base = _BLOCKER_LABELS[low]
    elif raw:
        # Truncate long Alpaca JSON / stack crumbs.
        base = raw.replace("_", " ")
        if len(base) > 28:
            base = base[:25] + "…"
    else:
        base = None
    if base:
        return base
    if detail_s:
        return (detail_s[:25] + "…") if len(detail_s) > 28 else detail_s
    return None


def set_block_reason(
    rec: dict,
    code: str,
    *,
    now: float | None = None,
    detail: str | None = None,
) -> None:
    """Persist last skip/fail so the UI can show why we did not buy."""
    if not isinstance(rec, dict):
        return
    c = str(code or "").strip() or "blocked"
    rec["block_code"] = c
    rec["block_reason"] = format_blocker(c, detail=detail) or c
    rec["block_ts"] = float(now if now is not None else time.time())
    if detail:
        rec["block_detail"] = str(detail)[:200]
    else:
        rec.pop("block_detail", None)


def clear_block_reason(rec: dict) -> None:
    if not isinstance(rec, dict):
        return
    for k in ("block_code", "block_reason", "block_ts", "block_detail"):
        rec.pop(k, None)


# Data-condition refuses that must not survive a fresh stream print.
# Sync used to copy block_code=stale_quote from prev then overwrite
# last_ask_src=stream (BIAF/SNDG/SMCI 2026-09-04) — file and paint then
# showed stream+young beside "stale quote".
_TAPE_DATA_BLOCK_CODES = frozenset({
    "stale_quote", "no_quote_age", "no_quote",
    "stream_required", "await_stream",
})


def _stream_field_age_sec(rec: dict) -> float | None:
    """Stamped last_ask_age_sec / price_age_sec on the row, if provable."""
    if not isinstance(rec, dict):
        return None
    for k in ("last_ask_age_sec", "price_age_sec"):
        try:
            v = rec.get(k)
            if v is None:
                continue
            f = float(v)
            if f >= 0:
                return f
        except (TypeError, ValueError):
            continue
    return None


def align_stream_clock_if_field_young(
    rec: dict,
    cfg: dict | None = None,
    *,
    now: float | None = None,
) -> bool:
    """When src=stream and field age is young, fix a lagging *map* clock.

    Class C / GTLB: paint stamped ``last_ask_age_sec``=2–5s (eng) with
    src=stream but left ``_LAST_QUOTE_TS`` on an older poller clock and
    omitted ``last_ask_ts``. ``row_quote_age_sec`` preferred the map,
    ``apply_tape_blocker`` restamped stale_quote, honesty demoted stream —
    desk showed stale_quote beside eng age ≤5s.

    Only realign when the row has **no** ``last_ask_ts``. An explicit old
    ``last_ask_ts`` with a frozen young field is the honesty case (field is
    the lie; demote). Returns True when the clock was realigned.
    """
    if not isinstance(rec, dict):
        return False
    src = str(
        rec.get("last_ask_src") or rec.get("price_src") or ""
    ).strip().lower()
    if src != "stream":
        return False
    # Explicit row ts owns the clock — do not override a dated stamp with a
    # frozen field age (test_public_snapshot_never_emits_stream_with_stale_age).
    if _num_or_none(rec.get("last_ask_ts")) is not None:
        return False
    field = _stream_field_age_sec(rec)
    if field is None:
        return False
    ceiling = decision_max_age_sec(cfg)
    if field > ceiling:
        return False
    mapped = row_quote_age_sec(rec, now=now)
    # Map already agrees (or is younger) — nothing to fix.
    if mapped is not None and mapped <= field + 0.25:
        return False
    tnow = float(now if now is not None else time.time())
    ts = tnow - float(field)
    rec["last_ask_ts"] = ts
    rec["last_ask_age_sec"] = float(field)
    rec["price_age_sec"] = float(field)
    sym = str(rec.get("symbol") or "").upper().strip()
    if sym:
        _set_quote_ts(sym, ts)
    return True


def _paint_trust_young_stream_field(
    rec: dict,
    cfg: dict | None = None,
    *,
    now: float | None = None,
) -> bool:
    """Young stream field age overwrites a lagging row/map clock.

    Used by ``apply_tape_blocker`` (live overlay / book paint) and the same
    check on the Class C poller path + ``should_arm_buy`` so arm/promote
    cannot disagree with paint. When eng just wrote last_ask_age_sec≤ceiling
    with src=stream, that is the print the operator sees — do not let an
    older last_ask_ts restamp stale_quote / tape_only.
    """
    if not isinstance(rec, dict):
        return False
    src = str(
        rec.get("last_ask_src") or rec.get("price_src") or ""
    ).strip().lower()
    if src != "stream":
        return False
    field = _stream_field_age_sec(rec)
    if field is None:
        return False
    ceiling = decision_max_age_sec(cfg)
    if field > ceiling:
        return False
    tnow = float(now if now is not None else time.time())
    ts = tnow - float(field)
    rec["last_ask_ts"] = ts
    rec["last_ask_age_sec"] = float(field)
    rec["price_age_sec"] = float(field)
    sym = str(rec.get("symbol") or "").upper().strip()
    if sym:
        _set_quote_ts(sym, ts)
    return True


def promote_stream_src_if_print_fresh(
    rec: dict,
    cfg: dict | None = None,
    *,
    now: float | None = None,
) -> bool:
    """Force ``last_ask_src=stream`` when a dated print is ≤ decision ceiling.

    Closes the Class C false-stale hole where ``src`` stayed ``stale_tape`` /
    ``none`` / empty beside a young field age (CAPR Aug25: ``tape_age`` ≤15
    with ``arm_why=tape_only``) or a young ``live_print``, while
    ``clear_tape_data_block_if_stream_fresh`` required ``src=stream`` first
    and could never recover the label.

    True thin (age unknown or > ceiling, and no young live_print) is
    unchanged — including no-trade-after-subscribe names with no prints.
    """
    if not isinstance(rec, dict):
        return False
    ceiling = decision_max_age_sec(cfg)
    tnow = float(now if now is not None else time.time())
    sym = str(rec.get("symbol") or "").upper().strip()
    src = str(
        rec.get("last_ask_src") or rec.get("price_src") or ""
    ).strip().lower()

    # Always consult live_print first — including when src is already
    # "stream". A lagging row clock beside a young engine print (CDNA) used
    # to early-return on the stream branch and never refresh. Take the
    # fresher of live_print vs the row stamp as the one price/one clock.
    try:
        lp = live_print(sym) if sym else None
    except Exception:
        lp = None
    lp_px, lp_age = 0.0, None
    if lp is not None and lp[0] and lp[1] is not None:
        try:
            lp_px, lp_age = float(lp[0]), float(lp[1])
        except (TypeError, ValueError):
            lp_px, lp_age = 0.0, None

    if src == "stream":
        align_stream_clock_if_field_young(rec, cfg, now=tnow)

    age = row_quote_age_sec(rec, now=tnow)
    if age is None:
        age = _stream_field_age_sec(rec)
    try:
        row_age = float(age) if age is not None else None
    except (TypeError, ValueError):
        row_age = None

    # Prefer the younger dated print. live_print wins on ties / when row
    # age is unknown.
    use_lp = (
        lp_px > 0 and lp_age is not None and lp_age <= ceiling
        and (row_age is None or lp_age <= row_age + 1e-9)
    )
    if use_lp:
        rec["last_ask"] = lp_px
        rec["last_ask_src"] = "stream"
        rec["price_src"] = "stream"
        rec["last_ask_age_sec"] = float(lp_age)
        rec["price_age_sec"] = float(lp_age)
        rec["last_ask_ts"] = tnow - float(lp_age)
        if sym:
            _set_quote_ts(sym, tnow - float(lp_age))
        return True

    if row_age is not None and row_age <= ceiling:
        try:
            px = float(rec.get("last_ask") or rec.get("price") or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px > 0:
            rec["last_ask_src"] = "stream"
            rec["price_src"] = "stream"
            rec["last_ask_age_sec"] = float(row_age)
            rec["price_age_sec"] = float(row_age)
            rec["last_ask_ts"] = tnow - float(row_age)
            if sym:
                _set_quote_ts(sym, tnow - float(row_age))
            return True
    return False


def clear_tape_data_block_if_stream_fresh(
    rec: dict,
    cfg: dict | None = None,
) -> bool:
    """Clear sticky tape-data blocks when last_ask_src=stream and age ≤ ceiling.

    Ceiling is ``ai_watch_decision_max_age_sec`` (default 15s). Returns True
    when a block was cleared. Promotes a false ``stale_tape`` / empty label
    first when a young dated print is present; true thin (untimed / old /
    no print) still refuses.
    """
    if not isinstance(rec, dict):
        return False
    # Recover false stale labels before the src==stream gate (Class C).
    promote_stream_src_if_print_fresh(rec, cfg)
    src = str(
        rec.get("last_ask_src") or rec.get("price_src") or ""
    ).strip().lower()
    if not price_src_fresh(src):
        return False
    # Prefer field age when the map clock lags (Class C).
    align_stream_clock_if_field_young(rec, cfg)
    age = row_quote_age_sec(rec)
    if age is None:
        age = _stream_field_age_sec(rec)
    try:
        age_f = float(age) if age is not None else None
    except (TypeError, ValueError):
        age_f = None
    if age_f is None or age_f > decision_max_age_sec(cfg):
        return False
    code = str(rec.get("block_code") or "").strip().lower()
    if code not in _TAPE_DATA_BLOCK_CODES:
        return False
    clear_block_reason(rec)
    return True


def release_orphaned_submits(
    symbols: list[str] | None = None,
    *,
    force: bool = False,
) -> list[str]:
    """Return stuck ``submitted`` rows to ``watching`` so they can re-arm.

    After a fill closes (or an entry never confirms), the watch row can stay
    status=submitted forever: the 2s desk sync preserves that status, and the
    poller will not arm non-``watching`` names. UI shows blocker \"sent\".

    With *force* False (default), only symbols with no live broker position
    and no open orders are released. With *force* True, every named
    submitted/filled row is reset (operator recovery).
    """
    want: set[str] | None = None
    if symbols is not None:
        want = {
            str(s or "").upper().strip()
            for s in symbols
            if str(s or "").upper().strip()
        }
        if not want:
            return []

    held: set[str] = set()
    open_ord: set[str] = set()
    if not force:
        try:
            import alpaca_trader as at
            detail = at.get_positions_detail() or {}
            if isinstance(detail, dict):
                held = {
                    str(k).upper().strip()
                    for k, v in detail.items()
                    if v and str(k).upper().strip()
                }
            for o in (at.get_open_orders() or []):
                if not isinstance(o, dict):
                    continue
                s = str(o.get("symbol") or "").upper().strip()
                if s:
                    open_ord.add(s)
        except Exception:
            # Fail closed: do not release if we cannot see the broker book.
            if not force:
                return []

    released: list[str] = []
    with _WATCH_LOCK:
        state = load_watch()
        changed = False
        for key, rec in list(state.items()):
            if not isinstance(rec, dict):
                continue
            sym = str(rec.get("symbol") or key or "").upper().strip()
            if not sym:
                continue
            if want is not None and sym not in want:
                continue
            status = str(rec.get("status") or "").lower().strip()
            if status not in ("submitted", "filled"):
                continue
            if not force and (sym in held or sym in open_ord):
                continue
            rec = dict(rec)
            rec["symbol"] = sym
            rec["status"] = "watching"
            clear_block_reason(rec)
            state[sym] = rec
            released.append(sym)
            changed = True
        if changed:
            save_watch(state)
    return released


def _num_or_none(v):
    """float(v) or None — never raises, never invents a zero."""
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def honesty_restamp_stream_src(rec: dict, cfg: dict | None = None) -> None:
    """Never leave last_ask_src=stream beside an aged-out / untimed print.

    Live 2026-09-02 ~11:05 ET: stream+stale_quote pairs returned on ALMS /
    BIAF / GTLB / ASST after ages grew through the 15s ceiling while src
    stayed stream. public_snapshot recomputes age from _LAST_QUOTE_TS but
    was republishing the frozen stream label; apply_tape_blocker restamped
    only on the paint path. Arms stay at decision_max_age_sec (15) —
    this only aligns the label with the refuse.

    Before demoting, realign a lagging map clock when the stamped field age
    is still young (Class C / APLD-SMCI after 9eacec7).
    """
    if not isinstance(rec, dict):
        return
    src = str(
        rec.get("last_ask_src") or rec.get("price_src") or ""
    ).strip().lower()
    if src != "stream":
        return
    align_stream_clock_if_field_young(rec, cfg)
    if _row_tape_stale(rec, cfg):
        rec["last_ask_src"] = "stale_tape"
        rec["price_src"] = "stale_tape"


def _row_tape_stale(rec: dict, cfg: dict | None = None) -> bool:
    """True when this row's print is too old (or unknown) to arm.

    LIVE 2026-08-25: STATE painted buy / EXH 100% OB while last_ask_src was
    stale_tape. Realtime EXH/RSI does not make a dead last print a fill.

    Computed from src/age only — never from a leftover ``block_code``.
    Treating ``stale_quote`` as sticky locked the whole book on 2026-08-25
    after one bad overlay age: the tape recovered and State still said stale.
    """
    if not isinstance(rec, dict):
        return False
    # False stale_tape/none beside a young dated print → promote, then judge.
    promote_stream_src_if_print_fresh(rec, cfg)
    src = str(
        rec.get("last_ask_src") or rec.get("price_src") or ""
    ).strip().lower()
    if src in ("stale_tape", "none"):
        return True
    # Recomputed from the quote's own timestamp, not read off the record.
    # poll_once rebuilds the record every cycle, so the stamped age is
    # routinely gone by the time this guard runs — and because the guard fails
    # closed on None (rightly), a row was refused as untimed while a provable
    # age sat in _LAST_QUOTE_TS. 11 rows carried a real age and still read
    # "no quote age" at 12:56 ET. Falls back to the record's own fields, so a
    # record priced before the map existed still answers.
    age = row_quote_age_sec(rec)
    if age is None:
        age = rec.get("price_age_sec")
    try:
        age_f = float(age) if age is not None else None
    except (TypeError, ValueError):
        age_f = None
    if age_f is None:
        # Unprovable age is stale. This returned False — "fresh" — and since
        # last_ask_age_sec was None on every REST-priced row, the 8s
        # threshold could not fire once in 17,585 RTH rows while tape_age
        # exceeded it on 69% of them. decision_price now supplies a real age
        # on the REST path, so None here means the quote genuinely cannot be
        # timed, and "absence is not a pass" is the rule everywhere else on
        # this desk (see passes_inclusion).
        return True
    return age_f > decision_max_age_sec(cfg)


def stream_price_required_block(px_src: str | None, cfg: dict | None) -> str | None:
    """Entry-only: when ``ai_watch_arm_require_stream_price``, require stream.

    Returns ``"stream_required"`` for rest / stale_tape / none / anything that
    is not a fresh ``stream`` print; ``None`` when the flag is off or src is
    ``stream``. Staleness of a stream print is still the caller's
    ``stale_tape`` / ``_row_tape_stale`` path — ``decision_price`` only emits
    ``stream`` when the tape is within the decision max age.
    """
    if not bool((cfg or {}).get("ai_watch_arm_require_stream_price", False)):
        return None
    if price_src_fresh(px_src):
        return None
    return "stream_required"


def stale_timeout_sec(cfg: dict | None = None) -> float:
    """Seconds a watch may sit on dead stale_tape before drop.

    0 disables. Default 180s RTH (mid-session 2026-09-18; was 360) — long
    enough for a thin name to print, short enough that a Finnhub-dead symbol
    does not own a book slot all day.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(
            cfg.get("ai_watch_stale_timeout_sec", _STALE_TIMEOUT_DEFAULT_SEC)
            or 0.0))
    except (TypeError, ValueError):
        return _STALE_TIMEOUT_DEFAULT_SEC


def stale_timeout_reseed_sec(cfg: dict | None = None) -> float:
    """How long a stale_timeout drop refuses re-seed while tape is still dead.

    0 = no reseed block. Default 300s (was 1800 — starved selection when
    Finnhub was merely slow). A young stream print clears the cool even
    inside the window — see ``_stale_timeout_blocked``.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(
            cfg.get("ai_watch_stale_timeout_reseed_sec",
                    _STALE_TIMEOUT_RESEED_DEFAULT_SEC)
            or 0.0))
    except (TypeError, ValueError):
        return _STALE_TIMEOUT_RESEED_DEFAULT_SEC


def stale_timeout_grace_sec(cfg: dict | None = None) -> float:
    """Seconds on-book before the stale_timeout clock may start.

    Early RTH Finnhub subscribe lag used to burn the only admit window when
    need-stream counted from t=0. 0 disables the grace.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(
            cfg.get("ai_watch_stale_timeout_grace_sec",
                    _STALE_TIMEOUT_GRACE_DEFAULT_SEC)
            or 0.0))
    except (TypeError, ValueError):
        return _STALE_TIMEOUT_GRACE_DEFAULT_SEC


def _young_trade_ts(rec: dict, now: float, cfg: dict | None = None) -> bool:
    """True when the row has a provably young trade/quote timestamp."""
    age = row_quote_age_sec(rec, now=now)
    if age is None:
        return False
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        max_age = float(cfg.get("ai_watch_stream_max_age_sec", 10.0) or 10.0)
    except (TypeError, ValueError):
        max_age = 10.0
    if max_age <= 0:
        max_age = 10.0
    return age <= max_age


def _young_stream_alive(
    symbol: str,
    cfg: dict | None = None,
    *,
    now: float | None = None,
    row: dict | None = None,
) -> bool:
    """True when Finnhub (or the desk tape) has a young dated print right now.

    Used to clear a reseed cool: a name that was dropped for dead tape but
    now has a live stream must be allowed back onto the book immediately.
    """
    t = float(now if now is not None else time.time())
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        max_age = float(cfg.get("ai_watch_stream_max_age_sec", 10.0) or 10.0)
    except (TypeError, ValueError):
        max_age = 10.0
    if max_age <= 0:
        max_age = 10.0
    # Candidate / watch row first — admit path often already has last_ask_ts.
    if isinstance(row, dict) and _young_trade_ts(row, t, cfg):
        return True
    try:
        got = live_print(symbol)
    except Exception:
        got = None
    if got is None or got[1] is None:
        return False
    try:
        return float(got[1]) <= max_age
    except (TypeError, ValueError):
        return False


def stale_timeout_quiet_max_sec(cfg: dict | None = None) -> float:
    """Max age of a known WS/engine print that still counts as 'quiet, not dead'.

    Thin names print every few minutes on Finnhub. 180s default (was 900):
    AEHG/AOUT/LABX parked 10–14 min of dated-but-dead book prints as false
    opportunity while quiet_max blocked the drop clock. 0 = any dated tape
    blocks the dead-tape clock.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(
            cfg.get("ai_watch_stale_timeout_quiet_max_sec", 180.0) or 0.0))
    except (TypeError, ValueError):
        return 180.0


def no_trade_after_subscribe_sec(cfg: dict | None = None) -> float:
    """Seconds after admit+grace with no young stream before drop. 0 = off."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(
            cfg.get("ai_watch_no_trade_after_subscribe_sec", 300.0) or 0.0))
    except (TypeError, ValueError):
        return 300.0


def no_trade_reseed_sec(cfg: dict | None = None) -> float:
    """Reseed cool after a no_stream_trade drop. 0 → stale_timeout_reseed_sec.

    Default 120s (mid-session 2026-09-18). Kept as a separate knob;
    no_stream_trade itself now uses stale_timeout_reseed_sec.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        v = float(cfg.get("ai_watch_no_trade_reseed_sec", 120.0) or 0.0)
    except (TypeError, ValueError):
        v = 120.0
    if v > 0:
        return v
    return stale_timeout_reseed_sec(cfg)


def no_stream_strike_limit(cfg: dict | None = None) -> int:
    """Same-day no_stream_trade drops before refuse re-admit. ≤0 disables."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return int(cfg.get(
            "ai_watch_no_stream_strike_limit",
            _NO_STREAM_STRIKE_LIMIT_DEFAULT,
        ))
    except (TypeError, ValueError):
        return _NO_STREAM_STRIKE_LIMIT_DEFAULT


def no_stream_strike_reasons(cfg: dict | None = None) -> frozenset[str]:
    """Drop reasons that increment the same-day strike counter. v1: no_stream_trade only."""
    cfg = cfg if isinstance(cfg, dict) else {}
    raw = cfg.get("ai_watch_no_stream_strike_reasons")
    if raw is None:
        return frozenset(_NO_STREAM_STRIKE_REASONS_DEFAULT)
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return frozenset(parts) if parts else frozenset(_NO_STREAM_STRIKE_REASONS_DEFAULT)
    if isinstance(raw, (list, tuple, set, frozenset)):
        parts = [str(p).strip() for p in raw if str(p).strip()]
        return frozenset(parts) if parts else frozenset(_NO_STREAM_STRIKE_REASONS_DEFAULT)
    return frozenset(_NO_STREAM_STRIKE_REASONS_DEFAULT)


def no_stream_strike_grace_sec(cfg: dict | None = None) -> float:
    """Seconds after ensure_watch_stream where no_stream_trade does not strike.

    Drop / arm refusal still apply — this only delays A2 strike accrual while
    Finnhub subscribe is catching up. 0 disables.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(
            cfg.get(
                "ai_watch_no_stream_strike_grace_sec",
                _NO_STREAM_STRIKE_GRACE_DEFAULT_SEC,
            ) or 0.0))
    except (TypeError, ValueError):
        return float(_NO_STREAM_STRIKE_GRACE_DEFAULT_SEC)


def stale_restream_grace_sec(cfg: dict | None = None) -> float:
    """Hold a stale/no_stream drop this long after one ensure_watch_stream.

    Dig 2026-09-15: majority ``stream_then_silent`` — seats wait the full
    no_trade window then drop; soft-seed cannot re-keep (stale_tape_admit).
    0 disables. Arm/fill still require honest live tape.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(
            cfg.get(
                "ai_watch_stale_restream_grace_sec",
                _STALE_RESTREAM_GRACE_DEFAULT_SEC,
            ) or 0.0))
    except (TypeError, ValueError):
        return float(_STALE_RESTREAM_GRACE_DEFAULT_SEC)


def stale_restream_pins_only(cfg: dict | None = None) -> bool:
    """When true, restream-hold applies only to pin/warming seats."""
    cfg = cfg if isinstance(cfg, dict) else {}
    return bool(cfg.get("ai_watch_stale_restream_pins_only", False))


def _clear_stale_restream(symbol: str) -> None:
    sym = str(symbol or "").upper().strip()
    if not sym:
        return
    _STALE_RESTREAM_AT.pop(sym, None)
    _STALE_RESTREAM_LOGGED.discard(sym)


def _within_stale_restream_grace(
    symbol: str,
    now: float,
    cfg: dict | None = None,
) -> bool:
    grace = stale_restream_grace_sec(cfg)
    if grace <= 0:
        return False
    sym = str(symbol or "").upper().strip()
    started = _STALE_RESTREAM_AT.get(sym)
    if started is None:
        return False
    try:
        return (float(now) - float(started)) < grace
    except (TypeError, ValueError):
        return False


def _eligible_stale_restream(rec: dict | None, cfg: dict | None) -> bool:
    if not stale_restream_pins_only(cfg):
        return True
    role = str((rec or {}).get("seat_role") or "").strip().lower()
    return role in ("pin", "warming")


def _maybe_stale_restream_hold(
    rec: dict,
    *,
    sym: str,
    cfg: dict,
    now: float,
    events: list,
    cp,
    age_sec: float | None = None,
    src: str | None = None,
    reason: str = "no_stream_trade",
) -> bool:
    """Ensure stream once and defer drop during restream grace.

    Returns True when the caller must **hold** (not drop) this poll.
    After grace expires still-stale, returns False and logs
    ``stale_restream_fail`` so the existing drop path proceeds.
    """
    grace = stale_restream_grace_sec(cfg)
    if grace <= 0:
        return False
    if not _eligible_stale_restream(rec, cfg):
        return False
    sym_u = str(sym or "").upper().strip()
    if not sym_u:
        return False

    started = _STALE_RESTREAM_AT.get(sym_u)
    if started is None:
        _STALE_RESTREAM_AT[sym_u] = float(now)
        try:
            ensure_watch_stream([sym_u], cfg=cfg)
        except Exception:
            pass
        if sym_u not in _STALE_RESTREAM_LOGGED:
            _STALE_RESTREAM_LOGGED.add(sym_u)
            role = str(rec.get("seat_role") or "") or None
            try:
                events.append(cp.log_event(
                    "stale_restream_grace", symbol=sym_u,
                    grace_sec=grace, reason=reason,
                    age_sec=round(age_sec, 1) if age_sec is not None else None,
                    src=src or None, seat_role=role))
            except Exception:  # noqa: BLE001
                events.append({
                    "kind": "stale_restream_grace",
                    "symbol": sym_u,
                    "grace_sec": grace,
                    "reason": reason,
                    "seat_role": role,
                })
        return True

    try:
        elapsed = float(now) - float(started)
    except (TypeError, ValueError):
        elapsed = grace
    if elapsed < grace:
        return True

    # Grace exhausted — allow drop; clear episode for a future re-admit.
    role = str(rec.get("seat_role") or "") or None
    try:
        events.append(cp.log_event(
            "stale_restream_fail", symbol=sym_u,
            grace_sec=grace, held_sec=round(elapsed, 1),
            reason=reason,
            age_sec=round(age_sec, 1) if age_sec is not None else None,
            src=src or None, seat_role=role))
    except Exception:  # noqa: BLE001
        events.append({
            "kind": "stale_restream_fail",
            "symbol": sym_u,
            "grace_sec": grace,
            "held_sec": round(elapsed, 1),
            "reason": reason,
        })
    _clear_stale_restream(sym_u)
    return False


def _mark_stream_ensured(
    symbols,
    *,
    now: float | None = None,
) -> None:
    """Stamp ensure_watch_stream wall time for strike-grace checks."""
    t0 = float(now if now is not None else time.time())
    for raw in symbols or []:
        sym = str(raw or "").upper().strip()
        if not sym:
            continue
        _STREAM_ENSURED_AT[sym] = t0


def _within_no_stream_strike_grace(
    symbol: str,
    now: float,
    cfg: dict | None = None,
) -> bool:
    """True when *symbol* was ensure_watch_stream'd within the grace window."""
    grace = no_stream_strike_grace_sec(cfg)
    if grace <= 0:
        return False
    sym = str(symbol or "").upper().strip()
    if not sym:
        return False
    ensured = _STREAM_ENSURED_AT.get(sym)
    if ensured is None:
        return False
    try:
        return (float(now) - float(ensured)) < grace
    except (TypeError, ValueError):
        return False


def _et_day_key(now: float | None = None) -> str:
    """America/New_York calendar day YYYY-MM-DD for strike day-roll."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    t0 = float(now if now is not None else time.time())
    return datetime.fromtimestamp(
        t0, tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def _prune_no_stream_strikes(et_day: str) -> None:
    """Drop strike buckets from prior ET days (no multi-day blacklist)."""
    stale = [d for d in _NO_STREAM_STRIKES if d != et_day]
    for d in stale:
        _NO_STREAM_STRIKES.pop(d, None)


def _no_stream_strike_count(
    symbol: str,
    now: float | None = None,
) -> int:
    sym = str(symbol or "").upper().strip()
    if not sym:
        return 0
    day = _et_day_key(now)
    _prune_no_stream_strikes(day)
    return int((_NO_STREAM_STRIKES.get(day) or {}).get(sym) or 0)


def _record_no_stream_strike(
    symbol: str,
    now: float,
    reason: str,
    cfg: dict | None = None,
) -> int:
    """Increment same-day strike for *reason* if configured. Returns new count.

    Does not demote open positions — caller only invokes this on a watch_drop
    after held/submitted checks. Young stream never clears these strikes.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return 0
    reasons = no_stream_strike_reasons(cfg)
    if str(reason or "").strip() not in reasons:
        return _no_stream_strike_count(sym, now)
    day = _et_day_key(now)
    _prune_no_stream_strikes(day)
    bucket = _NO_STREAM_STRIKES.setdefault(day, {})
    n = int(bucket.get(sym) or 0) + 1
    bucket[sym] = n
    return n


def _no_stream_strike_demoted(
    symbol: str,
    now: float,
    cfg: dict | None = None,
) -> bool:
    """True when same-day no_stream strikes ≥ limit — refuse re-admit only."""
    limit = no_stream_strike_limit(cfg)
    if limit <= 0:
        return False
    return _no_stream_strike_count(symbol, now) >= limit


def admit_max_tape_age_sec(cfg: dict | None = None) -> float:
    """Max live_print age to admit a name. 0 disables the admit tape gate."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(
            cfg.get("ai_watch_admit_max_tape_age_sec", 120.0) or 0.0))
    except (TypeError, ValueError):
        return 120.0


def movers_admit_max_tape_age_sec(cfg: dict | None = None) -> float:
    """Movers-only tape age ceiling. 0 → use admit_max_tape_age_sec."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        v = float(cfg.get("ai_watch_movers_admit_max_tape_age_sec", 60.0) or 0.0)
    except (TypeError, ValueError):
        v = 60.0
    if v > 0:
        return max(0.0, v)
    return admit_max_tape_age_sec(cfg)


def max_stale_tape_seats(cfg: dict | None = None) -> int:
    """Max watching stale_tape rows. <0 = unlimited."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return int(cfg.get("ai_watch_max_stale_tape_seats", 2))
    except (TypeError, ValueError):
        return 2


def _stale_feed_condition(
    rec: dict,
    px_src: str | None,
    cfg: dict | None,
    *,
    now: float,
) -> bool:
    """True when the row is on *dead* tape with no usable trade_ts.

    Default counts only ``stale_tape`` / ``none`` / empty — not brief
    need-stream/rest right after admit (early RTH subscribe lag). A known
    dated print younger than ``ai_watch_stale_timeout_quiet_max_sec`` is
    quiet tape, not dead — do not start the drop clock. Set
    ``ai_watch_stale_timeout_include_need_stream`` to also count
    stream_required. A young trade_ts always clears the condition.
    """
    if _young_trade_ts(rec, now, cfg):
        return False
    # Quiet-but-subscribed: engine/dash still has a dated print within the
    # quiet window — Finnhub delivered something; waiting for the next trade.
    # quiet_max <= 0 disables this protection (any stale_tape can be dead).
    age = row_quote_age_sec(rec, now=now)
    quiet_max = stale_timeout_quiet_max_sec(cfg)
    if quiet_max > 0 and age is not None and age <= quiet_max:
        src_q = str(px_src or rec.get("last_ask_src") or rec.get("price_src")
                    or "").strip().lower()
        if src_q in ("stale_tape", "stream"):
            return False
    src = str(px_src or rec.get("last_ask_src") or rec.get("price_src")
              or "").strip().lower()
    if src in ("stale_tape", "none", ""):
        return True
    if not bool((cfg or {}).get("ai_watch_stale_timeout_include_need_stream", False)):
        return False
    if stream_price_required_block(src, cfg) == "stream_required":
        return True
    code = str(rec.get("block_code") or "").strip().lower()
    return code in ("stale_quote", "stream_required", "no_quote_age", "no_quote",
                    "await_stream")


def _on_book_grace_ok(rec: dict, cfg: dict | None, now: float) -> bool:
    """False while the name is still inside the post-admit subscribe grace."""
    grace = stale_timeout_grace_sec(cfg)
    if grace <= 0:
        return True
    admitted = _f_or_none(rec.get("admit_ts"))
    if admitted is None or admitted <= 0:
        # No admit stamp — do not start the dead-tape clock yet.
        return False
    return (float(now) - float(admitted)) >= grace


def _within_subscribe_grace(rec: dict, cfg: dict | None, now: float) -> bool:
    """True during post-admit Finnhub subscribe grace (await_stream window)."""
    try:
        grace = float(
            (cfg or {}).get("ai_watch_stream_subscribe_grace_sec",
                            stale_timeout_grace_sec(cfg))
            or 0.0)
    except (TypeError, ValueError):
        grace = stale_timeout_grace_sec(cfg)
    if grace <= 0:
        return False
    admitted = _f_or_none(rec.get("admit_ts"))
    if admitted is None or admitted <= 0:
        return True
    return (float(now) - float(admitted)) < grace


# Early Finnhub join for raw panel symbols (before seed filters / inclusion).
# Dig 2026-09-14 + A1: candidates already ensure_watch_stream before
# passes_inclusion; this warms names still sitting on trending/movers/research
# boards so a flip to green does not start the subscribe clock from zero.
_PANEL_PREWARM_LAST_TS: float = 0.0
_PANEL_PREWARM_INTERVAL_DEFAULT_SEC = 30.0
_PANEL_PREWARM_MAX_DEFAULT = 48


def _symbols_from_panel_json(path: Path, *, limit: int = 40) -> list[str]:
    """Cheap symbol list from a panel file — no seed filters (includes red)."""
    out: list[str] = []
    seen: set[str] = set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return out
    if not isinstance(raw, dict):
        return out
    rows = raw.get("rows") or raw.get("stocks") or raw.get("items") or []
    if not isinstance(rows, list):
        return out
    for r in rows:
        if len(out) >= limit:
            break
        if not isinstance(r, dict) or r.get("is_crypto") is True:
            continue
        s = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
        if not s or not s.isalpha() or not (2 <= len(s) <= 5) or s in seen:
            continue
        if is_levered_etp(s):
            continue
        seen.add(s)
        out.append(s)
    return out


def panel_stream_prewarm_symbols(
    cfg: dict | None = None,
    *,
    max_n: int | None = None,
) -> list[str]:
    """Union of trending / movers / research board symbols for early stream.

    Intentionally broader than ``desk_candidate_rows``: seed drops (trending
    red, movers below min price) still get a Finnhub seat so tape can warm
    before the next sync promotes them.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        cap = int(
            max_n
            if max_n is not None
            else (cfg.get("ai_watch_panel_prewarm_max") or _PANEL_PREWARM_MAX_DEFAULT)
        )
    except (TypeError, ValueError):
        cap = _PANEL_PREWARM_MAX_DEFAULT
    cap = max(0, min(cap, 80))
    if cap <= 0:
        return []
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(syms) -> None:
        for s in syms or []:
            if len(ordered) >= cap:
                return
            t = str(s or "").upper().strip()
            if not t or t in seen:
                continue
            if not t.isalpha() or not (2 <= len(t) <= 5):
                continue
            if is_levered_etp(t):
                continue
            seen.add(t)
            ordered.append(t)

    _add(_symbols_from_panel_json(ROOT / "trending_stocks.json", limit=40))
    _add(_symbols_from_panel_json(ROOT / "movers_stocks.json", limit=40))
    try:
        _add(research_universe_symbols())
    except Exception:
        pass
    return ordered[:cap]


def maybe_prewarm_panel_streams(
    cfg: dict | None = None,
    now: float | None = None,
) -> dict:
    """Throttle + ``ensure_watch_stream`` for the raw panel universe."""
    global _PANEL_PREWARM_LAST_TS
    cfg = cfg if isinstance(cfg, dict) else {}
    if not bool(cfg.get("ai_watch_panel_prewarm_enabled", True)):
        return {"skipped": "disabled", "requested": 0}
    try:
        interval = float(
            cfg.get("ai_watch_panel_prewarm_interval_sec")
            or _PANEL_PREWARM_INTERVAL_DEFAULT_SEC
        )
    except (TypeError, ValueError):
        interval = _PANEL_PREWARM_INTERVAL_DEFAULT_SEC
    t0 = float(now if now is not None else time.time())
    if interval > 0 and _PANEL_PREWARM_LAST_TS > 0 and (
            t0 - _PANEL_PREWARM_LAST_TS) < interval:
        return {"skipped": "throttle", "requested": 0}
    syms = panel_stream_prewarm_symbols(cfg)
    _PANEL_PREWARM_LAST_TS = t0
    if not syms:
        return {"skipped": "empty", "requested": 0}
    out = ensure_watch_stream(syms, cfg=cfg)
    out["panel_prewarm"] = True
    return out


def ensure_watch_stream(symbols, *, cfg: dict | None = None) -> dict:
    """Force Finnhub priority + subscribe and engine book push for watch names.

    Called on admit/re-admit so AEHG/AOUT-class are not left on REST while
    SCAN already prints. Merges into the existing priority set (does not
    wipe desk/engine priorities). Safe no-op when Finnhub is unavailable.

    Stamps ``_STREAM_ENSURED_AT`` for A2 strike grace
    (``ai_watch_no_stream_strike_grace_sec``).
    """
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in symbols or []:
        t = str(raw or "").upper().strip()
        if not t or not t.isalpha() or not (2 <= len(t) <= 5) or t in seen:
            continue
        seen.add(t)
        wanted.append(t)
    out = {"requested": len(wanted), "subscribed": 0, "pushed": 0}
    if not wanted:
        return out
    _mark_stream_ensured(wanted)
    try:
        from finnhub_stream import (
            set_subscribe_priority, get_subscribe_priority, request_subscribe,
        )
        pri = get_subscribe_priority() | set(wanted)
        set_subscribe_priority(sorted(pri))
        request_subscribe(wanted)
        out["subscribed"] = len(wanted)
    except Exception:
        pass
    try:
        pushed = push_candidates_to_engine(wanted)
        out["pushed"] = int((pushed or {}).get("pushed") or 0)
    except Exception:
        pass
    return out


# Symbols whose reseed cool was cleared by a live stream this process —
# consumed once by passes_inclusion / sync for a clear log line.
_RESEED_STREAM_CLEARED: set[str] = set()

# Continuous soft seed (trending/movers/momentum/research scout refresh).
# Process-local clock.
_SOFT_SEED_LAST_TS: float = 0.0


def soft_seed_interval_sec(cfg: dict | None = None) -> float:
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(cfg.get("ai_watch_soft_seed_interval_sec", 300.0) or 0.0))
    except (TypeError, ValueError):
        return 300.0


def warming_seat_quota(cfg: dict | None = None) -> int:
    """Canonical scout / preheat quota (``ai_watch_warming_seats``)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0, int(cfg.get("ai_watch_warming_seats", 3)))
    except (TypeError, ValueError):
        return 3


def scout_seat_quota(cfg: dict | None = None) -> int:
    """Scout seats for bench/docs. Canonical knob: ``ai_watch_warming_seats``.

    ``ai_watch_scout_seats`` overrides when present so one desk can rename the
    quota without breaking preheat_steal (which still reads warming_seats).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if "ai_watch_scout_seats" in cfg:
        try:
            return max(0, int(cfg.get("ai_watch_scout_seats") or 0))
        except (TypeError, ValueError):
            pass
    return warming_seat_quota(cfg)


def pin_slot_quota(cfg: dict | None = None) -> int:
    """Protected Elite-6 pin seats. 0 disables the pin class."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0, int(cfg.get("ai_watch_pin_slots", 6) or 0))
    except (TypeError, ValueError):
        return 6


def warming_exh_band(cfg: dict | None = None) -> tuple[float, float]:
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        lo = float(cfg.get("ai_watch_warming_exh_min", 15.0) or 15.0)
    except (TypeError, ValueError):
        lo = 15.0
    try:
        hi = float(cfg.get("ai_watch_warming_exh_max", 45.0) or 45.0)
    except (TypeError, ValueError):
        hi = 45.0
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def _exh_from_row_or_ind(row: dict | None, ind: dict | None = None) -> float | None:
    """0–100 heat from candidate row or engine indicator; None if unknown."""
    for src in (ind, row):
        if not isinstance(src, dict):
            continue
        for key in ("pctr", "exh", "exhaustion_pct", "heat_pct"):
            v = _f_or_none(src.get(key))
            if v is None:
                continue
            # Williams %R style (-100..0) → heat
            if -100.5 <= v <= 0.5:
                return max(0.0, min(100.0, 100.0 + v))
            if 0.0 <= v <= 100.0:
                return float(v)
        nested = src.get("indicator") if isinstance(src.get("indicator"), dict) else None
        if nested and src is row:
            got = _exh_from_row_or_ind(None, nested)
            if got is not None:
                return got
    return None


def _exh_rising_hint(row: dict | None, ind: dict | None = None) -> bool | None:
    for src in (ind, row):
        if not isinstance(src, dict):
            continue
        if "pctr_rising" in src:
            return bool(src.get("pctr_rising")) if src.get("pctr_rising") is not None else None
        if "exh_rising" in src:
            return bool(src.get("exh_rising")) if src.get("exh_rising") is not None else None
        nested = src.get("indicator") if isinstance(src.get("indicator"), dict) else None
        if nested and src is row:
            return _exh_rising_hint(None, nested)
    return None


# ── Arm-ready admit pre-qualify (soft-seed / keep) ─────────────────────────
# Admit-time filter only — does not loosen RSI/EXH *arm* gates. Soft-seed
# failures must not consume keep seats; warming/scout-only short TTL is OK.

_NEVER_ARMABLE_BLOCK_CODES = frozenset({
    "rsi_not_rising",
    "rsi_extended",
    "exh_falling",
    "exh_not_rising",
    "exh_rising_required",
    "exh_not_tight",
    "wait_exh",
    "stale_quote",
    "stale_tape",
    "no_quote",
    "no_quote_age",
    "stream_required",
    "above_max_price",
})

_ARM_READY_DESK_SOURCES = frozenset({
    "momentum", "trending", "mom", "st", "stocktwits", "movers",
})


def admit_require_arm_ready(cfg: dict | None, now: float | None = None) -> bool:
    """True when arm-ready pre-qualify is active (default ON in RTH)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    if not bool(cfg.get("ai_watch_admit_require_arm_ready", True)):
        return False
    if not bool(cfg.get("ai_watch_admit_arm_ready_rth_only", True)):
        return True
    t0 = float(now if now is not None else time.time())
    try:
        return bool(trading_hours_active(cfg, t0, market_open=True))
    except Exception:
        return True


def unarmable_evict_sec(cfg: dict | None = None) -> float:
    """Seconds a never-armable block may stick before eviction. 0 disables."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(
            cfg.get("ai_watch_unarmable_evict_sec", 90.0) or 0.0))
    except (TypeError, ValueError):
        return 90.0


def scout_ttl_sec(cfg: dict | None = None) -> float:
    """Short TTL for scout-only (non-arm-ready warming) soft-seed seats."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(cfg.get("ai_watch_scout_ttl_sec", 120.0) or 0.0))
    except (TypeError, ValueError):
        return 120.0


def admit_chg_band_bounds(cfg: dict | None = None) -> tuple[float, float, float]:
    """(prefer_min, prefer_max, soft_max) for day CHG% admit/rank band."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        lo = float(cfg.get("ai_watch_admit_chg_prefer_min", 8.0) or 8.0)
    except (TypeError, ValueError):
        lo = 8.0
    try:
        hi = float(cfg.get("ai_watch_admit_chg_prefer_max", 40.0) or 40.0)
    except (TypeError, ValueError):
        hi = 40.0
    try:
        soft = float(cfg.get("ai_watch_admit_chg_soft_max", 50.0) or 50.0)
    except (TypeError, ValueError):
        soft = 50.0
    if hi < lo:
        lo, hi = hi, lo
    if soft < hi:
        soft = hi
    return lo, hi, soft


def classify_admit_chg_band(
    pct: float | None,
    cfg: dict | None = None,
) -> str:
    """``prefer`` | ``below_prefer`` | ``over_soft`` | ``mid`` | ``unknown``."""
    if pct is None:
        return "unknown"
    lo, hi, soft = admit_chg_band_bounds(cfg)
    p = float(pct)
    if lo <= p <= hi:
        return "prefer"
    if p > soft:
        return "over_soft"
    if p < lo:
        return "below_prefer"
    return "mid"


def admit_pullback_ok(row: dict | None, cfg: dict | None = None) -> bool:
    """True when an extended CHG% name still looks like a pullback admit."""
    if not isinstance(row, dict):
        return False
    # Explicit flag from seed / prior stamp.
    if row.get("pullback") is True or row.get("admit_pullback") is True:
        return True
    rp = _f_or_none(row.get("admit_range_pos"))
    if rp is None:
        rp = _f_or_none(row.get("range_pos"))
    try:
        cap = float((cfg or {}).get("ai_watch_admit_max_range_pos", 90.0) or 90.0)
    except (TypeError, ValueError):
        cap = 90.0
    if cap <= 0:
        cap = 90.0
    if rp is not None and float(rp) + 1e-9 < cap:
        return True
    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    kind = str(structure.get("zone_kind") or row.get("zone_kind") or "").lower()
    if kind in ("pullback_band", "double_bottom", "offset"):
        # In/below zone is the pullback geometry the desk arms on.
        try:
            ask = float(row.get("price") or row.get("last_ask") or 0)
            lo = float(structure.get("entry_low") or 0)
            hi = float(structure.get("entry_high") or 0)
            if ask > 0 and lo > 0 and hi >= lo and ask <= hi * 1.01:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _arm_ready_ind(row: dict, indicators: dict | None = None) -> dict:
    ind = row.get("indicator") if isinstance(row.get("indicator"), dict) else None
    if isinstance(ind, dict) and ind:
        return ind
    sym = str(row.get("symbol") or "").upper().strip()
    if sym and isinstance(indicators, dict):
        got = indicators.get(sym)
        if isinstance(got, dict):
            return got
    return {}


def _arm_ready_young_tape(
    row: dict,
    cfg: dict,
    *,
    now: float,
) -> tuple[bool, str]:
    """Young stream tape for admit; fail closed on stale / need-stream."""
    sym = str(row.get("symbol") or "").upper().strip()
    code = str(row.get("block_code") or "").strip().lower()
    if code in ("stale_quote", "stale_tape", "stream_required", "no_quote",
                "no_quote_age"):
        return False, "stale"
    if code == "await_stream":
        return False, "stale"
    ceiling = decision_max_age_sec(cfg)
    try:
        admit_ceil = admit_max_tape_age_sec(cfg)
        if admit_ceil > 0:
            ceiling = min(ceiling, admit_ceil) if ceiling > 0 else admit_ceil
    except Exception:
        pass
    if ceiling <= 0:
        ceiling = 15.0

    src = str(
        row.get("last_ask_src") or row.get("price_src") or ""
    ).strip().lower()
    age = _f_or_none(row.get("last_ask_age_sec"))
    if age is None:
        age = _f_or_none(row.get("tape_age_sec"))
    if age is None:
        age = _f_or_none(row.get("price_age_sec"))
    if price_src_fresh(src) and age is not None and float(age) <= ceiling:
        return True, "ok"
    if sym:
        try:
            tape = live_print(sym)
        except Exception:
            tape = None
        if tape is not None and tape[1] is not None and float(tape[1]) <= ceiling:
            return True, "ok"
    if src in ("stale_tape", "none", "") or (
        age is not None and float(age) > ceiling
    ):
        return False, "stale"
    if age is None and src != "stream":
        return False, "stale"
    return False, "stale"


def evaluate_arm_ready(
    row: dict,
    cfg: dict | None = None,
    *,
    indicators: dict[str, dict] | None = None,
    now: float | None = None,
    max_price: float | None = None,
) -> tuple[bool, str]:
    """Admit-time arm-ready check. Returns ``(ok, reason)``.

    Requires young tape, RSI rising under arm max, EXH rising at/above heat
    min, and price under effective max. Reason codes match arm/block labels
    where possible (``stale``, ``rsi_not_rising``, ``rsi_extended``,
    ``exh_falling``, ``above_max_price``, …).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not isinstance(row, dict):
        return False, "bad_row"
    t0 = float(now if now is not None else time.time())

    tape_ok, tape_why = _arm_ready_young_tape(row, cfg, now=t0)
    if not tape_ok:
        return False, tape_why

    # Price under effective max (no above_max_price seeds).
    px = _f_or_none(row.get("price"))
    if px is None:
        px = _f_or_none(row.get("last_ask"))
    cap = max_price
    if cap is None:
        try:
            cap = float(cfg.get("ai_max_price")) if cfg.get("ai_max_price") is not None else None
        except (TypeError, ValueError):
            cap = None
    if cap is not None and px is not None and float(px) + 1e-12 >= float(cap):
        return False, "above_max_price"

    ind = _arm_ready_ind(row, indicators)
    # RSI: rising and under arm max (same locked arm knobs — not loosened).
    rsi = _f_or_none(ind.get("cm_rsi"))
    if rsi is None:
        rsi = _f_or_none(row.get("cm_rsi"))
    if rsi is None:
        return False, "no_rsi_data"
    try:
        rsi_max = float(cfg.get("ai_watch_arm_cm_rsi_max", 75.0) or 75.0)
    except (TypeError, ValueError):
        rsi_max = 75.0
    if float(rsi) > rsi_max:
        return False, "rsi_extended"
    if bool(cfg.get("ai_watch_arm_cm_rsi_require_rising", True)):
        rising_rsi = ind.get("cm_rsi_rising")
        if rising_rsi is None:
            rising_rsi = row.get("cm_rsi_rising")
        if not bool(rising_rsi):
            return False, "rsi_not_rising"

    if exh_oversold_triangle_arm_enabled(cfg):
        cls, _ = classify_exh_seat(row, cfg, ind=ind)
        if cls == "os_triangle":
            rising_exh = _exh_rising_hint(row, ind)
            falling = ind.get("pctr_falling") if ind else None
            if falling is None and isinstance(row, dict):
                falling = row.get("pctr_falling")
            if falling is True or rising_exh is False:
                return False, "exh_falling"
            if bool(cfg.get("ai_watch_require_exh_rising", True)) and rising_exh is not True:
                return False, "exh_falling"
            return True, "ok"

    # EXH: rising and ≥ heat_min.
    exh = _exh_from_row_or_ind(row, ind)
    if exh is None and isinstance(row.get("indicator"), dict):
        exh = exhaustion_pct(row)
    if exh is None:
        return False, "exh_rising_required"
    try:
        heat_min = float(cfg.get("ai_watch_exhaustion_heat_min_pct", 40.0) or 0.0)
    except (TypeError, ValueError):
        heat_min = 40.0
    if heat_min > 0 and float(exh) + 1e-9 < heat_min:
        return False, "exh_too_low"
    rising_exh = _exh_rising_hint(row, ind)
    falling = ind.get("pctr_falling")
    if falling is None:
        falling = row.get("pctr_falling")
    if falling is True or rising_exh is False:
        return False, "exh_falling"
    if bool(cfg.get("ai_watch_require_exh_rising", True)) and rising_exh is not True:
        return False, "exh_falling"

    return True, "ok"


def stamp_arm_ready_fields(
    row: dict,
    cfg: dict | None = None,
    *,
    indicators: dict[str, dict] | None = None,
    now: float | None = None,
    max_price: float | None = None,
) -> tuple[bool, str]:
    """Stamp ``arm_ready`` / ``arm_ready_reason`` / CHG band on *row*."""
    cfg = cfg if isinstance(cfg, dict) else {}
    ready, why = evaluate_arm_ready(
        row, cfg, indicators=indicators, now=now, max_price=max_price)
    row["arm_ready"] = bool(ready)
    row["arm_ready_reason"] = str(why or ("ok" if ready else "unknown"))
    pct = _pct_change_value(row.get("pct_change"))
    if pct is None:
        pct = _pct_change_value(row.get("admit_pct_change"))
    band = classify_admit_chg_band(pct, cfg)
    row["admit_chg_band"] = band
    if pct is not None:
        row["admit_pct_change"] = float(pct)
    return ready, why


def _row_needs_arm_ready_gate(row: dict) -> bool:
    """Soft-seed keep seats only.

    Continuous soft-seed was filling the book with never-armable inventory.
    Desk panel seeds (momentum/trending/movers/research) keep existing
    inclusion; never-armable eviction clears stuck seats after grace.
    """
    if not isinstance(row, dict):
        return False
    if row.get("soft_seed") is True:
        return True
    crit = {str(c).lower() for c in (row.get("criteria") or [])}
    return "soft_seed" in crit


def is_warming_exh_profile(
    exh: float | None,
    exh_rising: bool | None,
    cfg: dict | None = None,
    *,
    allow_unknown: bool = True,
    row: dict | None = None,
    ind: dict | None = None,
) -> bool:
    """True when EXH is a warming / pre-square scout.

    When ``ai_watch_admit_prefer_square`` is on and dual-%R is present:
    warming ≡ ``pre_square`` or ``square`` (approach/OB band + tight + rising).
    Missing slow → unknown (not pre-square); ``allow_unknown`` only then.
    Legacy fallback: single-line heat in ``warming_exh_band``.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if admit_prefer_square(cfg) and (row is not None or ind is not None):
        cls, _gap = classify_exh_seat(row if isinstance(row, dict) else {}, cfg, ind=ind)
        if cls in ("pre_square", "square", "os_square", "os_triangle"):
            return True
        if cls == "unknown":
            return bool(allow_unknown)
        return False  # far
    lo, hi = warming_exh_band(cfg)
    if exh is None:
        return bool(allow_unknown)
    if exh + 1e-9 < lo or exh - 1e-9 > hi:
        return False
    # Prefer rising; flat/unknown OK; falling is not a warming scout.
    if exh_rising is False:
        return False
    return True


def soft_seed_scout_score(
    row: dict,
    cfg: dict | None = None,
    *,
    ind: dict | None = None,
) -> float:
    """Higher = better soft-seed scout.

    Primary (when prefer_square): square / os_triangle > pre_square / os_square ≫ far.
    Secondary: day CHG% soft band (~+8…+40). Deprioritize hot RSI.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if isinstance(row, dict) and ind and "indicator" not in row:
        # Allow classify_exh_seat to see dual-%R on the row.
        pass
    cls = stamp_exh_seat_fields(row, cfg, ind=ind) if isinstance(row, dict) else "unknown"
    score = 0.0
    if admit_prefer_square(cfg):
        # Aggressive farm: square/os_triangle ≫ pre_square/os_square ≫ everything; far never
        # outranks a true pre_square on CHG alone (floor well below).
        if cls in ("square", "os_triangle"):
            score += 120.0
        elif cls in ("pre_square", "os_square"):
            score += 80.0
        elif cls == "unknown":
            score += 10.0  # scout-only candidate (missing slow ≠ pre_square)
        else:  # far
            score -= 120.0
    else:
        exh = _exh_from_row_or_ind(row, ind)
        rising = _exh_rising_hint(row, ind)
        lo, hi = warming_exh_band(cfg)
        if exh is None:
            score += 20.0
        elif lo <= exh <= hi:
            score += 50.0 + (hi - abs((lo + hi) / 2.0 - exh))
            if rising is True:
                score += 15.0
        elif exh < lo:
            score += 10.0
        else:
            score -= 40.0 + max(0.0, exh - hi)
    rsi = None
    for src in (ind, row):
        if isinstance(src, dict):
            rsi = _f_or_none(src.get("cm_rsi") or src.get("rsi"))
            if rsi is not None:
                break
    if rsi is not None and rsi >= 52.0:
        score -= 25.0
    if rsi is not None and rsi >= 55.0:
        score -= 15.0
    dvol = _f_or_none(row.get("dollar_volume")) or 0.0
    score += min(20.0, math.log10(max(dvol, 1.0)) * 2.0)
    pct = _f_or_none(row.get("pct_change"))
    if pct is None:
        pct = 0.0
    band = classify_admit_chg_band(pct, cfg)
    if isinstance(row, dict):
        row["admit_chg_band"] = band
        row["admit_pct_change"] = float(pct)
    # CHG band is secondary to square-distance.
    if band == "prefer":
        score += 15.0 if admit_prefer_square(cfg) else 25.0
    elif band == "mid":
        score += 5.0 if admit_prefer_square(cfg) else 8.0
    elif band == "below_prefer":
        score += min(8.0, max(0.0, float(pct)) * 0.2)
    elif band == "over_soft":
        if admit_pullback_ok(row, cfg):
            score -= 10.0
            if isinstance(row, dict):
                row["admit_chg_band"] = "over_soft_pullback"
        else:
            score -= 55.0
    else:
        score += min(10.0, max(0.0, float(pct)) * 0.25)
    return score


def _soft_seed_max_price(cfg: dict) -> Any:
    """Price cap for soft-seed momentum/research enrichment (same as desk seeds)."""
    try:
        from desk_risk import dynamic_max_price
        eq = float(
            dashboard_state()
            .get("ai_positions", {})
            .get("account", {})
            .get("equity") or 0.0
        )
        return dynamic_max_price(eq, cfg)
    except Exception:
        return cfg.get("ai_max_price", cfg.get("claude_max_price"))


def _soft_seed_source_rows(
    cfg: dict,
    *,
    now: float | None = None,
) -> list[dict]:
    """Lightweight soft-seed shortlist from all enabled scout sources.

    Order (first claim wins within the soft-seed batch): trending → movers →
    momentum → research. Does **not** call ``desk_candidate_rows`` / clear
    seed-drop tallies. Inclusion still gates every row later.

    During morning flood, momentum + research are not N-truncated (full desk
    / board lists) and rows are stamped ``morning_flood=1``.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    cfg = cfg if isinstance(cfg, dict) else {}
    flood = morning_flood_active(cfg, now)

    def _add(sym: str, payload: dict) -> None:
        s = str(sym or "").upper().strip()
        if not s or s in seen or is_levered_etp(s):
            return
        seen.add(s)
        rows.append(payload)

    if bool(cfg.get("ai_watch_soft_seed_trending", True)):
        try:
            path = ROOT / "trending_stocks.json"
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            for r in (raw.get("rows") or [])[:40]:
                if not isinstance(r, dict) or r.get("is_crypto") is True:
                    continue
                s = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
                if not s:
                    continue
                look = str(r.get("look_reason") or "").strip().upper()
                if look == "WASH":
                    continue
                pct = _pct_change_value(r.get("pct_change"))
                if pct is not None and pct <= 0:
                    continue
                try:
                    score = float(r.get("trending_score", r.get("score") or 0) or 0)
                except (TypeError, ValueError):
                    score = 0.0
                _add(s, {
                    "symbol": s,
                    "source": "trending",
                    "price": r.get("price"),
                    "pct_change": pct,
                    "rvol": r.get("rvol"),
                    "trending_score": score,
                    "score": score,
                    "reason": f"soft_seed trending {score:.1f}",
                    "criteria": ["soft_seed", "trending"],
                    "dollar_volume": (
                        float(r["vol_session"]) * float(r["price"])
                        if r.get("vol_session") and r.get("price") else None
                    ),
                })
        except Exception:
            pass

    if bool(cfg.get("ai_watch_soft_seed_movers", True)):
        try:
            path = ROOT / "movers_stocks.json"
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            try:
                max_age = float(cfg.get("ai_movers_max_age_sec", 900.0) or 0.0)
            except (TypeError, ValueError):
                max_age = 900.0
            age = time.time() - float(raw.get("ts") or 0)
            if max_age > 0 and raw.get("ts") and age > max_age:
                raw = {}
            for r in (raw.get("rows") or [])[:40]:
                if not isinstance(r, dict):
                    continue
                s = str(r.get("symbol") or "").upper().strip()
                if not s:
                    continue
                pct = _pct_change_value(r.get("pct_change"))
                if pct is None or pct <= 0:
                    continue
                px = r.get("price")
                dvol = None
                try:
                    if r.get("dollar_volume") is not None:
                        dvol = float(r["dollar_volume"])
                    elif px is not None and r.get("volume") is not None:
                        dvol = float(px) * float(r["volume"])
                except (TypeError, ValueError):
                    dvol = None
                _add(s, {
                    "symbol": s,
                    "source": "movers",
                    "price": px,
                    "pct_change": pct,
                    "rvol": r.get("rvol"),
                    "reason": f"soft_seed movers {pct:+.0f}%",
                    "criteria": ["soft_seed", "movers"],
                    "dollar_volume": dvol,
                    "score": float(pct),
                })
        except Exception:
            pass

    # Momentum desk / big-mover / mom_open-class — reuse helpers, never
    # desk_candidate_rows (avoids wiping seed-drop tallies).
    if bool(cfg.get("ai_watch_soft_seed_momentum", True)):
        try:
            max_price = _soft_seed_max_price(cfg)
            try:
                min_pct = float(cfg.get("ai_watch_min_pct_change", 50.0) or 50.0)
            except (TypeError, ValueError):
                min_pct = 50.0
            scored: list[tuple[float, dict]] = []
            have: set[str] = set()
            for sc, r in _momentum_flagged_from_dashboard(max_price):
                sym = str(r.get("symbol") or "").upper().strip()
                if not sym or sym in have:
                    continue
                have.add(sym)
                scored.append((float(sc), r))
            for sc, r in _big_mover_from_dashboard(max_price, min_pct):
                sym = str(r.get("symbol") or "").upper().strip()
                if not sym or sym in have:
                    continue
                have.add(sym)
                scored.append((float(sc), r))
            # Soft open-class: remaining desk names under price cap (no
            # thin_rvol seed-drop — soft-seed is scout eligibility only).
            for r in _dashboard_tickers():
                if not isinstance(r, dict):
                    continue
                s = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
                if not s or not s[0].isalpha() or s in have:
                    continue
                pct = _pct_change_value(r.get("pct_change"))
                if is_levered_etp(s):
                    _note_seed_drop("momentum", s, "levered_etp",
                                    pct=pct, price=r.get("price"))
                    continue
                if not _price_under_cap(r.get("price"), max_price):
                    _note_seed_drop("momentum", s, "price_cap",
                                    pct=pct, price=r.get("price"))
                    continue
                if _is_wash_look(r):
                    continue
                try:
                    px = float(r.get("price")) if r.get("price") is not None else None
                except (TypeError, ValueError):
                    px = None
                try:
                    dvol = float(r.get("day_vol")) if r.get("day_vol") is not None else None
                except (TypeError, ValueError):
                    dvol = None
                rank = abs(float(pct)) if pct is not None else 0.0
                have.add(s)
                scored.append((rank, {
                    "symbol": s,
                    "trending_score": round(rank, 2),
                    "score": round(rank, 2),
                    "reason": "soft_seed momentum open",
                    "agreement": True,
                    "source": "momentum",
                    "price": px,
                    "pct_change": pct,
                    "rvol": r.get("rvol"),
                    "dollar_volume": (dvol * px) if (dvol and px) else None,
                    "criteria": ["mom_open"],
                }))
            scored.sort(key=lambda t: t[0], reverse=True)
            # Morning flood: take the full momentum desk list (no N truncate).
            mom_cap = len(scored) if flood else 40
            for _, r in scored[: max(0, mom_cap)]:
                s = str(r.get("symbol") or "").upper().strip()
                if not s:
                    continue
                row = dict(r)
                row["source"] = "momentum"
                row["reason"] = (
                    f"soft_seed momentum {row.get('reason') or ''}".strip()[:80]
                )
                row["criteria"] = ["soft_seed", "momentum"]
                if flood:
                    row["morning_flood"] = 1
                _add(s, row)
        except Exception:
            pass

    # Research / suggestions boards (+ seed_rank via research_candidate_rows).
    # Enrich quotes like the desk research seed; do not re-impose thin_rvol.
    if bool(cfg.get("ai_watch_soft_seed_research", True)):
        try:
            max_price = _soft_seed_max_price(cfg)
            desk_rows, tr_by = _live_quote_map()
            added = 0
            research_cap = 10_000 if flood else 40
            for r in research_candidate_rows():
                if added >= research_cap:
                    break
                s = str(r.get("symbol") or "").upper().strip()
                if not s:
                    continue
                if is_levered_etp(s):
                    continue
                live = desk_rows.get(s) or {}
                tr = tr_by.get(s) or {}
                px_src = (
                    live.get("price") if live.get("price") is not None
                    else tr.get("price")
                )
                pct_src = (
                    live.get("pct_change")
                    if live.get("pct_change") is not None
                    else tr.get("pct_change")
                )
                rvol_src = (
                    live.get("rvol") if live.get("rvol") is not None
                    else tr.get("rvol")
                )
                if not _price_under_cap(px_src, max_price):
                    continue
                try:
                    px = float(px_src) if px_src is not None else None
                except (TypeError, ValueError):
                    px = None
                # Morning flood: missing price fail-closed (do not seat).
                if flood and px is None:
                    continue
                pct_f = _pct_change_value(pct_src)
                try:
                    dvol = (
                        float(live.get("day_vol"))
                        if live.get("day_vol") is not None else None
                    )
                except (TypeError, ValueError):
                    dvol = None
                if dvol is None and tr.get("vol_session") is not None and px:
                    try:
                        dvol = float(tr.get("vol_session"))
                    except (TypeError, ValueError):
                        dvol = None
                src = str(r.get("source") or "research").lower().strip() or "research"
                try:
                    sc_raw = r.get("score", r.get("trending_score"))
                    score_f = float(sc_raw) if sc_raw is not None else 0.0
                except (TypeError, ValueError):
                    score_f = 0.0
                row = dict(r)
                row.update({
                    "price": px,
                    "pct_change": pct_f,
                    "rvol": rvol_src,
                    "dollar_volume": (dvol * px) if (dvol and px) else None,
                    "source": src,
                    "reason": f"soft_seed research {row.get('reason') or src}"[:80],
                    "criteria": ["soft_seed", "research"],
                    "score": score_f,
                })
                if flood:
                    row["morning_flood"] = 1
                before = len(rows)
                _add(s, row)
                if len(rows) > before:
                    added += 1
        except Exception:
            pass
    return rows


# Back-compat alias (older tests / call sites).
_soft_seed_file_rows = _soft_seed_source_rows


def maybe_soft_seed_rows(
    cfg: dict,
    *,
    now: float,
    seen: set[str] | None = None,
    indicators: dict[str, dict] | None = None,
) -> tuple[list[dict], bool]:
    """Interval soft seed from trending, movers, momentum, and research.

    Knobs: ``ai_watch_soft_seed_{trending,movers,momentum,research}`` (each
    independently switchable) plus shared ``ai_watch_soft_seed_max``. Sources
    compete on ``soft_seed_scout_score`` (EXH band / RSI / dvol / pct) — no
    per-source quota. Mechanical only. Prefers EXH 15–45 rising; deprioritizes
    hot RSI. Does not call ``desk_candidate_rows`` (avoids clearing seed-drop
    tallies).

    When ``ai_watch_admit_require_arm_ready`` is on (default RTH): arm-ready
    candidates take keep seats first; non-ready warming profiles may enter as
    scout-only with short TTL; other failures do not consume soft-seed seats.
    """
    global _SOFT_SEED_LAST_TS
    cfg = cfg if isinstance(cfg, dict) else {}
    if not bool(cfg.get("ai_watch_soft_seed_enabled", True)):
        return [], False
    interval = soft_seed_interval_sec(cfg)
    if interval <= 0:
        return [], False
    t0 = float(now)
    if _SOFT_SEED_LAST_TS > 0 and (t0 - _SOFT_SEED_LAST_TS) < interval:
        return [], False
    try:
        if not trading_hours_active(cfg, t0, market_open=True):
            return [], False
    except Exception:
        pass

    try:
        max_n = max(0, int(cfg.get("ai_watch_soft_seed_max", 12) or 0))
    except (TypeError, ValueError):
        max_n = 12
    flood = morning_flood_active(cfg, t0)
    # Flood still needs a positive soft_seed_max for non-flood sources; flood
    # momentum/research bypass the cap entirely below.
    if max_n <= 0 and not flood:
        return [], False

    seen = set(seen or set())
    indicators = indicators if isinstance(indicators, dict) else {}
    max_px = None
    try:
        max_px = _soft_seed_max_price(cfg)
        if max_px is not None:
            max_px = float(max_px)
    except (TypeError, ValueError):
        max_px = None
    require_ready = admit_require_arm_ready(cfg, t0)
    prefer_sq = admit_prefer_square(cfg)
    ttl = scout_ttl_sec(cfg)
    _, _, chg_soft = admit_chg_band_bounds(cfg)
    far_cap = max_far_exh_seats(cfg)

    ranked: list[tuple[float, dict]] = []
    try:
        _src_rows = _soft_seed_source_rows(cfg, now=t0)
    except TypeError:
        # Tests / older monkeypatches may still use the one-arg signature.
        _src_rows = _soft_seed_source_rows(cfg)
    for r in _src_rows:
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym or sym in seen:
            continue
        ind = indicators.get(sym) if isinstance(indicators.get(sym), dict) else None
        out = dict(r)
        if ind and "indicator" not in out:
            out["indicator"] = dict(ind)
        sc = soft_seed_scout_score(out, cfg, ind=ind)
        crit = list(out.get("criteria") or [])
        if "soft_seed" not in crit:
            crit.append("soft_seed")
        exh = _exh_from_row_or_ind(out, ind)
        rising = _exh_rising_hint(out, ind)
        if is_warming_exh_profile(
            exh, rising, cfg, allow_unknown=True, row=out, ind=ind,
        ):
            out["seat_role"] = "warming"
            if "warming" not in crit:
                crit.append("warming")
        out["criteria"] = crit
        out["soft_seed"] = True
        stamp_arm_ready_fields(
            out, cfg, indicators=indicators, now=t0, max_price=max_px)
        ranked.append((sc, out))
    ranked.sort(key=lambda t: -t[0])

    def _far_keep_count(rows: list[dict]) -> int:
        n = 0
        for r in rows:
            if bool(r.get("scout_only")):
                continue
            if str(r.get("exh_seat_class") or "") == "far":
                n += 1
        return n

    picked: list[dict] = []

    # Morning flood: momentum + research still bypass soft_seed_max, but
    # prefer-square keeps the approach ahead of far. A far flood name does
    # not take a keep seat. Square / pre_square / oversold-square do.
    if flood:
        for _sc, row in ranked:
            if not is_morning_flood_source(row):
                continue
            if not _morning_flood_price_ok(row, cfg):
                continue
            out = dict(row)
            out["morning_flood"] = 1
            out["scout_only"] = False
            out["soft_seed"] = True
            # Stamp dual-%R class at flood seat so admit is not born unknown.
            try:
                stamp_exh_seat_fields(out, cfg)
            except Exception:
                pass
            if prefer_sq:
                cls = str(out.get("exh_seat_class") or "")
                if cls not in ("square", "pre_square", "os_square", "os_triangle"):
                    continue
            picked.append(out)
        flood_syms = {
            str(r.get("symbol") or "").upper().strip() for r in picked
        }
    else:
        flood_syms = set()

    if not require_ready and not prefer_sq:
        for sc, row in ranked:
            sym = str(row.get("symbol") or "").upper().strip()
            if sym in flood_syms:
                continue
            # Non-flood sources still honor soft_seed_max.
            other_n = sum(
                1 for r in picked
                if str(r.get("symbol") or "").upper() not in flood_syms
            )
            if max_n > 0 and other_n >= max_n:
                break
            if sc < 0 and any(s >= 20 for s, _ in ranked[: max(1, max_n or 1)]):
                continue
            picked.append(row)
        _SOFT_SEED_LAST_TS = t0
        return picked, True

    # Pass 1: square / pre_square keeps (arm-ready when required).
    # Flood momentum/research already seated above; skip them here.
    for sc, row in ranked:
        sym = str(row.get("symbol") or "").upper().strip()
        if sym in flood_syms:
            continue
        other_n = sum(
            1 for r in picked
            if str(r.get("symbol") or "").upper() not in flood_syms
        )
        if max_n > 0 and other_n >= max_n:
            break
        if sc < 0 and any(s >= 20 for s, _ in ranked[: max(1, max_n or 1)]):
            continue
        cls = str(row.get("exh_seat_class") or "")
        if prefer_sq and cls not in ("square", "pre_square", "os_square", "os_triangle"):
            continue
        if require_ready and not bool(row.get("arm_ready")):
            # Prefer-square: pre_square, square, os_square, os_triangle keep seats without full
            # arm_ready — arm_ready gates the OPEN, not the approach seat.
            if cls not in ("square", "pre_square", "os_square", "os_triangle"):
                continue
        pct = _pct_change_value(row.get("pct_change"))
        if pct is None:
            pct = _pct_change_value(row.get("admit_pct_change"))
        if (
            pct is not None
            and float(pct) > chg_soft
            and not admit_pullback_ok(row, cfg)
        ):
            row["arm_ready"] = False
            row["arm_ready_reason"] = "chg_band"
            row["admit_chg_band"] = "over_soft"
            continue
        row["scout_only"] = False
        picked.append(row)

    def _other_keep_n() -> int:
        return sum(
            1 for r in picked
            if str(r.get("symbol") or "").upper() not in flood_syms
        )

    # Pass 1b: limited far keeps. prefer_square alone starves far.
    # Heating-with-square: only rising-heat far seats (both lines rising,
    # tight, heat band) — falling/stale far must not fill the book.
    heat_with_sq = exh_heating_with_square(cfg)
    if (not prefer_sq or heat_with_sq) and (max_n <= 0 or _other_keep_n() < max_n):
        for sc, row in ranked:
            sym = str(row.get("symbol") or "").upper().strip()
            if sym in flood_syms:
                continue
            if max_n > 0 and _other_keep_n() >= max_n:
                break
            if str(row.get("exh_seat_class") or "") != "far":
                continue
            if far_cap >= 0 and _far_keep_count(picked) >= far_cap:
                break
            if heat_with_sq and not rising_heat_quality(row, cfg):
                continue
            if require_ready and not bool(row.get("arm_ready")):
                # Rising-heat approach seats may warm without full arm_ready
                # (price-rise window still filling).
                if not (heat_with_sq and rising_heat_quality(row, cfg)):
                    continue
            row["scout_only"] = False
            row["rising_heat_seat"] = True
            picked.append(row)

    # Pass 2: scout-only warming / unknown (short TTL) — not far keeps.
    if max_n <= 0 or _other_keep_n() < max_n:
        picked_syms = {
            str(r.get("symbol") or "").upper() for r in picked
        }
        for sc, row in ranked:
            if max_n > 0 and _other_keep_n() >= max_n:
                break
            sym = str(row.get("symbol") or "").upper().strip()
            if not sym or sym in picked_syms or sym in flood_syms:
                continue
            cls = str(row.get("exh_seat_class") or "")
            if prefer_sq and cls == "far":
                continue  # far does not consume soft-seed seats
            if cls in ("square", "pre_square", "os_square", "os_triangle") and not bool(row.get("scout_only")):
                # Already eligible for keep; skip duplicate scout.
                if any(
                    str(p.get("symbol") or "").upper() == sym for p in picked
                ):
                    continue
            why = str(row.get("arm_ready_reason") or "")
            if why == "above_max_price":
                continue
            pct = _pct_change_value(row.get("pct_change"))
            if pct is None:
                pct = _pct_change_value(row.get("admit_pct_change"))
            if (
                pct is not None
                and float(pct) > chg_soft
                and not admit_pullback_ok(row, cfg)
            ):
                row["arm_ready_reason"] = "chg_band"
                continue
            ind_r = (
                row.get("indicator")
                if isinstance(row.get("indicator"), dict) else None
            )
            exh = _exh_from_row_or_ind(row, ind_r)
            rising = _exh_rising_hint(row, ind_r)
            # Unknown (no slow) may scout briefly; far never.
            allow_unk = cls == "unknown"
            if not is_warming_exh_profile(
                exh, rising, cfg, allow_unknown=allow_unk, row=row, ind=ind_r,
            ):
                if not (allow_unk and prefer_sq):
                    continue
            out = dict(row)
            out["scout_only"] = True
            out["seat_role"] = "warming"
            crit = list(out.get("criteria") or [])
            if "warming" not in crit:
                crit.append("warming")
            if "scout_only" not in crit:
                crit.append("scout_only")
            out["criteria"] = crit
            if ttl > 0:
                out["scout_until"] = float(t0) + float(ttl)
            picked.append(out)
            picked_syms.add(sym)

    _SOFT_SEED_LAST_TS = t0
    return picked, True


def tag_warming_on_candidates(
    rows: list[dict],
    cfg: dict,
    *,
    indicators: dict[str, dict] | None = None,
) -> int:
    """Stamp ``seat_role=warming`` on inclusion candidates in the pre-heat band.

    Returns count tagged. Does not bypass inclusion — tag only.
    """
    indicators = indicators if isinstance(indicators, dict) else {}
    n = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        ind = indicators.get(sym) if sym else None
        if isinstance(ind, dict) and not isinstance(r.get("indicator"), dict):
            r["indicator"] = dict(ind)
        stamp_exh_seat_fields(r, cfg, ind=ind if isinstance(ind, dict) else None)
        exh = _exh_from_row_or_ind(r, ind if isinstance(ind, dict) else None)
        rising = _exh_rising_hint(r, ind if isinstance(ind, dict) else None)
        if not is_warming_exh_profile(
            exh, rising, cfg, allow_unknown=False, row=r,
            ind=ind if isinstance(ind, dict) else None,
        ):
            continue
        r["seat_role"] = "warming"
        crit = list(r.get("criteria") or [])
        if "warming" not in crit:
            crit.append("warming")
        r["criteria"] = crit
        n += 1
    return n


def _is_protected_warming_seat(
    rec: dict,
    cfg: dict | None,
    *,
    now: float,
) -> bool:
    """Warming + young stream + not falling EXH — protect from stale_tape_cap."""
    if not isinstance(rec, dict):
        return False
    if str(rec.get("seat_role") or "").strip().lower() != "warming":
        return False
    if not _is_stream_ready_seat(rec, cfg, now=now):
        return False
    ind = rec.get("indicator") if isinstance(rec.get("indicator"), dict) else {}
    if ind.get("pctr_falling") is True:
        return False
    return True


# Elite-6 pin stream-ready accrual (same ET day). Process-local v1 + rec stamp.
_PIN_READY_ACCUM: dict[str, float] = {}
_PIN_READY_MARK: dict[str, float] = {}
_PIN_READY_DAY: str = ""
_PIN_HEAT_SOURCES = frozenset({"momentum", "movers", "trending"})


def _pin_roll_et_day(now: float) -> None:
    global _PIN_READY_DAY
    day = _et_day_key(now)
    if day == _PIN_READY_DAY:
        return
    _PIN_READY_ACCUM.clear()
    _PIN_READY_MARK.clear()
    _PIN_READY_DAY = day


def _is_protected_pin_seat(
    rec: dict,
    cfg: dict | None,
    *,
    now: float,
) -> bool:
    """True when seat_role=pin and steal protection is enabled.

    Pins are immune to preheat_steal / unarmable_steal / stale_tape_cap.
    Demote-dead and A2 still clear pin status separately. Also true while a
    pin is inside stale-restream grace (dig 2026-09-15 B2 light).
    """
    if not isinstance(rec, dict):
        return False
    cfg = cfg if isinstance(cfg, dict) else {}
    sym = str(rec.get("symbol") or "").upper().strip()
    if sym and _within_stale_restream_grace(sym, now, cfg):
        if str(rec.get("seat_role") or "").strip().lower() == "pin":
            return True
    if not bool(cfg.get("ai_watch_pin_protect_steals", True)):
        return False
    return str(rec.get("seat_role") or "").strip().lower() == "pin"


def _pin_ready_sec(sym: str, rec: dict | None = None) -> float:
    sym_u = str(sym or "").upper().strip()
    if sym_u and sym_u in _PIN_READY_ACCUM:
        return float(_PIN_READY_ACCUM.get(sym_u) or 0.0)
    if isinstance(rec, dict):
        v = _f_or_none(rec.get("pin_stream_ready_sec"))
        if v is not None:
            return float(v)
    return 0.0


def _accumulate_pin_stream_ready(
    rec: dict,
    cfg: dict | None,
    *,
    now: float,
) -> None:
    """Accrue same-ET-day stream-ready seconds; track continuous dead for pins."""
    if not isinstance(rec, dict):
        return
    sym = str(rec.get("symbol") or "").upper().strip()
    if not sym:
        return
    _pin_roll_et_day(now)
    ready = _is_stream_ready_seat(rec, cfg, now=now)
    if ready:
        last = _PIN_READY_MARK.get(sym)
        if last is not None and float(last) < float(now):
            _PIN_READY_ACCUM[sym] = float(
                _PIN_READY_ACCUM.get(sym) or 0.0
            ) + (float(now) - float(last))
        elif sym not in _PIN_READY_ACCUM:
            _PIN_READY_ACCUM[sym] = float(
                _f_or_none(rec.get("pin_stream_ready_sec")) or 0.0
            )
        _PIN_READY_MARK[sym] = float(now)
        rec["pin_stream_ready_sec"] = float(_PIN_READY_ACCUM.get(sym) or 0.0)
        rec.pop("pin_dead_since", None)
        return
    _PIN_READY_MARK.pop(sym, None)
    if str(rec.get("seat_role") or "").strip().lower() == "pin":
        if _f_or_none(rec.get("pin_dead_since")) is None:
            rec["pin_dead_since"] = float(now)


def _pin_metrics_snapshot(rec: dict, sym: str) -> dict:
    dvol = _f_or_none(rec.get("admit_dollar_volume"))
    if dvol is None:
        dvol = _f_or_none(rec.get("dollar_volume"))
    return {
        "stream_ready_sec": round(_pin_ready_sec(sym, rec), 1),
        "dollar_volume": round(float(dvol), 0) if dvol is not None else None,
        "source": str(rec.get("source") or "")[:24] or None,
        "status": str(rec.get("status") or "")[:16] or None,
    }


def _log_pin_event(
    events: list | None,
    cp,
    kind: str,
    *,
    symbol: str,
    reason: str,
    rec: dict | None = None,
) -> None:
    snap = _pin_metrics_snapshot(rec or {}, symbol)
    payload = {"kind": kind, "symbol": symbol, "reason": reason, **snap}
    if events is None:
        events = []
    try:
        if cp is not None:
            events.append(cp.log_event(kind, symbol=symbol, reason=reason, **snap))
            return
    except Exception:  # noqa: BLE001
        pass
    events.append(payload)


def _apply_pin_roles(
    state: dict,
    *,
    cfg: dict | None,
    now: float,
    events: list | None = None,
    cp=None,
) -> list[str]:
    """Demote dead pins and promote heat+stream-ready seats into free pin slots.

    Returns symbols newly promoted (caller may ensure_watch_stream). Process-
    local stream-ready accrual; prefer heat sources (momentum/movers/trending).
    """
    if not isinstance(state, dict):
        return []
    cfg = cfg if isinstance(cfg, dict) else {}
    events = events if isinstance(events, list) else []
    slots = pin_slot_quota(cfg)
    promoted: list[str] = []

    for rec in state.values():
        if isinstance(rec, dict):
            _accumulate_pin_stream_ready(rec, cfg, now=now)

    if slots <= 0:
        for key, rec in list(state.items()):
            if not isinstance(rec, dict):
                continue
            if str(rec.get("seat_role") or "").strip().lower() != "pin":
                continue
            sym = str(rec.get("symbol") or key or "").upper().strip()
            rec["seat_role"] = "warming"
            rec.pop("pin_dead_since", None)
            if sym:
                _log_pin_event(
                    events, cp, "pin_demote", symbol=sym,
                    reason="pins_disabled", rec=rec)
        return promoted

    try:
        demote_dead = max(
            0.0, float(cfg.get("ai_watch_pin_demote_dead_sec", 600.0) or 0.0))
    except (TypeError, ValueError):
        demote_dead = 600.0
    try:
        min_ready = max(
            0.0,
            float(cfg.get("ai_watch_pin_min_stream_ready_sec", 120.0) or 0.0))
    except (TypeError, ValueError):
        min_ready = 120.0
    try:
        min_dvol = max(
            0.0,
            float(cfg.get("ai_watch_pin_min_dollar_volume", 2e6) or 0.0))
    except (TypeError, ValueError):
        min_dvol = 2e6

    # Demote: continuous dead / A2-demoted lose pin protection.
    for key, rec in list(state.items()):
        if not isinstance(rec, dict):
            continue
        if str(rec.get("seat_role") or "").strip().lower() != "pin":
            continue
        sym = str(rec.get("symbol") or key or "").upper().strip()
        if not sym:
            continue
        if _no_stream_strike_demoted(sym, now, cfg):
            rec["seat_role"] = "warming"
            rec.pop("pin_dead_since", None)
            _log_pin_event(
                events, cp, "pin_demote", symbol=sym,
                reason="a2_demoted", rec=rec)
            continue
        dead_since = _f_or_none(rec.get("pin_dead_since"))
        if (
            demote_dead > 0
            and dead_since is not None
            and (float(now) - float(dead_since)) >= demote_dead
        ):
            rec["seat_role"] = "warming"
            rec.pop("pin_dead_since", None)
            _log_pin_event(
                events, cp, "pin_demote", symbol=sym,
                reason="dead_timeout", rec=rec)

    pin_n = sum(
        1 for rec in state.values()
        if isinstance(rec, dict)
        and str(rec.get("seat_role") or "").strip().lower() == "pin"
    )
    free = max(0, slots - pin_n)
    if free <= 0:
        return promoted

    ranked: list[tuple[float, float, str, dict]] = []
    for key, rec in state.items():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("seat_role") or "").strip().lower() == "pin":
            continue
        sym = str(rec.get("symbol") or key or "").upper().strip()
        if not sym:
            continue
        status = str(rec.get("status") or "").lower().strip()
        if status not in ("watching", "armed"):
            continue
        if _no_stream_strike_demoted(sym, now, cfg):
            continue
        src = str(rec.get("source") or "").lower().strip()
        if src not in _PIN_HEAT_SOURCES:
            continue
        if not _is_stream_ready_seat(rec, cfg, now=now):
            continue
        ready_sec = _pin_ready_sec(sym, rec)
        if ready_sec < min_ready:
            continue
        dvol = _f_or_none(rec.get("admit_dollar_volume"))
        if dvol is None:
            dvol = _f_or_none(rec.get("dollar_volume")) or 0.0
        if float(dvol) < min_dvol:
            continue
        ranked.append((float(ready_sec), float(dvol), sym, rec))
    ranked.sort(key=lambda t: (-t[0], -t[1], t[2]))
    for ready_sec, dvol, sym, rec in ranked[:free]:
        rec["seat_role"] = "pin"
        rec.pop("pin_dead_since", None)
        rec["pin_stream_ready_sec"] = float(ready_sec)
        promoted.append(sym)
        _log_pin_event(
            events, cp, "pin_promote", symbol=sym,
            reason="metrics", rec=rec)
    return promoted


def _stale_timeout_blocked(
    symbol: str,
    now: float,
    *,
    cfg: dict | None = None,
    row: dict | None = None,
) -> bool:
    """True while a prior stale_timeout drop refuses re-seed (tape still dead).

    Clears immediately when a young stream print is available — the admit
    path logs ``reseed_allowed_stream`` via ``_consume_reseed_stream_clear``.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return False
    until = float(_STALE_TIMEOUT_UNTIL.get(sym) or 0.0)
    if until <= 0:
        return False
    if now >= until:
        _STALE_TIMEOUT_UNTIL.pop(sym, None)
        return False
    if _young_stream_alive(sym, cfg, now=now, row=row):
        _STALE_TIMEOUT_UNTIL.pop(sym, None)
        _RESEED_STREAM_CLEARED.add(sym)
        return False
    return True


def _consume_reseed_stream_clear(symbol: str) -> bool:
    sym = str(symbol or "").upper().strip()
    if not sym:
        return False
    if sym in _RESEED_STREAM_CLEARED:
        _RESEED_STREAM_CLEARED.discard(sym)
        return True
    return False


def _mark_stale_timeout_block(
    symbol: str,
    now: float,
    cfg: dict | None,
    *,
    cool_sec: float | None = None,
) -> None:
    cool = float(cool_sec) if cool_sec is not None else stale_timeout_reseed_sec(cfg)
    sym = str(symbol or "").upper().strip()
    if not sym or cool <= 0:
        return
    _STALE_TIMEOUT_UNTIL[sym] = float(now) + cool


def _clear_stale_feed_since(rec: dict) -> None:
    if isinstance(rec, dict):
        rec.pop("stale_feed_since", None)


def _maybe_stale_timeout_drop(
    rec: dict,
    *,
    sym: str,
    px_src: str | None,
    cfg: dict,
    now: float,
    events: list,
    cp,
    gt,
) -> bool:
    """Drop a watch stuck on dead stale_tape past the timeout.

    Returns True when the symbol was dropped (caller should ``continue``).
    Never drops submitted/filled rows or open broker positions. Does not
    start the clock during post-admit grace, and by default ignores brief
    need-stream (subscribe lag) — only pure stale_tape counts.
    """
    limit = stale_timeout_sec(cfg)
    if limit <= 0:
        _clear_stale_feed_since(rec)
        return False
    if not _on_book_grace_ok(rec, cfg, now):
        _clear_stale_feed_since(rec)
        return False
    if not _stale_feed_condition(rec, px_src, cfg, now=now):
        _clear_stale_feed_since(rec)
        _clear_stale_restream(sym)
        return False
    status = str(rec.get("status") or "").lower().strip()
    if status in ("submitted", "filled"):
        return False
    try:
        if gt.has_open_position(sym):
            return False
    except Exception:
        pass
    since = _f_or_none(rec.get("stale_feed_since"))
    if since is None or since <= 0 or since > now:
        rec["stale_feed_since"] = float(now)
        return False
    elapsed = float(now) - float(since)
    if elapsed < limit:
        return False
    age_f = row_quote_age_sec(rec, now=now)
    if _maybe_stale_restream_hold(
        rec, sym=sym, cfg=cfg, now=now, events=events, cp=cp,
        age_sec=age_f, src=str(px_src or "") or None, reason="stale_timeout",
    ):
        return False
    try:
        events.append(cp.log_event(
            "watch_drop", symbol=sym, reason="stale_timeout",
            elapsed_sec=round(elapsed, 1), src=px_src,
            block=str(rec.get("block_code") or ""),
            seat_role=str(rec.get("seat_role") or "") or None))
    except Exception:  # noqa: BLE001
        events.append({
            "kind": "watch_drop",
            "symbol": sym,
            "reason": "stale_timeout",
            "elapsed_sec": round(elapsed, 1),
        })
    _clear_stale_restream(sym)
    _mark_stale_timeout_block(sym, now, cfg)
    drop_watch_symbols([sym])
    return True


def _maybe_no_trade_after_subscribe_drop(
    rec: dict,
    *,
    sym: str,
    cfg: dict,
    now: float,
    events: list,
    cp,
    gt,
) -> bool:
    """Drop a watch that never got a young stream print after subscribe.

    Dig 2026-09-04: AEHG/AOUT/LABX stayed Finnhub-book-subscribed with
    3–14 min-old rt ages — quiet_max treated them as alive, so the book
    filled with permanent stale_quote false opportunities. After admit +
    subscribe grace + ``ai_watch_no_trade_after_subscribe_sec``, require a
    young stream (age ≤ decision ceiling) or drop.
    """
    limit = no_trade_after_subscribe_sec(cfg)
    if limit <= 0:
        return False
    status = str(rec.get("status") or "").lower().strip()
    if status in ("submitted", "filled"):
        return False
    try:
        if gt.has_open_position(sym):
            return False
    except Exception:
        pass
    admitted = _f_or_none(rec.get("admit_ts"))
    if admitted is None or admitted <= 0:
        return False
    grace = stale_timeout_grace_sec(cfg)
    try:
        sub_grace = float(
            (cfg or {}).get("ai_watch_stream_subscribe_grace_sec", grace)
            or grace)
    except (TypeError, ValueError):
        sub_grace = grace
    wait = max(0.0, float(grace), float(sub_grace)) + float(limit)
    if float(now) - float(admitted) < wait:
        return False
    ceiling = decision_max_age_sec(cfg)
    # Young live tape (≤ decision ceiling) → keep.
    age_f: float | None = None
    try:
        got = live_print(sym)
        if got is not None and got[1] is not None:
            age_f = float(got[1])
            if age_f <= ceiling:
                _clear_stale_feed_since(rec)
                _clear_stale_restream(sym)
                return False
    except Exception:
        pass
    src = str(
        rec.get("last_ask_src") or rec.get("price_src") or ""
    ).strip().lower()
    if age_f is None:
        age = row_quote_age_sec(rec, now=now)
        if age is None:
            age = rec.get("last_ask_age_sec")
        try:
            age_f = float(age) if age is not None else None
        except (TypeError, ValueError):
            age_f = None
    if src == "stream" and age_f is not None and age_f <= ceiling:
        _clear_stale_feed_since(rec)
        _clear_stale_restream(sym)
        return False
    # Dig 2026-09-15 B1: re-stream once and hold drop during grace before
    # freeing the seat. Arm/fill still blocked on stale. Pins/warming always
    # eligible; pins_only knob narrows if dig says thrash is pin-only.
    if _maybe_stale_restream_hold(
        rec, sym=sym, cfg=cfg, now=now, events=events, cp=cp,
        age_sec=age_f, src=src or None, reason="no_stream_trade",
    ):
        return False
    # Stream-before-strike grace: recently ensure_watch_stream'd names still
    # drop (free the seat) but do not accrue A2 strikes while subscribe lags.
    # Arm/fill remain blocked on no stream. After grace, strikes resume.
    grace_hold = _within_no_stream_strike_grace(sym, now, cfg)
    seat_role = str(rec.get("seat_role") or "") or None
    if grace_hold:
        ensured_at = _STREAM_ENSURED_AT.get(sym)
        try:
            grace_age = (
                round(float(now) - float(ensured_at), 1)
                if ensured_at is not None else None
            )
        except (TypeError, ValueError):
            grace_age = None
        last_log = _NO_STREAM_GRACE_LOGGED_AT.get(sym)
        if last_log is None or (
            ensured_at is not None and float(last_log) < float(ensured_at)
        ):
            _NO_STREAM_GRACE_LOGGED_AT[sym] = float(now)
            try:
                events.append(cp.log_event(
                    "no_stream_grace", symbol=sym,
                    grace_sec=no_stream_strike_grace_sec(cfg),
                    ensure_age_sec=grace_age,
                    elapsed_sec=round(float(now) - float(admitted), 1),
                    age_sec=round(age_f, 1) if age_f is not None else None,
                    src=src or None, seat_role=seat_role))
            except Exception:  # noqa: BLE001
                events.append({
                    "kind": "no_stream_grace",
                    "symbol": sym,
                    "grace_sec": no_stream_strike_grace_sec(cfg),
                    "ensure_age_sec": grace_age,
                })
        strikes = _no_stream_strike_count(sym, now)
        demoted = False
        try:
            events.append(cp.log_event(
                "watch_drop", symbol=sym, reason="no_stream_trade",
                elapsed_sec=round(float(now) - float(admitted), 1),
                age_sec=round(age_f, 1) if age_f is not None else None,
                src=src or None, seat_role=seat_role,
                no_stream_strikes=strikes,
                no_stream_strike_grace=True,
                no_stream_strike_demote=None))
        except Exception:  # noqa: BLE001
            events.append({
                "kind": "watch_drop",
                "symbol": sym,
                "reason": "no_stream_trade",
                "elapsed_sec": round(float(now) - float(admitted), 1),
                "no_stream_strikes": strikes,
                "no_stream_strike_grace": True,
                "seat_role": seat_role,
            })
    else:
        strikes = _record_no_stream_strike(sym, now, "no_stream_trade", cfg)
        limit = no_stream_strike_limit(cfg)
        demoted = bool(limit > 0 and strikes >= limit)
        try:
            events.append(cp.log_event(
                "watch_drop", symbol=sym, reason="no_stream_trade",
                elapsed_sec=round(float(now) - float(admitted), 1),
                age_sec=round(age_f, 1) if age_f is not None else None,
                src=src or None, seat_role=seat_role,
                no_stream_strikes=strikes,
                no_stream_strike_limit=limit if limit > 0 else None,
                no_stream_strike_demote=demoted or None))
        except Exception:  # noqa: BLE001
            events.append({
                "kind": "watch_drop",
                "symbol": sym,
                "reason": "no_stream_trade",
                "elapsed_sec": round(float(now) - float(admitted), 1),
                "no_stream_strikes": strikes,
                "no_stream_strike_demote": demoted or None,
                "seat_role": seat_role,
            })
        if demoted:
            try:
                events.append(cp.log_event(
                    "no_stream_strike_demote", symbol=sym,
                    strikes=strikes, limit=limit,
                    et_day=_et_day_key(now)))
            except Exception:  # noqa: BLE001
                events.append({
                    "kind": "no_stream_strike_demote",
                    "symbol": sym,
                    "strikes": strikes,
                    "limit": limit,
                    "et_day": _et_day_key(now),
                })
    _clear_stale_restream(sym)
    _mark_stale_timeout_block(
        sym, now, cfg, cool_sec=stale_timeout_reseed_sec(cfg))
    drop_watch_symbols([sym])
    return True


def _enforce_stale_tape_seat_cap(
    state: dict,
    *,
    cfg: dict,
    now: float,
    events: list,
    cp,
    gt,
    candidates: list | None = None,
) -> list[str]:
    """Drop excess watching stale_tape rows when a young-stream admittee waits.

    Demand-driven: if ``candidates`` is None/empty, or none are off-book with
    young stream (``_candidate_young_stream_age``), return [] — do not churn
    seats into vacuum. When ≥1 waiting young-stream admittee exists, keep up
    to ``ai_watch_max_stale_tape_seats`` (highest $vol, youngest age) and drop
    the rest. <0 disables. Cap drops free seats only — no reseed cool (same
    contract as unarmable_steal / e8ff57e).
    """
    cap = max_stale_tape_seats(cfg)
    if cap < 0 or not isinstance(state, dict):
        return []
    on_book = {
        str(rec.get("symbol") or key or "").upper().strip()
        for key, rec in state.items()
        if isinstance(rec, dict)
    }
    waiting = 0
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        sym = str(cand.get("symbol") or "").upper().strip()
        if not sym or sym in on_book:
            continue
        if _candidate_young_stream_age(cand, cfg, now=now) is None:
            continue
        waiting += 1
        break
    if waiting <= 0:
        return []
    stale: list[tuple[str, dict, float, float]] = []
    for key, rec in state.items():
        if not isinstance(rec, dict):
            continue
        status = str(rec.get("status") or "").lower().strip()
        if status in ("submitted", "filled", "armed"):
            continue
        src = str(
            rec.get("last_ask_src") or rec.get("price_src") or ""
        ).strip().lower()
        if src != "stale_tape":
            continue
        # Protect pins and warming+young-stream from thrash drops.
        if _is_protected_pin_seat(rec, cfg, now=now):
            continue
        if _is_protected_warming_seat(rec, cfg, now=now):
            continue
        sym = str(rec.get("symbol") or key or "").upper().strip()
        if not sym:
            continue
        # Never steal/cap-drop a seat mid restream grace (B2).
        if _within_stale_restream_grace(sym, now, cfg):
            continue
        try:
            if gt is not None and gt.has_open_position(sym):
                continue
        except Exception:
            pass
        dvol = _f_or_none(rec.get("admit_dollar_volume")) or 0.0
        age = row_quote_age_sec(rec, now=now)
        if age is None:
            age = _f_or_none(rec.get("last_ask_age_sec"))
        age_f = float(age) if age is not None else 1e9
        stale.append((sym, rec, float(dvol), age_f))
    if len(stale) <= cap:
        return []
    # Keep best first: high $vol, then younger tape.
    stale.sort(key=lambda t: (-t[2], t[3], t[0]))
    dropped: list[str] = []
    for sym, _rec, dvol, age_f in stale[cap:]:
        try:
            events.append(cp.log_event(
                "watch_drop", symbol=sym, reason="stale_tape_cap",
                age_sec=round(age_f, 1) if age_f < 1e8 else None,
                dollar_volume=round(dvol, 0) if dvol else None,
                cap=cap))
        except Exception:  # noqa: BLE001
            events.append({
                "kind": "watch_drop", "symbol": sym,
                "reason": "stale_tape_cap",
            })
        dropped.append(sym)
    if dropped:
        drop_watch_symbols(dropped)
    return dropped


def _is_stream_ready_seat(
    rec: dict,
    cfg: dict | None,
    *,
    now: float,
) -> bool:
    """True when a watching/armed row has young stream tape (≤ decision ceiling)."""
    if not isinstance(rec, dict):
        return False
    status = str(rec.get("status") or "").lower().strip()
    if status not in ("watching", "armed"):
        return False
    src = str(
        rec.get("last_ask_src") or rec.get("price_src") or ""
    ).strip().lower()
    if not price_src_fresh(src):
        return False
    age = row_quote_age_sec(rec, now=now)
    if age is None:
        age = _f_or_none(rec.get("last_ask_age_sec"))
    if age is None:
        return False
    return float(age) <= decision_max_age_sec(cfg)


def _is_unarmable_stale_watching(
    rec: dict,
    cfg: dict | None,
    *,
    now: float,
) -> bool:
    """True for a watching-only seat that cannot arm (stale tape / sticky quote).

    Past subscribe grace only. Confirmed via ``stale_tape`` src, sticky
    ``stale_quote`` block, or tape_only (``stale_tape``). Never armed /
    submitted / filled. Also true for sticky RSI/EXH never-armable blocks
    past ``ai_watch_unarmable_evict_sec`` so arm-ready admits can steal.
    """
    if not isinstance(rec, dict):
        return False
    status = str(rec.get("status") or "").lower().strip()
    if status != "watching":
        return False
    if _within_subscribe_grace(rec, cfg, now):
        return False
    # Young stream seat is armable on tape — never steal for *stale* alone.
    # RSI/EXH stuck seats may still be young-tape but never armable.
    code = str(rec.get("block_code") or "").strip().lower()
    if code in ("rsi_not_rising", "rsi_extended", "exh_falling",
                "exh_not_rising", "exh_rising_required", "above_max_price",
                "exh_not_tight", "wait_exh"):
        limit = unarmable_evict_sec(cfg)
        if limit <= 0:
            return False
        since = _f_or_none(rec.get("unarmable_since"))
        if since is None:
            since = _f_or_none(rec.get("block_ts"))
        if since is None or since <= 0:
            return False
        return (float(now) - float(since)) >= limit
    # Far dual-%R seats (square bus): stealable past far_exh grace — including
    # pins AND morning-flood sources so a far-only flood spray cannot freeze
    # the book. Tape-dead grace (_within_stale_restream_grace) still holds.
    if admit_prefer_square(cfg):
        cls = str(rec.get("exh_seat_class") or "")
        if not cls:
            cls, _ = classify_exh_seat(rec, cfg)
        if cls == "far":
            limit = far_exh_evict_sec(cfg)
            if limit > 0:
                since = _f_or_none(rec.get("far_exh_since"))
                if since is not None and (float(now) - float(since)) >= limit:
                    return True
    if _is_stream_ready_seat(rec, cfg, now=now):
        return False
    src = str(
        rec.get("last_ask_src") or rec.get("price_src") or ""
    ).strip().lower()
    tape_only = src == "stale_tape"
    sticky_stale_quote = code == "stale_quote"
    if not (tape_only or sticky_stale_quote or src in ("none", "")):
        return False
    # Anti-thrash confirm: sticky block, streak, or dead-feed clock.
    try:
        streak = int(rec.get("stale_tape_streak") or 0)
    except (TypeError, ValueError):
        streak = 0
    since = _f_or_none(rec.get("stale_feed_since"))
    if sticky_stale_quote or streak >= 1 or (since is not None and since > 0):
        return True
    # Pure stale_tape src after grace is itself confirmation (seat-cap spirit).
    return tape_only or src in ("none", "")


def _track_unarmable_block(rec: dict, *, now: float, cfg: dict | None = None) -> None:
    """Start/clear ``unarmable_since`` from sticky never-armable block codes.

    With ai_watch_admit_arm_gates on, the arm's hard-gate codes (spread_wide,
    gapped_down, price band) count too; otherwise their clock was cleared on
    every poll and a gate-blocked seat could never age into an eviction.
    """
    if not isinstance(rec, dict):
        return
    code = str(rec.get("block_code") or "").strip().lower()
    sticky = code in _NEVER_ARMABLE_BLOCK_CODES or (
        admit_arm_gates_enabled(cfg) and code in _ADMIT_GATE_CODES)
    if sticky:
        if _f_or_none(rec.get("unarmable_since")) is None:
            # Prefer block_ts so a long-stuck code does not get a fresh clock.
            bt = _f_or_none(rec.get("block_ts"))
            rec["unarmable_since"] = float(bt) if bt is not None else float(now)
    else:
        rec.pop("unarmable_since", None)


def _maybe_scout_ttl_drop(
    rec: dict,
    *,
    sym: str,
    cfg: dict,
    now: float,
    events: list,
    cp,
    gt,
) -> bool:
    """Drop scout-only warming seats past ``scout_until``. Returns True if dropped."""
    if not isinstance(rec, dict):
        return False
    until = _f_or_none(rec.get("scout_until"))
    if until is None:
        return False
    if not bool(rec.get("scout_only")) and str(
        rec.get("seat_role") or ""
    ).lower() != "warming":
        # scout_until without scout_only — still honor expiry.
        pass
    if float(now) < float(until):
        return False
    status = str(rec.get("status") or "").lower().strip()
    if status in ("submitted", "filled", "armed"):
        return False
    # Arm-ready now → promote out of scout TTL instead of dropping.
    try:
        ready, _why = evaluate_arm_ready(rec, cfg, now=now)
        if ready:
            rec["scout_only"] = False
            rec.pop("scout_until", None)
            rec["arm_ready"] = True
            rec["arm_ready_reason"] = "ok"
            return False
    except Exception:
        pass
    try:
        if gt is not None and gt.has_open_position(sym):
            return False
    except Exception:
        pass
    try:
        events.append(cp.log_event(
            "watch_drop", symbol=sym, reason="scout_ttl",
            arm_ready=False,
            arm_ready_reason=str(rec.get("arm_ready_reason") or "scout_ttl"),
            admit_pct_change=_f_or_none(rec.get("admit_pct_change")),
            admit_chg_band=str(rec.get("admit_chg_band") or "") or None,
            seat_role=str(rec.get("seat_role") or "") or None))
    except Exception:  # noqa: BLE001
        events.append({
            "kind": "watch_drop", "symbol": sym, "reason": "scout_ttl",
            "arm_ready": False,
        })
    drop_watch_symbols([sym])
    return True


def _maybe_never_armable_evict(
    rec: dict,
    *,
    sym: str,
    cfg: dict,
    now: float,
    events: list,
    cp,
    gt,
) -> bool:
    """Drop watching seats stuck on never-armable blocks past short grace."""
    limit = unarmable_evict_sec(cfg)
    if limit <= 0 or not isinstance(rec, dict):
        return False
    status = str(rec.get("status") or "").lower().strip()
    if status != "watching":
        return False
    if _is_protected_pin_seat(rec, cfg, now=now):
        return False
    if _within_subscribe_grace(rec, cfg, now):
        return False
    _track_unarmable_block(rec, now=now, cfg=cfg)
    code = str(rec.get("block_code") or "").strip().lower()
    gate_evict = admit_arm_gates_enabled(cfg) and code in _ADMIT_GATE_CODES
    if code not in _NEVER_ARMABLE_BLOCK_CODES and not gate_evict:
        return False
    # Stale tape already has stale_timeout / no_stream_trade — avoid double drop
    # unless the code is RSI/EXH/above_max (the inventory that blocked opens).
    if code in ("stale_quote", "stale_tape", "no_quote", "no_quote_age",
                "stream_required"):
        # Align with stale timeout: only evict here when past the shorter
        # unarmable grace AND stale_timeout would not already own the drop.
        # Prefer dedicated stale paths; this branch covers RSI/EXH primarily.
        return False
    since = _f_or_none(rec.get("unarmable_since"))
    if since is None or since <= 0:
        return False
    if (float(now) - float(since)) < limit:
        return False
    try:
        if gt is not None and gt.has_open_position(sym):
            return False
    except Exception:
        pass
    # Re-check: if now arm-ready, clear clock instead of dropping.
    try:
        ready, why = evaluate_arm_ready(rec, cfg, now=now)
        if ready and gate_evict:
            ready, why = admit_arm_gates(rec, cfg, now=now)
        if ready:
            rec.pop("unarmable_since", None)
            rec["arm_ready"] = True
            rec["arm_ready_reason"] = "ok"
            return False
        rec["arm_ready"] = False
        rec["arm_ready_reason"] = why
    except Exception:
        rec["arm_ready"] = False
        rec["arm_ready_reason"] = code or "never_armable"
    try:
        events.append(cp.log_event(
            "watch_drop", symbol=sym, reason="never_armable",
            block=code,
            arm_ready=False,
            arm_ready_reason=str(rec.get("arm_ready_reason") or code),
            elapsed_sec=round(float(now) - float(since), 1),
            admit_pct_change=_f_or_none(rec.get("admit_pct_change")),
            admit_chg_band=str(rec.get("admit_chg_band") or "") or None,
            seat_role=str(rec.get("seat_role") or "") or None))
    except Exception:  # noqa: BLE001
        events.append({
            "kind": "watch_drop", "symbol": sym, "reason": "never_armable",
            "block": code, "arm_ready": False,
        })
    drop_watch_symbols([sym])
    return True


def _track_far_exh_seat(rec: dict, cfg: dict | None, *, now: float) -> None:
    """Start/clear ``far_exh_since`` from dual-%R seat class.

    Also stamps ``square_since`` when the seat first reaches dual OB square
    (for late-into-square vs dead-follow-through journaling).
    """
    if not isinstance(rec, dict):
        return
    cls = stamp_exh_seat_fields(rec, cfg)
    if cls == "far":
        if _f_or_none(rec.get("far_exh_since")) is None:
            rec["far_exh_since"] = float(now)
    else:
        rec.pop("far_exh_since", None)
    if cls == "square":
        if _f_or_none(rec.get("square_since")) is None:
            rec["square_since"] = float(now)
    elif cls == "os_square":
        if _f_or_none(rec.get("os_square_since")) is None:
            rec["os_square_since"] = float(now)
    elif cls in ("far", "unknown"):
        rec.pop("square_since", None)
        rec.pop("os_square_since", None)
    # pre_square / os_triangle: keep any prior square_since / os_square_since.


_DEAD_UNKNOWN_BLOCKS = frozenset({
    "stale_quote", "stale_tape", "no_quote", "no_quote_age",
    "no_rsi_data", "no_exhaustion_data", "exh_falling",
})

_PROTECTED_DEAD_CLASSES = frozenset({
    "square", "pre_square", "os_square", "os_triangle",
})


def _maybe_dead_unknown_evict(
    rec: dict,
    *,
    sym: str,
    cfg: dict,
    now: float,
    events: list,
    cp,
    gt,
) -> bool:
    """Drop seats that cannot arm and are past ``ai_watch_dead_seat_evict_sec``.

    Unknown / missing-%R seats, and any seat whose block is stale tape,
    no RSI, or falling heat. Subscribe grace (often 90s) and the morning
    flood do not keep these: they occupy a slot and the 30s dead clock
    never reached them on the far-only path. A live rising-heat seat
    without one of those blocks stays. Square / pre-square stay until a
    dead block is actually set.

    The clock is ``dead_unknown_since``, not ``block_ts``. The poll
    restamps ``block_ts`` every cycle, which would otherwise reset a
    30s timer forever.
    """
    try:
        limit = float(cfg.get("ai_watch_dead_seat_evict_sec", 30.0) or 0.0)
    except (TypeError, ValueError):
        limit = 30.0
    if limit <= 0 or not isinstance(rec, dict):
        return False
    status = str(rec.get("status") or "").lower().strip()
    if status != "watching":
        return False
    try:
        if gt is not None and gt.has_open_position(sym):
            return False
    except Exception:
        pass

    code = str(rec.get("block_code") or "").strip().lower()
    dead_block = code in _DEAD_UNKNOWN_BLOCKS
    # One price, one clock: a lagging row label beside a young live_print
    # must not start the dead-seat timer (CDNA). Re-stamp before judging.
    if dead_block and code in ("stale_quote", "stale_tape", "no_quote", "no_quote_age"):
        try:
            if promote_stream_src_if_print_fresh(rec, cfg, now=now):
                clear_tape_data_block_if_stream_fresh(rec, cfg)
                code = str(rec.get("block_code") or "").strip().lower()
                dead_block = code in _DEAD_UNKNOWN_BLOCKS
        except Exception:  # noqa: BLE001
            pass
    cls, _gap = classify_exh_seat(rec, cfg)
    ind = rec.get("indicator") if isinstance(rec.get("indicator"), dict) else {}
    fast = _f_or_none(ind.get("pctr")) if isinstance(ind, dict) else None
    slow = _f_or_none(ind.get("pctr_slow")) if isinstance(ind, dict) else None
    missing_pctr = fast is None or slow is None
    missing_unknown = cls in ("unknown", "") and missing_pctr

    if rising_heat_quality(rec, cfg) and not dead_block:
        rec.pop("dead_unknown_since", None)
        return False
    if cls in _PROTECTED_DEAD_CLASSES and not dead_block:
        rec.pop("dead_unknown_since", None)
        return False
    if not dead_block and not missing_unknown:
        rec.pop("dead_unknown_since", None)
        return False

    since = _f_or_none(rec.get("dead_unknown_since"))
    if since is None or since <= 0 or since > float(now):
        # Never-painted %R: the seat has been dead since admit, so an
        # old admit does not wait another full window.
        if missing_unknown and not dead_block:
            admitted = _f_or_none(rec.get("admit_ts"))
            since = float(admitted) if admitted and admitted > 0 else float(now)
        else:
            since = float(now)
        rec["dead_unknown_since"] = float(since)
    if (float(now) - float(since)) < limit:
        return False

    try:
        events.append(cp.log_event(
            "watch_drop", symbol=sym, reason="dead_unknown",
            block=code or ("missing_pctr" if missing_unknown else ""),
            exh_seat_class=cls,
            elapsed_sec=round(float(now) - float(since), 1),
            seat_role=str(rec.get("seat_role") or "") or None,
        ))
    except Exception:  # noqa: BLE001
        events.append({
            "kind": "watch_drop", "symbol": sym, "reason": "dead_unknown",
            "block": code, "exh_seat_class": cls,
        })
    drop_watch_symbols([sym])
    return True


def _maybe_far_exh_evict(
    rec: dict,
    *,
    sym: str,
    cfg: dict,
    now: float,
    events: list,
    cp,
    gt,
) -> bool:
    """Drop seats stuck ``far`` from dual-%R square past short grace.

    Pins are eligible (book must not freeze on HOOD/ONON-class lag). Promote
    instead of drop when the seat has become pre_square/square.

    Morning flood far seats are **stealable** (see ``_preferential_far_exh_steal``)
    but are not blind-evicted into vacuum here — flood occupancy still matters
    until a pre_square/square admittee is waiting.
    """
    if not admit_prefer_square(cfg):
        return False
    # Rising-heat far seats stay. Falling / stale / no-RSI seats do not —
    # they were filling the book with names that cannot open.
    _dead_far = str((rec or {}).get("block_code") or "").strip().lower() in (
        "exh_falling", "stale_quote", "no_rsi_data",
    )
    if exh_heating_with_square(cfg) and not _dead_far:
        # Quality heater still on the book — keep.
        if rising_heat_quality(rec, cfg):
            return False
        # Not rising-heat and not a sticky dead code: still stealable as far.
    try:
        dead_limit = float(cfg.get("ai_watch_dead_seat_evict_sec", 30.0) or 0.0)
    except (TypeError, ValueError):
        dead_limit = 30.0
    limit = far_exh_evict_sec(cfg)
    if _dead_far and dead_limit > 0:
        limit = min(limit, dead_limit) if limit > 0 else dead_limit
    if limit <= 0 or not isinstance(rec, dict):
        return False
    status = str(rec.get("status") or "").lower().strip()
    if status != "watching":
        return False
    if _within_subscribe_grace(rec, cfg, now):
        return False
    _track_far_exh_seat(rec, cfg, now=now)
    _cls = str(rec.get("exh_seat_class") or "")
    if _cls != "far" and not (
        exh_heating_with_square(cfg) and _dead_far and _cls in ("unknown", "")
    ):
        return False
    # Flood far: do not blind-drop; dedicated steal yields to pre_square/square.
    if morning_flood_active(cfg, now) and is_morning_flood_source(rec):
        return False
    since = _f_or_none(rec.get("far_exh_since"))
    if since is None or since <= 0:
        since = _f_or_none(rec.get("block_ts")) or _f_or_none(
            rec.get("unarmable_since"))
    if since is None or since <= 0:
        return False
    if (float(now) - float(since)) < limit:
        return False
    try:
        if gt is not None and gt.has_open_position(sym):
            return False
    except Exception:
        pass
    try:
        events.append(cp.log_event(
            "watch_drop", symbol=sym, reason="far_exh",
            exh_seat_class="far",
            pctr_gap=_f_or_none(rec.get("pctr_gap")),
            elapsed_sec=round(float(now) - float(since), 1),
            seat_role=str(rec.get("seat_role") or "") or None,
            arm_ready=bool(rec.get("arm_ready")) if "arm_ready" in rec else None,
        ))
    except Exception:  # noqa: BLE001
        events.append({
            "kind": "watch_drop", "symbol": sym, "reason": "far_exh",
            "exh_seat_class": "far",
        })
    drop_watch_symbols([sym])
    return True


def _candidate_young_stream_age(
    cand: dict,
    cfg: dict | None,
    *,
    now: float,
    prefer_live: bool = False,
) -> float | None:
    """Return tape age when the candidate still has young stream; else None.

    Row-stamped stream age first; ``live_print`` fallback. When
    ``prefer_live`` is set (steal-time recheck), a dated live print that is
    too old refuses; missing live_print falls back to the stamped age.
    """
    if not isinstance(cand, dict):
        return None
    sym = str(cand.get("symbol") or "").upper().strip()
    ceiling = decision_max_age_sec(cfg)

    live_age: float | None = None
    live_seen = False
    if sym:
        try:
            got = live_print(sym)
            if got is not None and got[1] is not None:
                live_seen = True
                live_age = float(got[1])
        except Exception:
            pass

    if prefer_live and live_seen:
        return live_age if live_age is not None and live_age <= ceiling else None

    src = str(
        cand.get("last_ask_src") or cand.get("price_src") or ""
    ).strip().lower()
    age_f = _f_or_none(cand.get("last_ask_age_sec"))
    if age_f is None:
        age_f = _f_or_none(cand.get("tape_age_sec"))
    if age_f is not None and float(age_f) <= ceiling:
        if src in ("stream", ""):
            return float(age_f)
    if live_age is not None and live_age <= ceiling:
        return float(live_age)
    return None


def _preferential_unarmable_steal(
    state: dict,
    *,
    cfg: dict,
    now: float,
    events: list,
    cp,
    gt,
    candidates: list | None = None,
) -> list[str]:
    """Drop worst unarmable-stale watching seats for young-stream admits.

    Fires when stream-ready seats on the book are below 2 and at least one
    inclusion-cleared young-stream candidate needs a seat. Never steals
    armed/submitted/filled or open broker positions. No reseed cool (A2).
    Returns dropped victim symbols.
    """
    if not isinstance(state, dict):
        return []
    stream_ready = 0
    for rec in state.values():
        if _is_stream_ready_seat(rec, cfg, now=now):
            stream_ready += 1
    if stream_ready >= 2:
        return []

    on_book = {
        str(rec.get("symbol") or key or "").upper().strip()
        for key, rec in state.items()
        if isinstance(rec, dict)
    }
    admittees: list[tuple[str, float, float]] = []
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        sym = str(cand.get("symbol") or "").upper().strip()
        if not sym or sym in on_book:
            continue
        age = _candidate_young_stream_age(cand, cfg, now=now)
        if age is None:
            continue
        dvol = _f_or_none(cand.get("admit_dollar_volume"))
        if dvol is None:
            dvol = _f_or_none(cand.get("dollar_volume")) or 0.0
        admittees.append((sym, float(dvol), float(age)))
    if not admittees:
        return []
    # Prefer higher $vol, younger tape among waiting admittees.
    admittees.sort(key=lambda t: (-t[1], t[2], t[0]))

    victims: list[tuple[str, dict, float, float]] = []
    for key, rec in state.items():
        if not isinstance(rec, dict):
            continue
        # Pins protected unless far dual-%R (square bus — don't freeze on lag).
        # Flood far seats are also stealable past TTL (aggressive pre-square farm).
        pin_prot = _is_protected_pin_seat(rec, cfg, now=now)
        if pin_prot:
            cls = str(rec.get("exh_seat_class") or "")
            if not cls:
                cls, _ = classify_exh_seat(rec, cfg)
            if cls != "far" or not admit_prefer_square(cfg):
                continue
        if _is_protected_warming_seat(rec, cfg, now=now):
            continue
        if not _is_unarmable_stale_watching(rec, cfg, now=now):
            continue
        sym = str(rec.get("symbol") or key or "").upper().strip()
        if not sym:
            continue
        if _within_stale_restream_grace(sym, now, cfg):
            continue
        try:
            if gt is not None and gt.has_open_position(sym):
                continue
        except Exception:
            pass
        dvol = _f_or_none(rec.get("admit_dollar_volume")) or 0.0
        age = row_quote_age_sec(rec, now=now)
        if age is None:
            age = _f_or_none(rec.get("last_ask_age_sec"))
        age_f = float(age) if age is not None else 1e9
        victims.append((sym, rec, float(dvol), age_f))
    if not victims:
        return []
    # Worst first: lowest $vol, then oldest tape (stale_tape_cap spirit).
    victims.sort(key=lambda t: (t[2], -t[3], t[0]))

    need = max(0, 2 - stream_ready)
    n = min(need, len(victims), len(admittees))
    if n <= 0:
        return []
    dropped: list[str] = []
    for i in range(n):
        sym, _rec, dvol, age_f = victims[i]
        admit_sym, _advol, admit_age = admittees[i]
        # Re-check admittee still young-stream at steal time.
        still = _candidate_young_stream_age(
            {"symbol": admit_sym, "last_ask_age_sec": admit_age,
             "last_ask_src": "stream"},
            cfg, now=now, prefer_live=True,
        )
        if still is None:
            continue
        try:
            events.append(cp.log_event(
                "watch_drop", symbol=sym, reason="unarmable_steal",
                admittee=admit_sym,
                age_sec=round(age_f, 1) if age_f < 1e8 else None,
                dollar_volume=round(dvol, 0) if dvol else None,
                stream_ready_count=stream_ready,
                admittee_age_sec=round(float(still), 1)))
        except Exception:  # noqa: BLE001
            events.append({
                "kind": "watch_drop", "symbol": sym,
                "reason": "unarmable_steal",
                "admittee": admit_sym,
                "stream_ready_count": stream_ready,
            })
        dropped.append(sym)
    if dropped:
        drop_watch_symbols(dropped)
    return dropped


def _preferential_preheat_steal(
    state: dict,
    *,
    cfg: dict,
    now: float,
    events: list,
    cp,
    gt,
    candidates: list | None = None,
) -> list[str]:
    """Steal dead/unarmable seats for warming young-stream scouts (cool-free).

    Fires when warming+stream-ready seats are below ``ai_watch_warming_seats``
    and a warming (or soft_seed) candidate has young stream. Same victim class
    as ``unarmable_steal``. Never steals armed/submitted/filled/open positions.
    """
    if not isinstance(state, dict):
        return []
    target = warming_seat_quota(cfg)
    if target <= 0:
        return []

    warming_ready = 0
    stream_ready = 0
    for rec in state.values():
        if not isinstance(rec, dict):
            continue
        if _is_stream_ready_seat(rec, cfg, now=now):
            stream_ready += 1
            if str(rec.get("seat_role") or "").lower() == "warming":
                warming_ready += 1
    # Need room toward warming quota OR stream-ready floor of 2.
    need_warm = max(0, target - warming_ready)
    need_stream = max(0, 2 - stream_ready)
    need = max(need_warm, need_stream)
    if need <= 0:
        return []

    on_book = {
        str(rec.get("symbol") or key or "").upper().strip()
        for key, rec in state.items()
        if isinstance(rec, dict)
    }
    admittees: list[tuple[str, float, float]] = []
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        sym = str(cand.get("symbol") or "").upper().strip()
        if not sym or sym in on_book:
            continue
        role = str(cand.get("seat_role") or "").lower()
        crit = {str(c).lower() for c in (cand.get("criteria") or [])}
        if role != "warming" and "warming" not in crit and "soft_seed" not in crit:
            # Funnel kept_symbols alone — treat as scout if young stream.
            if cand.get("soft_seed") is not True and role != "warming":
                # Still allow plain funnel symbols when warming seats are short.
                if need_warm <= 0:
                    continue
        age = _candidate_young_stream_age(cand, cfg, now=now)
        if age is None:
            continue
        dvol = _f_or_none(cand.get("admit_dollar_volume"))
        if dvol is None:
            dvol = _f_or_none(cand.get("dollar_volume")) or 0.0
        admittees.append((sym, float(dvol), float(age)))
    if not admittees:
        return []
    admittees.sort(key=lambda t: (-t[1], t[2], t[0]))

    victims: list[tuple[str, dict, float, float]] = []
    for key, rec in state.items():
        if not isinstance(rec, dict):
            continue
        if _is_protected_pin_seat(rec, cfg, now=now):
            continue
        if _is_protected_warming_seat(rec, cfg, now=now):
            continue
        if not _is_unarmable_stale_watching(rec, cfg, now=now):
            continue
        sym = str(rec.get("symbol") or key or "").upper().strip()
        if not sym:
            continue
        if _within_stale_restream_grace(sym, now, cfg):
            continue
        try:
            if gt is not None and gt.has_open_position(sym):
                continue
        except Exception:
            pass
        dvol = _f_or_none(rec.get("admit_dollar_volume")) or 0.0
        age = row_quote_age_sec(rec, now=now)
        if age is None:
            age = _f_or_none(rec.get("last_ask_age_sec"))
        age_f = float(age) if age is not None else 1e9
        victims.append((sym, rec, float(dvol), age_f))
    if not victims:
        return []
    victims.sort(key=lambda t: (t[2], -t[3], t[0]))

    n = min(need, len(victims), len(admittees))
    if n <= 0:
        return []
    dropped: list[str] = []
    for i in range(n):
        sym, _rec, dvol, age_f = victims[i]
        admit_sym, _advol, admit_age = admittees[i]
        still = _candidate_young_stream_age(
            {"symbol": admit_sym, "last_ask_age_sec": admit_age,
             "last_ask_src": "stream"},
            cfg, now=now, prefer_live=True,
        )
        if still is None:
            continue
        try:
            events.append(cp.log_event(
                "watch_drop", symbol=sym, reason="preheat_steal",
                admittee=admit_sym,
                age_sec=round(age_f, 1) if age_f < 1e8 else None,
                dollar_volume=round(dvol, 0) if dvol else None,
                warming_ready=warming_ready,
                warming_target=target,
                admittee_age_sec=round(float(still), 1)))
        except Exception:  # noqa: BLE001
            events.append({
                "kind": "watch_drop", "symbol": sym,
                "reason": "preheat_steal",
                "admittee": admit_sym,
            })
        dropped.append(sym)
    if dropped:
        drop_watch_symbols(dropped)
    return dropped


def _preferential_far_exh_steal(
    state: dict,
    *,
    cfg: dict,
    now: float,
    events: list,
    cp,
    gt,
    candidates: list | None = None,
) -> list[str]:
    """Steal far dual-%R seats for waiting ``pre_square`` / ``square`` admittees.

    Aggressive pre-square farm: far (including flood/pin past TTL) yields as
    soon as a true approach/OB candidate is waiting. Distinct drop reasons:
    ``far_exh_steal_for_pre_square`` / ``far_exh_steal_for_square``.
    """
    if not isinstance(state, dict) or not admit_prefer_square(cfg):
        return []
    limit = far_exh_evict_sec(cfg)
    if limit <= 0:
        return []

    on_book = {
        str(rec.get("symbol") or key or "").upper().strip()
        for key, rec in state.items()
        if isinstance(rec, dict)
    }
    admittees: list[tuple[str, str, float, float]] = []
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        sym = str(cand.get("symbol") or "").upper().strip()
        if not sym or sym in on_book:
            continue
        cls = str(cand.get("exh_seat_class") or "").strip().lower()
        if not cls:
            cls, _ = classify_exh_seat(cand, cfg)
        if cls not in ("pre_square", "square", "os_square", "os_triangle"):
            continue
        age = _candidate_young_stream_age(cand, cfg, now=now)
        # Class-ranked hunt may lack a live stream stamp yet — still allow
        # steal so soft-seed can claim the seat next cycle.
        age_f = float(age) if age is not None else 9e8
        dvol = _f_or_none(cand.get("admit_dollar_volume"))
        if dvol is None:
            dvol = _f_or_none(cand.get("dollar_volume")) or 0.0
        admittees.append((sym, cls, float(dvol), age_f))
    if not admittees:
        return []
    # square / os_triangle before pre_square / os_square, then $vol, then younger tape.
    rank = {"square": 0, "os_triangle": 0, "pre_square": 1, "os_square": 1}
    admittees.sort(key=lambda t: (rank.get(t[1], 9), -t[2], t[3], t[0]))

    victims: list[tuple[str, dict, float, float]] = []
    for key, rec in state.items():
        if not isinstance(rec, dict):
            continue
        status = str(rec.get("status") or "").lower().strip()
        if status != "watching":
            continue
        if _within_subscribe_grace(rec, cfg, now):
            continue
        cls = str(rec.get("exh_seat_class") or "")
        if not cls:
            cls, _ = classify_exh_seat(rec, cfg)
        if cls != "far":
            continue
        since = _f_or_none(rec.get("far_exh_since"))
        if since is None or (float(now) - float(since)) < limit:
            # Flood far: mark stealable immediately (TTL 0-hold for farm).
            flood_far = (
                morning_flood_active(cfg, now)
                and is_morning_flood_source(rec)
            )
            if not flood_far:
                continue
            if since is None:
                rec["far_exh_since"] = float(now)
                since = float(now)
            # Flood far steals after half TTL (or immediately if TTL tiny).
            if (float(now) - float(since)) < min(limit, 15.0):
                continue
        sym = str(rec.get("symbol") or key or "").upper().strip()
        if not sym:
            continue
        if _within_stale_restream_grace(sym, now, cfg):
            continue
        try:
            if gt is not None and gt.has_open_position(sym):
                continue
        except Exception:
            pass
        # Warming pre_square seats are never victims here.
        if str(rec.get("seat_role") or "").lower() == "warming":
            wcls = str(rec.get("exh_seat_class") or "")
            if wcls in ("pre_square", "square", "os_square", "os_triangle"):
                continue
        dvol = _f_or_none(rec.get("admit_dollar_volume")) or 0.0
        age = row_quote_age_sec(rec, now=now)
        if age is None:
            age = _f_or_none(rec.get("last_ask_age_sec"))
        age_f = float(age) if age is not None else 1e9
        victims.append((sym, rec, float(dvol), age_f))
    if not victims:
        return []
    victims.sort(key=lambda t: (t[2], -t[3], t[0]))

    n = min(len(victims), len(admittees))
    if n <= 0:
        return []
    dropped: list[str] = []
    for i in range(n):
        sym, rec, dvol, age_f = victims[i]
        admit_sym, admit_cls, _advol, _aage = admittees[i]
        reason = (
            "far_exh_steal_for_square"
            if admit_cls in ("square", "os_triangle")
            else "far_exh_steal_for_pre_square"
        )
        try:
            events.append(cp.log_event(
                "watch_drop", symbol=sym, reason=reason,
                admittee=admit_sym,
                admittee_class=admit_cls,
                exh_seat_class="far",
                exh_seat_class_at_steal="far",
                age_sec=round(age_f, 1) if age_f < 1e8 else None,
                dollar_volume=round(dvol, 0) if dvol else None,
                morning_flood=bool(rec.get("morning_flood")),
                seat_role=str(rec.get("seat_role") or "") or None,
            ))
        except Exception:  # noqa: BLE001
            events.append({
                "kind": "watch_drop", "symbol": sym, "reason": reason,
                "admittee": admit_sym, "admittee_class": admit_cls,
                "exh_seat_class": "far",
            })
        dropped.append(sym)
    if dropped:
        drop_watch_symbols(dropped)
    return dropped


def _poller_blocked(rec: dict) -> bool:
    """True when the last poll recorded a real reason it would not buy.

    READY must reflect the poller's own verdict, not just price-vs-zone. Two
    ways they diverge: the stream pre-filter skips the REST quote and leaves
    last_ask stale (so a stale in-zone ask would read READY while the tape is
    far away), and portfolio gates like daily_loss_limit block a name whose
    price genuinely is in the zone. Showing READY for either is the same class
    of lie as the zone-pad mismatch this file already had.

    ``below_zone`` is *not* a hard poller block when the live print still sits
    in the armable overshoot window — that geometry is a buy, same as in-zone.
    """
    if _row_tape_stale(rec):
        return True
    code = str(rec.get("block_code") or "").strip().lower()
    # Poller-stamped stream_required stays a real veto until the decision
    # print is stream again. Do not re-read live bot_config here — that
    # would make unit tests (and book paint) depend on whatever the desk
    # file currently says. Enforcement lives in the poll/pre-place path.
    if code == "stream_required":
        src = str(
            rec.get("last_ask_src") or rec.get("price_src") or ""
        ).strip().lower()
        if not price_src_fresh(src):
            return True
    # Tape is fresh (check above). A leftover data-condition refuse is not a
    # real poller veto — same rule as derive_blocker fall-through.
    if not code or code in (
        "in_zone", "placing", "at_last",
        "stale_quote", "no_quote_age", "no_quote",
        "stream_required",
    ):
        return False
    if code.startswith("last_") or code.startswith("zone_"):
        return False
    if code == "below_zone":
        # Stale below stamp while price is still an armable dip → not blocked.
        try:
            structure = rec.get("structure") if isinstance(
                rec.get("structure"), dict) else {}
            lo = float(structure.get("entry_low") or rec.get("entry_low") or 0)
            hi = float(structure.get("entry_high") or rec.get("entry_high") or 0)
            ask = float(rec.get("last_ask") or 0)
            stop = float(structure.get("stop_price") or 0) or None
        except (TypeError, ValueError):
            return True
        if lo > 0 and hi > 0 and ask > 0 and ask_triggers_zone(
            ask, lo, hi, stop=stop, max_below_r=DEFAULT_ARM_BELOW_MAX_R,
            arm_below=True,
        ):
            return False
        return True
    return True


def derive_blocker(
    rec: dict,
    *,
    pad_pct: float = 0.0,
    max_below_r: float = DEFAULT_ARM_BELOW_MAX_R,
    arm_below: bool = True,
) -> tuple[str | None, str | None]:
    """Return (code, label) for why this watch is not an open buy.

    Prefers the last poll decision; falls back to live last_ask vs zone.
    Armable pullback overshoots (within ``max_below_r`` of the floor) report
    as ``in_zone`` so the book column matches the arm gate.
    """
    if not isinstance(rec, dict):
        return None, None
    status = str(rec.get("status") or "").lower().strip()
    if status in ("submitted", "filled", "armed"):
        if status == "armed":
            return "placing", format_blocker("placing")
        if status == "submitted":
            return "submitted", "sent"
        return "filled", "filled"

    if _row_tape_stale(rec):
        # Two different faults have been sharing one word. "stale quote" is a
        # print we can see is old — a quiet tape, which is normal on a thin
        # name. "no quote age" is a print we cannot TIME at all, which is
        # plumbing: decision_price returns an age on demand while the record
        # carries None, and an untimed price does not trip the staleness
        # guard, it disables it. The operator cannot act on the second while
        # it is wearing the first one's label.
        #
        # Display only. Both refuse identically; this names which is which.
        # Asked via row_quote_age_sec so the label matches the guard above:
        # reading the raw field here reported "no quote age" on 11 rows that
        # had provable ages of 1.5s-462s, which is the opposite of the
        # distinction this branch exists to draw.
        if row_quote_age_sec(rec) is None and str(
                rec.get("last_ask_src") or "").strip().lower() not in (
                "", "none", "stale_tape"):
            return "no_quote_age", format_blocker("no_quote_age")
        return "stale_quote", format_blocker("stale_quote")

    stored = rec.get("block_code") or rec.get("block_reason")
    if stored:
        code = str(rec.get("block_code") or stored).strip().lower()
        # Poller-stamped stream_required: keep it visible while the print is
        # still rest/stale/none. Once last_ask_src is stream, fall through so
        # the column can recompute (same pattern as stale_quote recovery).
        if code == "stream_required":
            src = str(
                rec.get("last_ask_src") or rec.get("price_src") or ""
            ).strip().lower()
            if src != "stream":
                return code, format_blocker(code)
        # Poller may have stamped below_zone before the print recovered into
        # the armable dip window; re-evaluate geometry so the column is not
        # stuck on a stale below while price is still a valid buy.
        #
        # Same for tape-data refuses: stale_quote / no_quote_age must clear
        # once _row_tape_stale is false. Keeping them in `stored` locked
        # stream+young-age rows on "stale quote" after the 60s ceiling
        # recovered the print (2026-09-01). stream_required clears the same
        # way once last_ask_src is stream again.
        if code in (
            "below_zone", "above_zone", "in_zone",
            "stale_quote", "no_quote_age", "no_quote",
            "stream_required", "await_stream",
        ):
            # Persist the clear on stream+age≤ceiling so file/UI cannot keep
            # sticky stale_quote beside a young stream stamp.
            clear_tape_data_block_if_stream_fresh(rec)
            pass  # fall through to live geometry / arm checks below
        else:
            label = str(rec.get("block_reason") or format_blocker(code) or code)
            return code, label

    structure = rec.get("structure") if isinstance(rec.get("structure"), dict) else {}
    wk = str(structure.get("wait_kind") or "").lower().strip()
    if wk == "hard_no":
        return "hard_no", format_blocker("hard_no")
    if wk == "wait_setup":
        return "wait_setup", format_blocker("wait_setup")

    try:
        lo = float(structure.get("entry_low") or rec.get("entry_low") or 0)
        hi = float(structure.get("entry_high") or rec.get("entry_high") or 0)
        ask = float(rec.get("last_ask") or 0)
    except (TypeError, ValueError):
        lo = hi = ask = 0.0
    if lo <= 0 or hi <= 0:
        return "no_structure", format_blocker("no_structure")
    if ask <= 0:
        return "no_structure", "no quote"
    try:
        stop = float(structure.get("stop_price") or 0) or None
    except (TypeError, ValueError):
        stop = None
    if ask_triggers_zone(
        ask, lo, hi,
        pad_pct=pad_pct,
        stop=stop,
        max_below_r=max_below_r,
        arm_below=arm_below,
    ):
        return "in_zone", format_blocker("in_zone")
    frac = max(0.0, float(pad_pct or 0)) / 100.0
    high_bound = max(lo, hi) * (1.0 + frac)
    if ask > high_bound:
        return "above_zone", format_blocker("above_zone")
    return "below_zone", format_blocker("below_zone")


_GEOMETRY_BLOCK_CODES = frozenset({
    "in_zone", "above_zone", "below_zone", "placing", "in_zone_fade_ok", "",
})
# Poller stamps should_arm_buy does not know about. Keep these.
# Do NOT keep stale arm vetoes (thin_rvol, heating_too_low) after knobs change.
# Do NOT keep stale_quote: it is a data condition that must clear when the
# tape is fresh again. Keeping it sticky locked every row on 2026-08-25.
_POST_ARM_BLOCK_CODES = frozenset({
    "dead_reentry", "loser_reentry", "reentry_cooldown",
    "buy_cap", "max_positions", "not_trading_hours",
    "already_held", "already_holding", "already_managed",
    "wash_trade", "wash_cooldown",
    "daily_loss_limit", "open_risk_cap",
    "trader_not_ready", "no_equity", "no_buying_power",
    "placing",
})


def _row_arm_refuse(row: dict, px: float) -> str | None:
    """Live should_arm_buy why, reconstructed from a book row. None = would arm."""
    rec = {
        "symbol": str(row.get("symbol") or "").upper().strip(),
        "status": "watching",
        "source": row.get("source") or "momentum",
        "look_reason": row.get("look_reason") or row.get("admit_look_reason"),
        "admit_look_reason": row.get("admit_look_reason") or row.get("look_reason"),
        "rvol": row.get("rvol"),
        "admit_rvol": row.get("rvol") if row.get("rvol") is not None
        else row.get("admit_rvol"),
        "structure": {
            "decision": "WAIT",
            "wait_kind": "wait_for_zone",
            "entry_low": row.get("entry_low"),
            "entry_high": row.get("entry_high"),
            "stop_price": row.get("stop_price"),
            "target_1": row.get("target_1") or (
                float(row.get("entry_high") or 0) * 1.06 or 1.0
            ),
            "reward_risk": row.get("reward_risk") or 0.6,
            "zone_kind": row.get("zone_kind") or "pullback_band",
            "synthetic": str(row.get("zone_kind") or "").lower()
            in ("pullback_band", "offset"),
        },
    }
    # Row rvol only. _desk_rvol GETs /api/state, and overlay_ai_book_live_prices
    # runs this on the /api/state path — a self-fetch that waited on the
    # snapshot it was building (48s, then desk logins timed out behind it).
    pctr = _f_or_none(row.get("pctr"))
    if pctr is None:
        exh = _f_or_none(row.get("exhaustion"))
        if exh is not None:
            pctr = exh - 100.0
    src = str(row.get("pctr_src") or "").lower()
    state = str(row.get("exhaustion_state") or "").lower()
    # CM RSI-2 travels separately from %R and must be carried either way: it
    # is an independent gate, so a row with no usable %R can still have a
    # perfectly good RSI, and vice versa.
    #
    # Leaving it out is not a missing nicety, it is a wrong answer. This
    # reconstruction is what paints the State column, and cm_rsi_allows_buy
    # reads indicator["cm_rsi"] — so every row rendered as "no rsi data"
    # regardless of what the book actually held, masking the real refusals
    # (rsi_extended, rsi_not_rising, rsi_not_realtime_*) behind a reason that
    # was never true. Observed on the whole book at 11:52 on 2026-08-20.
    # Copy every indicator field the wire carries, by prefix, rather than
    # listing them. Hand-listing has now failed twice in one session: the RSI
    # fields were missing, so every row read "no rsi data"; then pctr_src was
    # missing, so every row read "pctr not live missing" while the records
    # actually held live / clock_range and were refusing for real reasons
    # (heating_too_low, rsi_extended). Both times the State column reported a
    # cause that was never true and hid the one that was.
    #
    # The wire uses the same names as the indicator dict — _exhaustion_wire_fields
    # copies them straight across — so a prefix sweep keeps this in step with
    # any gate added later, which a literal list cannot.
    rsi_fields = {
        k: row.get(k) for k in row
        if k.startswith(("cm_rsi", "pctr_", "macd_", "macd")) and k not in (
            "pctr_rising", "pctr_falling")
    }
    if isinstance(row.get("indicator"), dict):
        for ik, iv in row["indicator"].items():
            if ik not in rsi_fields or rsi_fields[ik] is None:
                rsi_fields[ik] = iv
    if src == "thin" or (pctr is None and state in ("", "unknown")):
        rec["indicator"] = dict(rsi_fields)
    else:
        rec["indicator"] = {
            "pctr": pctr,
            "pctr_rising": state in ("heating", "overbought")
            or bool(row.get("pctr_rising")),
            "pctr_falling": state == "cooling" or bool(row.get("pctr_falling")),
            **rsi_fields,
        }
    _MID_RISE_PEEK.on = True          # display only: never advance the latch
    try:
        ok, why = should_arm_buy(rec, ask=float(px), bid=None, cfg=_push_cfg())
    except Exception:
        return None
    finally:
        _MID_RISE_PEEK.on = False
    if ok:
        return None
    return str(why or "").strip() or "blocked"


def apply_tape_blocker(row: dict, px: float | None) -> None:
    """Stamp blocker from the live print without hiding a real refuse.

    Price above the band → above zone. Price under the printed band →
    below zone (do not call a dip "in zone"). In-band keeps heat / rvol /
    cheap-OB / no-%R / loser stamps, or computes should_arm_buy if the
    poller only left a geometry code. That is why ONDS/RUM/UMAC/SORA
    painted READY while nothing bought.

    Arm-at-last: last is the entry. Above/below the printed band is not a
    refuse — only a real post-arm or should_arm_buy veto is.
    """
    if not isinstance(row, dict):
        return
    try:
        lo = float(row.get("entry_low") or 0)
        hi = float(row.get("entry_high") or 0)
        last = float(px or 0)
    except (TypeError, ValueError):
        return
    if lo <= 0 or hi <= 0 or last <= 0:
        return
    # Paint authority: if overlay/eng just stamped src=stream with a young
    # field age, that age wins over a lagging last_ask_ts / _LAST_QUOTE_TS
    # (Class C / APLD-SMCI). honesty_restamp still demotes when only a
    # frozen field remains beside a truly old row ts.
    promote_stream_src_if_print_fresh(row)
    _paint_trust_young_stream_field(row)
    if _row_tape_stale(row):
        row["ready"] = False
        row["block_code"] = "stale_quote"
        row["blocker"] = format_blocker("stale_quote")
        row["block_reason"] = row["blocker"]
        if not row.get("block_detail"):
            row["block_detail"] = "tape age unknown or old"
        # PPBT Sep2 honesty: never leave last_ask_src=stream beside
        # block_code=stale_quote (age/src already said the print is dead).
        honesty_restamp_stream_src(row)
        return
    # Stream+fresh: drop sticky tape-data refuse before geometry recompute.
    clear_tape_data_block_if_stream_fresh(row)
    # Keep a poller-stamped stream_required refuse until the print is stream.
    # Do not re-derive the flag from live bot_config here (tests / paint).
    _src_now = str(
        row.get("last_ask_src") or row.get("price_src") or ""
    ).strip().lower()
    if (str(row.get("block_code") or "").strip().lower() == "stream_required"
            and _src_now != "stream"):
        row["ready"] = False
        row["block_code"] = "stream_required"
        row["blocker"] = format_blocker("stream_required")
        row["block_reason"] = row["blocker"]
        if not row.get("block_detail"):
            row["block_detail"] = _src_now or "not_stream"
        return
    stored = str(row.get("block_code") or "").strip()
    keep = stored in _POST_ARM_BLOCK_CODES

    def _keep_stored() -> None:
        row["ready"] = False
        row["block_code"] = stored
        row["blocker"] = (
            row.get("blocker") or row.get("block_reason")
            or format_blocker(stored)
        )
        row["block_reason"] = row.get("blocker")

    # Capital/session refuses stay visible even when last is above the band.
    # DUOT 08-18: tape in-zone painted "buy" while the poller had already
    # dead-reentry'd, then stamped above_zone off a stale REST ask.
    if keep and not arm_at_last(_push_cfg()):
        if last > max(lo, hi):
            row["in_zone"] = False
        elif ask_in_zone(last, lo, hi, 0.0):
            row["in_zone"] = True
        else:
            row["in_zone"] = False
        _keep_stored()
        return

    if arm_at_last(_push_cfg()):
        if keep:
            _keep_stored()
            return
        why = _row_arm_refuse(row, last)
        if why and why not in (
            "above_zone", "below_zone", "zone",
        ) and not str(why).startswith("last_") and not str(why).startswith("zone_"):
            row["in_zone"] = True
            row["ready"] = False
            row["block_code"] = why
            row["blocker"] = format_blocker(why) or why.replace("_", " ")
            row["block_reason"] = row["blocker"]
            return
        row["in_zone"] = True
        row["block_code"] = "in_zone"
        row["blocker"] = format_blocker("in_zone")
        row["block_reason"] = row["blocker"]
        row["ready"] = True
        return

    if last > max(lo, hi):
        row["in_zone"] = False
        row["ready"] = False
        row["block_code"] = "above_zone"
        row["blocker"] = format_blocker("above_zone")
        row["block_reason"] = row["blocker"]
        return

    if ask_in_zone(last, lo, hi, 0.0):
        row["in_zone"] = True
        if keep:
            _keep_stored()
            return
        why = _row_arm_refuse(row, last)
        if why and why not in ("above_zone", "below_zone", "zone"):
            row["ready"] = False
            row["block_code"] = why
            row["blocker"] = format_blocker(why) or why.replace("_", " ")
            row["block_reason"] = row["blocker"]
            return
        row["block_code"] = "in_zone"
        row["blocker"] = format_blocker("in_zone")
        row["block_reason"] = row["blocker"]
        row["ready"] = True
        return

    row["in_zone"] = False
    row["ready"] = False
    row["block_code"] = "below_zone"
    row["blocker"] = format_blocker("below_zone")
    row["block_reason"] = row["blocker"]


def load_watch() -> dict[str, dict]:
    """Load symbol -> watch record; empty dict if missing/corrupt."""
    path = WATCH_STATE_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for key, val in raw.items():
        if not isinstance(val, dict):
            continue
        sym = str(key or val.get("symbol") or "").upper().strip()
        if not sym:
            continue
        rec = dict(val)
        rec["symbol"] = sym
        out[sym] = rec
    return out


def save_watch(state: dict) -> None:
    """Atomic write so a crash mid-write does not corrupt the watch file."""
    path = WATCH_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state if isinstance(state, dict) else {}
    tmp = path.with_suffix(path.suffix + ".tmp")
    if path.suffix == ".json":
        tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def merge_watch_records(records: dict[str, dict]) -> dict[str, dict]:
    """Re-read the book and write back only *records*, leaving the rest alone.

    poll_once may spend many seconds between its load and its save (quotes per
    symbol, an LLM structure call, an order placement). Blind-writing the dict
    it loaded at the start would clobber every symbol the 2s sync added or
    dropped in the meantime. Merging per-record keeps both writers' work.
    """
    if not isinstance(records, dict) or not records:
        return load_watch()
    with _WATCH_LOCK:
        state = load_watch()
        for sym, rec in records.items():
            if not isinstance(rec, dict):
                continue
            key = str(sym or rec.get("symbol") or "").upper().strip()
            if not key:
                continue
            merged = dict(rec)
            merged["symbol"] = key
            state[key] = merged
        save_watch(state)
        return state


def drop_watch_symbols(symbols) -> dict[str, dict]:
    """Remove *symbols* from the book unless a paper order is in flight.

    Dead-today losers must not occupy a slot or a quote. Submitted / filled
    rows stay so an open ticket is still managed.
    """
    wanted = {
        str(s or "").upper().strip()
        for s in (symbols or [])
        if str(s or "").strip()
    }
    if not wanted:
        return load_watch()
    with _WATCH_LOCK:
        state = load_watch()
        changed = False
        for key in wanted:
            rec = state.get(key)
            if not isinstance(rec, dict):
                continue
            status = str(rec.get("status") or "").lower().strip()
            if status in ("submitted", "filled"):
                continue
            state.pop(key, None)
            changed = True
        if changed:
            save_watch(state)
        return state


def public_snapshot(state: dict | None = None) -> list[dict]:
    """Operator-facing watch queue rows for positions JSON.

    Each item: symbol, status, wait_kind, entry_low, entry_high, last_ask,
    score, agreement, reason, source, ready. Open queue only (watching/armed);
    terminal statuses are omitted. Ready = armed, or watching with ask in zone.
    Sorted: ready first, then score desc, then symbol.
    """
    if state is None:
        state = load_watch()
    if not isinstance(state, dict):
        return []
    # Exact zone (pad=0) for UI "ready" — matches default ai_entry_zone_pad_pct.
    # Avoid load_config here (snapshot is hot-path and must stay import-light).
    pad_pct = 0.0
    rows: list[dict] = []
    for key, rec in state.items():
        if not isinstance(rec, dict):
            continue
        sym = str(rec.get("symbol") or key or "").upper().strip()
        if not sym:
            continue
        status = str(rec.get("status") or "").lower().strip() or "watching"
        if status in _TERMINAL_STATUSES:
            continue
        if status and status not in _ARMABLE_STATUSES:
            continue
        block = str(rec.get("block_code") or "").lower().strip()
        if block in ("dead_reentry", "loser_reentry"):
            continue
        structure = rec.get("structure")
        if not isinstance(structure, dict):
            structure = {}
        wait_kind = structure.get("wait_kind")
        if wait_kind is not None:
            wait_kind = str(wait_kind).lower().strip() or None
        entry_low = structure.get("entry_low")
        entry_high = structure.get("entry_high")
        # Prefer nested structure levels; fall back to top-level if present.
        if entry_low is None:
            entry_low = rec.get("entry_low")
        if entry_high is None:
            entry_high = rec.get("entry_high")
        try:
            entry_low_f = float(entry_low) if entry_low is not None else None
        except (TypeError, ValueError):
            entry_low_f = None
        try:
            entry_high_f = float(entry_high) if entry_high is not None else None
        except (TypeError, ValueError):
            entry_high_f = None
        # Align decision last_ask with live_print (young engine rt_* wins
        # when ≤ ceiling; else freshest dated dash) before last_ask is read /
        # honesty restamp. Fixes GTLB-class desk-vs-engine lag and avoids
        # leaving stale_tape when only adj engine age crossed the ceiling
        # while dash still has a young dated print.
        try:
            _lp = live_print(sym)
            if (
                _lp is not None
                and _lp[0]
                and _lp[1] is not None
                and float(_lp[0]) > 0
                and float(_lp[1]) <= decision_max_age_sec(None)
            ):
                _epx, _eage = float(_lp[0]), float(_lp[1])
                _now_al = time.time()
                rec["last_ask"] = _epx
                rec["last_ask_src"] = "stream"
                rec["price_src"] = "stream"
                rec["last_ask_age_sec"] = _eage
                rec["last_ask_ts"] = _now_al - _eage
                _set_quote_ts(sym, _now_al - _eage)
                # Drop sticky stale_quote once paint has a young stream print.
                clear_tape_data_block_if_stream_fresh(rec)
        except Exception:
            pass
        last_ask = rec.get("last_ask")
        try:
            last_ask_f = float(last_ask) if last_ask is not None else None
        except (TypeError, ValueError):
            last_ask_f = None
        score = rec.get("score")
        try:
            score_f = float(score) if score is not None else None
        except (TypeError, ValueError):
            score_f = None
        try:
            stop_f = float(structure.get("stop_price") or 0) or None
        except (TypeError, ValueError):
            stop_f = None
        in_zone = False
        if (
            last_ask_f is not None
            and entry_low_f is not None
            and entry_high_f is not None
            and entry_low_f > 0
            and entry_high_f > 0
        ):
            # Include armable below-zone dips (same geometry as should_arm_buy).
            in_zone = ask_triggers_zone(
                last_ask_f, entry_low_f, entry_high_f,
                pad_pct=pad_pct,
                stop=stop_f,
                max_below_r=DEFAULT_ARM_BELOW_MAX_R,
                arm_below=True,
            )
            if not in_zone and arm_at_last(_push_cfg()) and last_ask_f > 0:
                in_zone = True
        ready = status == "armed" or (
            status == "watching" and in_zone and not _poller_blocked(rec))
        # Restamp BEFORE derive_blocker so chip/src/block_code agree:
        # age>15 must never publish last_ask_src=stream (Sep2 11:05 ET).
        honesty_restamp_stream_src(rec)
        b_code, b_label = derive_blocker(rec, pad_pct=pad_pct)
        rows.append({
            "symbol": sym,
            "status": status or None,
            "wait_kind": wait_kind,
            "entry_low": entry_low_f,
            "entry_high": entry_high_f,
            "stop_price": stop_f,
            "last_ask": last_ask_f,
            "last_ask_src": rec.get("last_ask_src"),
            # Recomputed from the quote's own timestamp, never republished as
            # measured. A record rebuilt but not yet re-priced still reports a
            # correct (growing) age instead of None, which is what made 5 of
            # 11 rows read "no quote age" at 12:23 ET while the arm gate was
            # seeing real ages the whole time.
            "last_ask_age_sec": _f_or_none(row_quote_age_sec(rec)),
            # The age the tape-staleness guard actually reads, published under
            # the name the rest of the desk uses for it. Without this the book
            # legend's FRESH row could never be evaluated at all — it went
            # permanently blank, which reads as "unknown" and was in fact
            # "never asked". Sixth field to travel this chain.
            "price_age_sec": _f_or_none(
                rec.get("price_age_sec")
                if rec.get("price_age_sec") is not None
                else rec.get("last_ask_age_sec")),
            "score": score_f,
            # RVOL as measured at admission. The score is a blend whose scale
            # differs per source (momentum ~1000, Stocktwits ~10-20), so it is
            # not comparable down the column; rvol is one unit everywhere.
            "rvol": _f_or_none(rec.get("admit_rvol")),
            # Did a human call this one out? The seeder tags `bro_call` onto
            # rows another source owns (a call is the weakest evidence and
            # must not seize the row), so `source` alone cannot answer it —
            # DAIC on 8/26 was called by Trader Bro and reached the book as
            # `momentum` with nothing recording the call. Shipped as its own
            # boolean rather than the whole criteria list: the panel wants a
            # badge, and criteria is a producer-side vocabulary.
            "bro_call": bool(
                "bro_call" in (rec.get("admit_criteria") or [])
                or str(rec.get("source") or "").strip().lower()
                in _BB_LIVE_SOURCES),
            # % of the way to overbought (100 + fast %R) and which way it is
            # moving. Both, because the level alone cannot tell "pinned at the
            # highs and rolling over" from "climbing into them".
            "exhaustion": _f_or_none(exhaustion_pct(rec)),
            "exhaustion_state": exhaustion_state(rec, _push_cfg()),
            **_exhaustion_wire_fields(rec),
            **_macd_wire_fields(rec),
            **_rsi_wire_fields(rec),
            "agreement": bool(rec.get("agreement")) if rec.get("agreement") is not None else None,
            "reason": str(rec.get("reason") or "")[:80] or None,
            "source": str(rec.get("source") or "research")[:24] or "research",
            "ready": bool(ready),
            "in_zone": bool(in_zone),
            # Which geometry drew this band. A double-bottom zone is anchored to
            # a real shelf; an offset zone is a percentage guess off the last
            # print with a 5% stop. They were indistinguishable on the wire.
            "zone_kind": str(structure.get("zone_kind") or "") or None,
            "block_code": b_code,
            "blocker": b_label,
            "block_reason": b_label,
            "block_detail": rec.get("block_detail"),
            # Same ceiling the arm gate uses, so the UI cannot say stale at 8s
            # while the poller still buys at 30s.
            "decision_max_age_sec": decision_max_age_sec(_push_cfg()),
            # Live day % (desk first, admit stamp fallback). Book UI paints
            # this next to Last; without it only Momentum dual-lists colored.
            "pct_change": _wire_pct_change(sym, rec),
            "admit_pct_change": _f_or_none(rec.get("admit_pct_change")),
        })
    # Ready first, then higher score, then symbol for stable UI.
    rows.sort(key=lambda r: (
        0 if r.get("ready") else 1,
        -(r.get("score") or 0.0),
        r["symbol"],
    ))
    return rows


def _watch_row_from_record(sym: str, rec: dict, *, pad_pct: float = 0.0) -> dict:
    """Normalize one watch-state record for the book table."""
    structure = rec.get("structure") if isinstance(rec.get("structure"), dict) else {}
    wait_kind = structure.get("wait_kind")
    if wait_kind is not None:
        wait_kind = str(wait_kind).lower().strip() or None
    entry_low = structure.get("entry_low", rec.get("entry_low"))
    entry_high = structure.get("entry_high", rec.get("entry_high"))
    try:
        entry_low_f = float(entry_low) if entry_low is not None else None
    except (TypeError, ValueError):
        entry_low_f = None
    try:
        entry_high_f = float(entry_high) if entry_high is not None else None
    except (TypeError, ValueError):
        entry_high_f = None
    last_ask = rec.get("last_ask")
    try:
        last_ask_f = float(last_ask) if last_ask is not None else None
    except (TypeError, ValueError):
        last_ask_f = None
    try:
        score_f = float(rec["score"]) if rec.get("score") is not None else None
    except (TypeError, ValueError, KeyError):
        score_f = None
    status = str(rec.get("status") or "watching").lower().strip() or "watching"
    try:
        stop_f = float(structure.get("stop_price") or 0) or None
    except (TypeError, ValueError):
        stop_f = None
    in_zone = False
    if (
        last_ask_f is not None
        and entry_low_f is not None
        and entry_high_f is not None
        and entry_low_f > 0
        and entry_high_f > 0
    ):
        in_zone = ask_triggers_zone(
            last_ask_f, entry_low_f, entry_high_f,
            pad_pct=pad_pct,
            stop=stop_f,
            max_below_r=DEFAULT_ARM_BELOW_MAX_R,
            arm_below=True,
        )
        if not in_zone and arm_at_last(_push_cfg()) and last_ask_f > 0:
            in_zone = True
    ready = status == "armed" or (
        status == "watching" and in_zone and not _poller_blocked(rec))
    if status == "armed" or ready:
        phase = "ready"
    elif status == "submitted":
        phase = "submitted"
    elif status == "filled":
        phase = "filled"  # upgraded to open if broker position present
    else:
        phase = "watching"
    src = str(rec.get("source") or "research").strip() or "research"
    b_code, b_label = derive_blocker(rec, pad_pct=pad_pct)
    return {
        "symbol": sym,
        "phase": phase,
        "status": status,
        "ready": bool(ready),
        "in_zone": bool(in_zone),
        "source": src,
        "score": score_f,
        "rvol": _f_or_none(rec.get("admit_rvol")),
        "exhaustion": _f_or_none(exhaustion_pct(rec)),
        "exhaustion_state": exhaustion_state(rec, _push_cfg()),
        "exh_seat_class": (
            str(rec.get("exh_seat_class") or "").strip().lower() or None
        ),
        **_exhaustion_wire_fields(rec),
        **_macd_wire_fields(rec),
        **_rsi_wire_fields(rec),
        "reason": str(rec.get("reason") or "")[:80] or None,
        "wait_kind": wait_kind,
        "entry_low": entry_low_f,
        "entry_high": entry_high_f,
        "stop_price": stop_f,
        "last_ask": last_ask_f,
        "last_ask_src": rec.get("last_ask_src"),
        "last_ask_age_sec": _f_or_none(rec.get("last_ask_age_sec")),
        "price": last_ask_f,
        # See public_snapshot: double_bottom (real shelf) vs offset (percentage
        # band, 5% stop) is the difference between two strategies, not a detail.
        "zone_kind": str(structure.get("zone_kind") or "") or None,
        "block_code": b_code,
        "blocker": b_label,
        "block_reason": b_label,
        "block_detail": rec.get("block_detail"),
        "pct_change": _wire_pct_change(sym, rec),
        "admit_pct_change": _f_or_none(rec.get("admit_pct_change")),
        "qty": None,
        "avg_entry": None,
        "pl": None,
        "plpc": None,
        "mkt_val": None,
        "local_stop": None,
        "is_position": False,
    }


def _row_risk_ps(r: dict) -> float:
    """Structural R for a book row: frozen risk, else zone floor − stop."""
    try:
        rps = float(r.get("risk_per_share") or 0)
    except (TypeError, ValueError):
        rps = 0.0
    if rps > 0:
        return rps
    try:
        lo = float(r.get("entry_low") or 0)
        stop = float(r.get("entry_stop_price") or r.get("stop_price") or 0)
    except (TypeError, ValueError):
        return 0.0
    if lo > 0 and stop > 0 and lo > stop:
        return lo - stop
    return 0.0


def _stamp_display_trail(rows: list) -> None:
    """RStop only exists on an open long. Watches show the plan stop, not a trail.

    Previewing last − give on a watch puts a shelf *above the zone* while we
    are still waiting to buy. The ratchet starts at fill (local_stop_price
    seeded from the entry stop), not on the book preview.
    """
    import ai_positions as cp

    cfg = _push_cfg()
    for r in rows:
        if not isinstance(r, dict):
            continue
        phase = str(r.get("phase") or r.get("status") or "").lower()
        is_open = bool(
            r.get("is_position")
            or phase in ("open", "submitted", "filled")
        )
        if not is_open:
            r["local_stop"] = None
            continue
        # Do not recompute last − give here. That number falls when last
        # dips (UMAC 33.98 → 33.80) while the engine shelf stays put.
        locked = cp.never_lower_rstop(
            r.get("local_stop"),
            r.get("local_stop_price"),
            r.get("entry_stop_price"),
        )
        if locked is not None:
            r["local_stop"] = round(locked, 6)


def book_table_rows(
    *,
    positions: dict | None = None,
    watch_rows: list | None = None,
    state: dict | None = None,
) -> list[dict]:
    """Unified AI book rows for the dashboard Watch section.

    Sources include research plus desk heat (``momentum`` / ``trending``)
    when those were seeded into the watch queue. Open broker positions
    appear as ``phase=open`` with live P&L (watch metadata preserved when
    the symbol was on the queue). Sort: open → ready → submitted → watching.
    """
    _record_desk_event("paint", time.time())
    pos_map = positions if isinstance(positions, dict) else {}
    by_sym: dict[str, dict] = {}

    # Prefer full watch state so submitted/filled stay visible until position
    # shows (or until expired/invalidated). Fall back to public_snapshot list.
    raw_state = state if isinstance(state, dict) else load_watch()
    if isinstance(raw_state, dict) and raw_state:
        for key, rec in raw_state.items():
            if not isinstance(rec, dict):
                continue
            sym = str(rec.get("symbol") or key or "").upper().strip()
            if not sym:
                continue
            status = str(rec.get("status") or "").lower().strip()
            if status in ("invalidated", "expired"):
                continue
            if (
                status not in ("submitted", "filled")
                and str(rec.get("block_code") or "") in (
                    "dead_reentry", "loser_reentry")
            ):
                continue
            by_sym[sym] = _watch_row_from_record(sym, rec)
    elif isinstance(watch_rows, list):
        for w in watch_rows:
            if not isinstance(w, dict):
                continue
            sym = str(w.get("symbol") or "").upper().strip()
            if not sym:
                continue
            ready = bool(w.get("ready"))
            status = str(w.get("status") or "watching").lower().strip()
            if status == "armed" or ready:
                phase = "ready"
            elif status == "submitted":
                phase = "submitted"
            else:
                phase = "watching"
            by_sym[sym] = {
                "symbol": sym,
                "phase": phase,
                "status": status,
                "ready": ready,
                "in_zone": bool(w.get("in_zone")),
                "source": w.get("source") or "research",
                "score": w.get("score"),
                "reason": w.get("reason"),
                "wait_kind": w.get("wait_kind"),
                "entry_low": w.get("entry_low"),
                "entry_high": w.get("entry_high"),
                "last_ask": w.get("last_ask"),
                "price": w.get("last_ask"),
                "block_code": w.get("block_code"),
                "blocker": w.get("blocker") or w.get("block_reason"),
                "block_reason": w.get("block_reason") or w.get("blocker"),
                "qty": None,
                "avg_entry": None,
                "pl": None,
                "plpc": None,
                "mkt_val": None,
                "is_position": False,
            }

    for sym_raw, p in pos_map.items():
        sym = str(sym_raw or "").upper().strip()
        if not sym or not isinstance(p, dict):
            continue
        prev = by_sym.get(sym) or {
            "symbol": sym,
            "source": "position",
            "score": None,
            "reason": None,
            "wait_kind": None,
            "entry_low": None,
            "entry_high": None,
            "last_ask": None,
        }
        current = p.get("current")
        if current is None:
            current = p.get("current_price")
        by_sym[sym] = {
            **prev,
            "phase": "open",
            "status": "open",
            "ready": False,
            "in_zone": False,
            "is_position": True,
            "blocker": None,
            "block_code": None,
            "block_reason": None,
            "price": current if current is not None else prev.get("price"),
            "last_ask": current if current is not None else prev.get("last_ask"),
            "qty": p.get("qty"),
            "avg_entry": p.get("avg_entry"),
            "pl": p.get("pl"),
            "plpc": p.get("plpc"),
            "mkt_val": p.get("mkt_val"),
            "local_stop": prev.get("local_stop"),
        }

    # Stamp the software trail so the book can show the lock level.
    try:
        import ai_positions as _cp
        managed = _cp._load_state()
    except Exception:
        managed = {}
    if isinstance(managed, dict):
        for msym, mpos in managed.items():
            if not isinstance(mpos, dict):
                continue
            key = str(msym or "").upper().strip()
            if not key or key not in by_sym:
                continue
            cfg = _push_cfg()
            try:
                give_r = float(cfg.get("ai_local_trail_give_r") or 0.10)
            except (TypeError, ValueError):
                give_r = 0.10
            by_sym[key]["trail_give_r"] = give_r
            try:
                give_px = float(cfg.get("ai_local_trail_give_px") or 0)
            except (TypeError, ValueError):
                give_px = 0.0
            by_sym[key]["trail_give_px"] = give_px if give_px > 0 else None
            try:
                risk = float(mpos.get("risk_per_share") or 0) or None
            except (TypeError, ValueError):
                risk = None
            by_sym[key]["risk_per_share"] = risk
            try:
                floor = float(mpos.get("entry_stop_price") or 0) or None
            except (TypeError, ValueError):
                floor = None
            if floor is None:
                try:
                    floor = float(mpos.get("stop_price") or 0) or None
                except (TypeError, ValueError):
                    floor = None
            by_sym[key]["entry_stop_price"] = floor
            loc = mpos.get("local_stop_price")
            try:
                by_sym[key]["local_stop"] = (
                    float(loc) if loc is not None else None)
            except (TypeError, ValueError):
                by_sym[key]["local_stop"] = None
            try:
                peak = float(mpos.get("peak_price") or 0) or None
            except (TypeError, ValueError):
                peak = None
            by_sym[key]["peak_price"] = peak
            et = mpos.get("entry_time")
            try:
                by_sym[key]["entry_time"] = float(et) if et is not None else None
            except (TypeError, ValueError):
                by_sym[key]["entry_time"] = None
            try:
                import ai_positions as _cp2
                now_h = time.time()
                held = _cp2.soft_exit_held_back(mpos, now_h)
                by_sym[key]["min_hold_active"] = bool(held)
                if held and et is not None:
                    left = float(cfg.get("ai_exit_min_hold_sec", 0) or 0) - (
                        now_h - float(et))
                    by_sym[key]["min_hold_left_sec"] = (
                        round(left, 1) if left > 0 else None)
                else:
                    by_sym[key]["min_hold_left_sec"] = None
                try:
                    by_sym[key]["min_hold_sec"] = float(
                        cfg.get("ai_exit_min_hold_sec", 0) or 0)
                except (TypeError, ValueError):
                    by_sym[key]["min_hold_sec"] = None
            except Exception:
                by_sym[key]["min_hold_active"] = False
                by_sym[key]["min_hold_left_sec"] = None
                by_sym[key]["min_hold_sec"] = None

    # Membership is owned by sync_watch_from_source_panels (watch file). Do NOT
    # re-filter against the pre-gate shortlist here: Stocktwits score/rvol
    # flicker was hiding valid book names (file had 5, UI showed 2 forever).
    # Open/submitted positions already land in by_sym above.
    rows = list(by_sym.values())

    # Prefer live desk tape for PRICE on every paint (same Finnhub/Alpaca path))
    # as Momentum Stocks). Zone levels stay from structure; only the print moves.
    for r in rows:
        if r.get("is_position") or str(r.get("phase") or "") == "open":
            continue
        try:
            got = stream_quote(r.get("symbol"))
        except Exception:
            got = None
        if got is not None:
            px, age = got
            if px and px > 0:
                r["price"] = px
                r["last_ask"] = px
                # Carry the print's own clock onto the row BEFORE
                # apply_tape_blocker. Discarding age here left last_ask_age
                # None/old on an otherwise live price, so the blocker
                # re-stamped stale_quote on stream rows (FRVO/OLOX/WKHS).
                try:
                    age_f = float(age) if age is not None else None
                except (TypeError, ValueError):
                    age_f = None
                if age_f is not None and age_f >= 0:
                    r["last_ask_age_sec"] = age_f
                    r["price_age_sec"] = age_f
                    # Carry the print clock onto the row AND the process map
                    # before apply_tape_blocker. book_table_rows used to set
                    # age/src from young live_print/eng while leaving a stale
                    # _LAST_QUOTE_TS from an earlier poll — row_quote_age_sec
                    # then preferred the old ts, apply_tape_blocker restamped
                    # stale_quote + honesty→stale_tape (GTLB Sep2 ~12:25 ET:
                    # eng≈3.5s while entry_book painted stale_tape≈18.6).
                    # public_snapshot (3f007f3) already writes the clock; this
                    # is the second paint path (entry_book) that did not.
                    _now_bk = time.time()
                    r["last_ask_ts"] = _now_bk - age_f
                    _sym_bk = str(r.get("symbol") or "").upper().strip()
                    if _sym_bk:
                        _set_quote_ts(_sym_bk, _now_bk - age_f)
                    max_age = decision_max_age_sec(_push_cfg())
                    if age_f <= max_age:
                        r["last_ask_src"] = "stream"
                        r["price_src"] = "stream"
                        clear_tape_data_block_if_stream_fresh(r)
                    else:
                        # PPBT Sep2 honesty: never leave last_ask_src=stream
                        # while age exceeds the decision ceiling — blocker
                        # stamps stale_quote next; src must match.
                        r["last_ask_src"] = "stale_tape"
                        r["price_src"] = "stale_tape"
                # Refresh above/below from live print so BLOCKER tracks the tape.
                # Armable overshoots (within max_r below the floor) count as
                # in-zone — same buy geometry as should_arm_buy.
                try:
                    lo = float(r.get("entry_low") or 0)
                    hi = float(r.get("entry_high") or 0)
                except (TypeError, ValueError):
                    lo = hi = 0.0
                try:
                    stop = float(r.get("stop_price") or 0) or None
                except (TypeError, ValueError):
                    stop = None
                if lo > 0 and hi > 0:
                    apply_tape_blocker(r, px)
                continue
        if _positive_price(r.get("price")) is None and _positive_price(r.get("last_ask")) is not None:
            r["price"] = r["last_ask"]

    _stamp_display_trail(rows)

    def _sort_key(r: dict) -> tuple:
        phase = str(r.get("phase") or "")
        phase_rank = {"open": 0, "ready": 1, "submitted": 2, "watching": 3}.get(
            phase, 9)
        # Stable order within phase (symbol) — score/P&L sorting caused UI jitter
        # as quotes ticked every couple of seconds.
        return (phase_rank, r.get("symbol") or "")

    rows.sort(key=_sort_key)
    return rows


def _row_passes_agreement(row: dict, cfg: dict) -> bool:
    """Agreement gate: require both-book agreement unless single-source mode."""
    if not cfg.get("ai_watch_require_agreement", True):
        return True
    if bool(row.get("agreement")):
        return True
    if cfg.get("ai_watch_single_source", False):
        return True
    return False


def _score_from_row(row: dict) -> float:
    for key in ("trending_score", "score", "ai_score"):
        if key in row and row[key] is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return 0.0


# Criteria that record something that HAPPENED rather than something that is
# currently true, and so must survive a re-seed. `bro_call` is the case: a
# Trader Bro call-out is only live for ai_watch_bb_live_fresh_sec (900s), and
# the seeder tags it onto rows another source owns. Without this, DAIC on
# 8/26 was tagged at 07:04, then momentum re-seeded it with
# ['mom_open', 'uptrend'] and the fact that a human had named it vanished
# from the record — the same disappearing act the source relabel was doing.
_STICKY_CRITERIA = frozenset({"bro_call"})


def _merge_admit_criteria(row: dict, prev: dict) -> list:
    """Admission criteria for this pass, keeping the sticky ones.

    Non-sticky criteria are a snapshot of why the name qualifies NOW, so the
    fresh list wins as before. Sticky ones are history and are unioned back
    in, deduplicated, in a stable order.
    """
    fresh = list((row or {}).get("criteria") or [])
    before = list((prev or {}).get("admit_criteria") or [])
    out = fresh or list(before)
    for c in before:
        if c in _STICKY_CRITERIA and c not in out:
            out.append(c)
    return out


def _admission_fields(row: dict, prev: dict, now: float) -> dict[str, Any]:
    """Admission provenance for a watch record — why this name was let on.

    Shared by both record builders. The live book is rebuilt by ``_sync_locked``
    every 2s; ``upsert_from_rows`` serves the research path. Duplicating these
    fields once already left the live path silently unrecorded, so both callers
    go through here.

    Falls back to *prev* so a refresh poll that arrives without the numbers
    (the producer publishes rvol=None until its volume refresh resolves) does
    not erase what admission actually saw.
    """
    prev = prev if isinstance(prev, dict) else {}
    row = row if isinstance(row, dict) else {}
    rvol = row.get("rvol")
    pct = row.get("pct_change")
    # Volume, from whichever producer supplied this row. The first version
    # of this read only "dollar_volume", a key that exists on the seed dicts
    # and on NO live row — dashboard rows keep rvol nested under "funnel"
    # and the research rows carry vol_session / avg_vol_consolidated. It
    # would have logged None every poll of every session: a dead column
    # that looks like a working one, which is the same defect as a knob
    # nothing reads.
    def _pick(*keys):
        for k in keys:
            v = row.get(k)
            if v is not None:
                return _f_or_none(v)
        for k in keys:
            v = prev.get("admit_" + k)
            if v is not None:
                return _f_or_none(v)
        return None

    vol_session = _pick("vol_session", "day_vol")
    avg_vol = _pick("avg_vol_consolidated", "avg_vol")
    dvol = row.get("dollar_volume")
    if dvol is None and vol_session is not None:
        px_now = _f_or_none(row.get("price"))
        dvol = (vol_session * px_now) if px_now else None
    return {
        # RVOL's numerator and denominator, so the ratio stops being a
        # number we either trust or discard. 3.94% of logged RVOLs exceed
        # 100 and the max is 81,820; with these two the bad ones become
        # diagnosable instead of merely flagged.
        "admit_vol_session": vol_session,
        "admit_avg_vol": avg_vol,
        "admit_rvol_raw": _f_or_none(row.get("rvol_raw")),
        "admit_rvol": (
            _f_or_none(rvol) if rvol is not None
            else _f_or_none(prev.get("admit_rvol"))),
        # Raw traded value at admission. RVOL is a ratio whose denominator
        # nobody logged, so a reading of 3144 was indistinguishable from a
        # reading of 3 — 3.94% of shadow RVOLs are above 100, which is not a
        # relative volume. dollar_volume / price recovers the share count, so
        # this is the column that makes RVOL auditable rather than trusted.
        "admit_dollar_volume": (
            _f_or_none(dvol) if dvol is not None
            else _f_or_none(prev.get("admit_dollar_volume"))),
        "admit_pct_change": (
            _f_or_none(pct) if pct is not None
            else _f_or_none(prev.get("admit_pct_change"))),
        "admit_look_reason": _look_reason_value(
            row, prev.get("admit_look_reason")),
        "admit_criteria": _merge_admit_criteria(row, prev),
        "admit_ts": float(prev.get("admit_ts") or now),
        # Arm-ready admit provenance (soft-seed / keep hygiene).
        "arm_ready": (
            bool(row["arm_ready"]) if "arm_ready" in row
            else (bool(prev["arm_ready"]) if "arm_ready" in prev else None)),
        "arm_ready_reason": (
            str(row.get("arm_ready_reason") or "")
            or str(prev.get("arm_ready_reason") or "")
            or None),
        "admit_chg_band": (
            str(row.get("admit_chg_band") or "")
            or str(prev.get("admit_chg_band") or "")
            or None),
        "scout_only": bool(row.get("scout_only") or prev.get("scout_only") or False),
        "scout_until": (
            _f_or_none(row.get("scout_until"))
            if row.get("scout_until") is not None
            else _f_or_none(prev.get("scout_until"))),
        "exh_seat_class": (
            str(row.get("exh_seat_class") or "")
            or str(prev.get("exh_seat_class") or "")
            or None),
        # Freeze first *known* class only — never lock in "unknown".
        "exh_seat_class_admit": (
            (
                str(prev.get("exh_seat_class_admit") or "").strip().lower()
                if str(prev.get("exh_seat_class_admit") or "").strip().lower()
                in _KNOWN_EXH_SEAT_CLASSES
                else ""
            )
            or (
                str(row.get("exh_seat_class") or "").strip().lower()
                if str(row.get("exh_seat_class") or "").strip().lower()
                in _KNOWN_EXH_SEAT_CLASSES
                else ""
            )
            or (
                str(prev.get("exh_seat_class") or "").strip().lower()
                if str(prev.get("exh_seat_class") or "").strip().lower()
                in _KNOWN_EXH_SEAT_CLASSES
                else ""
            )
            or None),
        "pctr_gap": (
            _f_or_none(row.get("pctr_gap"))
            if row.get("pctr_gap") is not None
            else _f_or_none(prev.get("pctr_gap"))),
        "far_exh_since": (
            _f_or_none(row.get("far_exh_since"))
            if row.get("far_exh_since") is not None
            else _f_or_none(prev.get("far_exh_since"))),
        "square_since": (
            _f_or_none(row.get("square_since"))
            if row.get("square_since") is not None
            else _f_or_none(prev.get("square_since"))),
    }


# A relative volume of 100x is already extraordinary; 3,144 and 81,820 both
# appear in shadow.jsonl. Above this the reading is a producer bug, not a
# busy tape, and anything averaging it has been eating garbage.
RVOL_SANE_MAX = 100.0


def _stream_pctr_fields(symbol: str, price: float | None, cfg: dict,
                        now: float) -> dict:
    """The SAME %R, computed from Finnhub stream bars instead of IEX bars.

    Shadow only — nothing reads these to decide. The point is to answer
    "would a denser feed fix the window" with a measurement instead of a
    projection, by running the identical ``_live_percent_r_line`` over the
    identical parameters and changing only the bar source.

    A stream has no history, so early in a name's life this is legitimately
    empty. That is reported as None rather than filled in, because the
    forward-only gap is one of the things being measured.
    """
    out = {"pctr_stream": None, "pctr_stream_src": None,
           "pctr_stream_bars": None, "pctr_stream_span_sec": None,
           "stream_bar_count": None, "stream_empty_min": None,
           "cm_rsi_stream": None, "cm_rsi_stream_rising": None,
           "cm_rsi_stream_bars": None}
    try:
        px = float(price) if price is not None else 0.0
        if px <= 0:
            return out
        import stream_bars
        # Feed the aggregator first so this minute includes the current
        # print, then read the window back. The watch loop polls every ~2s
        # against a median print gap of 11s, so this sees essentially every
        # price change without a trade stream in this process.
        stream_bars.observe(symbol, px, now)
        cov = stream_bars.coverage(symbol)
        out["stream_bar_count"] = cov["bars"]
        out["stream_empty_min"] = cov["empty_minutes"]
        if not cov["bars"]:
            return out
        length = _rte_fast_length(cfg)
        try:
            eps = float(cfg.get("rte_direction_eps", 0.05) or 0.0)
        except (TypeError, ValueError):
            eps = 0.05
        try:
            span = float(cfg.get("rte_fast_ewm_span", 7) or 7)
        except (TypeError, ValueError):
            span = 7.0
        try:
            min_range = int(cfg.get("ai_watch_exhaustion_min_range_bars", 6) or 6)
        except (TypeError, ValueError):
            min_range = 6
        try:
            slack = float(cfg.get("ai_watch_exhaustion_clock_slack", 1.25) or 1.25)
        except (TypeError, ValueError):
            slack = 1.25
        rows, span_sec = stream_bars.window_rows(symbol, now, length, slack)
        if rows:
            got = _live_percent_r_line(rows, px, length, span, eps,
                                       min_range=max(2, min_range))
            if got is not None:
                out["pctr_stream"] = got[0]
                out["pctr_stream_src"] = got[3]
                out["pctr_stream_bars"] = len(rows)
                out["pctr_stream_span_sec"] = span_sec
        # RSI on the same bars, through the same arithmetic live_cm_rsi
        # uses. RSI wants a long contiguous series rather than a clock
        # window — RMA smoothing carries the whole history — so it reads
        # every bar the aggregator holds, not the %R slice.
        all_rows, _ = stream_bars.window_rows(symbol, now, stream_bars.MAX_BARS,
                                              slack=1e9)
        if len(all_rows) >= 3:
            closes = [float(r[2]) for r in all_rows] + [px]
            try:
                period = max(2, int(cfg.get("cm_rsi_length", 2) or 2))
            except (TypeError, ValueError):
                period = 2
            series = cm_rsi_series(closes, period)
            if series:
                look = cm_rsi_trend_lookback(cfg)
                out["cm_rsi_stream"] = float(series[-1])
                out["cm_rsi_stream_rising"] = bool(
                    len(series) > look and series[-1] > series[-1 - look])
                out["cm_rsi_stream_bars"] = len(all_rows)
    except Exception:  # noqa: BLE001
        pass
    return out


def _news_fields(symbol: str, now: float) -> dict:
    """Catalyst features for the shadow row. Never raises, never blocks.

    Wrapped rather than called inline because this is the one field group
    backed by an external service. If ``news_feed`` is missing, the cache is
    corrupt, or anything else goes wrong, the row must still be written —
    a telemetry gap is a bad day, a raised exception inside the entry poll
    is a stopped desk.
    """
    try:
        import news_feed
        f = news_feed.features_for(symbol, now)
        f["cache_age_sec"] = news_feed.cache_age_sec()
        return f
    except Exception:  # noqa: BLE001
        f = {"has_news_24h": None, "n_news_24h": None, "mins_since": None,
             "bearish": None, "bullish": None, "cache_age_sec": None}
        return f


def _setup_fields(rec: dict, symbol: str, price: float | None,
                  sig: dict, news: dict) -> dict:
    """Stage 1 + stage 2 state for the shadow row. Never raises.

    Evaluated live rather than reconstructed later, because the
    conjunction spans four separate logs and a share count that changes
    after the fact would silently rewrite history. Same reason the news
    read is wrapped: this is telemetry, and telemetry may not stop a poll.
    """
    out = {
        "shares_out_m": None, "ok": None, "legs": None, "n_legs": None,
        "pctr_rising": None, "pctr_slow_rising": None,
        "pctr_slow_falling": None, "pctr_both_rising": None,
        "pctr_diverging": None,
        "rsi_at_bottom": None, "rsi_at_top": None,
        "setup_entry_ok": None, "setup_exit_ok": None,
    }
    try:
        import float_feed
        import setup_rules
        sig = sig if isinstance(sig, dict) else {}
        so = float_feed.shares_out(symbol)
        legs = setup_rules.evaluate(
            pct_change=rec.get("admit_pct_change"),
            rvol=rec.get("admit_rvol"),
            price=price,
            shares_out_m=so,
            news_mins_since=news.get("mins_since"),
            news_n_24h=news.get("n_news_24h"))
        s2 = setup_rules.stage2(
            pctr_rising=sig.get("pctr_rising"),
            pctr_slow_rising=sig.get("pctr_slow_rising"),
            pctr_slow_falling=sig.get("pctr_slow_falling"),
            cm_rsi=sig.get("cm_rsi"),
            cm_rsi_rising=sig.get("cm_rsi_rising"))
        out.update({
            "shares_out_m": so,
            "ok": legs["ok"],
            # Sorted names of the legs that passed — readable in a log tail
            # and cheap to group on, unlike five separate booleans.
            "legs": ",".join(sorted(k for k, v in legs.items()
                                    if k not in ("ok", "n_legs") and v)),
            "n_legs": legs["n_legs"],
            "pctr_rising": sig.get("pctr_rising"),
            "pctr_slow_rising": sig.get("pctr_slow_rising"),
            "pctr_slow_falling": sig.get("pctr_slow_falling"),
            "pctr_both_rising": s2["pctr_both_rising"],
            "pctr_diverging": s2["pctr_diverging"],
            "rsi_at_bottom": s2["rsi_at_bottom"],
            "rsi_at_top": s2["rsi_at_top"],
            "setup_entry_ok": s2["entry_ok"],
            "setup_exit_ok": s2["exit_ok"],
        })
    except Exception:  # noqa: BLE001
        pass
    return out


def _rvol_is_sane(v: Any) -> bool | None:
    """Is this RVOL a plausible ratio? None when there is no reading at all.

    Absence and nonsense are different states and must stay distinguishable:
    19 fills had no RVOL and ~4% of rows had an impossible one, and a single
    boolean that conflated them would hide the second inside the first.
    """
    f = _f_or_none(v)
    if f is None:
        return None
    return 0.0 < f <= RVOL_SANE_MAX


def _f_or_none(v: Any) -> float | None:
    """Float, or None when absent/unparseable. Never substitutes a default —
    a missing feature must stay missing so slicing can exclude it rather than
    average a zero into the result."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _window_span_min(rec: dict) -> float | None:
    """Minutes covered by this name's %R window, for the shadow row. None when
    the bars are not cached — a diagnostic, so it must never raise."""
    try:
        cfg = _push_cfg()
        length = max(2, int(cfg.get("rte_fast_length", 21) or 21))
        span = window_span_sec(rec.get("symbol") or "", length, cfg, time.time())
        return None if span is None else round(span / 60.0, 1)
    except Exception:
        return None


def _positive_price(v: Any) -> float | None:
    """Strict positive price, or None. Used to seed last_ask / reject blanks."""
    p = _f_or_none(v)
    if p is None or p <= 0:
        return None
    return p


def _seed_last_ask(prev: dict, row: dict | None = None) -> float | None:
    """Best price to put on a new/refreshed book row before the poller quotes.

    Prefer an already-polled ask, then the admission/producer price. Without
    this seed the book shows PRICE — until REST succeeds, and structure never
    builds on that first cycle (desk synth + LLM both require ask > 0).
    """
    prev = prev if isinstance(prev, dict) else {}
    row = row if isinstance(row, dict) else {}
    for v in (
        prev.get("last_ask"),
        row.get("last_ask"),
        row.get("price"),
        prev.get("price"),
    ):
        p = _positive_price(v)
        if p is not None:
            return p
    return None


def _opt_float(value: Any, default: float) -> float:
    """float(value), treating only missing/blank/unparseable as *default*.

    ``float(cfg.get(k, d) or d)`` cannot express a deliberate zero — it is why
    ai_watch_synth_trail_pct=0 came back as 2.5 and no config value could turn
    the runner trail off. Same trap for every zone percentage below.
    """
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _look_reason_value(row: dict, prev_val: Any = None) -> str | None:
    """The LOOK tag as a recorded VALUE, not a truthiness test.

    apply_look_highlights writes ``look_reason=""`` on every row it did not
    tag, so ``row.get("look_reason") or None`` collapsed "computed, not
    tagged" into the same None as "the producer never ran". completeness()
    counts non-None, so the feature read 0% present on BOTH arms forever —
    and the gate that turns on look_reason was the one thing it could not be
    measured against. Missing is not zero, and "not EXT" is not missing.

    Provenance stays sticky: a name admitted while tagged keeps the tag that
    let it on, the same way admit_ts keeps the moment it was admitted.
    """
    cur = str(row.get("look_reason") or "").strip().upper()
    if cur:
        return cur
    prior = str(prev_val or "").strip().upper()
    if prior and prior != "NONE":
        return prior
    return "NONE" if "look_reason" in row else None


def _is_wash_look(row: dict | None) -> bool:
    """LOOK=WASH is a near-low washout. Never seed or buy it."""
    rec = row if isinstance(row, dict) else {}
    for key in ("look_reason", "admit_look_reason"):
        if str(rec.get(key) or "").strip().upper() == "WASH":
            return True
    return False


def upsert_from_rows(
    rows: list[dict],
    *,
    cfg: dict,
    now: float,
) -> dict:
    """Merge research rows into watch state; save and return full state.

    Eligible rows become/stay ``watching`` with refreshed reason/score.
    Existing ``structure`` / poll fields are preserved when the symbol remains.
    """
    state = load_watch()
    if not isinstance(rows, list):
        save_watch(state)
        return state

    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _row_passes_agreement(row, cfg):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue

        prev = state.get(sym) if isinstance(state.get(sym), dict) else {}
        prev_status = str(prev.get("status") or "").lower().strip()
        # Never clobber in-flight / completed entries back to watching.
        if prev_status in ("submitted", "filled"):
            status = prev_status
        else:
            status = "watching"
        src = _merge_source(
            str(prev.get("source") or ""),
            str(row.get("source") or ""),
        )
        # Desk seeds refresh score/reason only when they own the row or are new;
        # research ownership keeps its thesis text.
        keep_research = (
            str(prev.get("source") or "").lower() in _RESEARCH_SOURCES
            and str(row.get("source") or "").lower() in _DESK_SOURCES
        )
        reason = (
            str(prev.get("reason") or "")
            if keep_research
            else str(row.get("reason") or prev.get("reason") or "")
        )
        score = (
            float(prev.get("score") or 0) if keep_research and prev.get("score") is not None
            else _score_from_row(row)
        )
        if keep_research and prev.get("score") is not None:
            try:
                score = float(prev.get("score"))
            except (TypeError, ValueError):
                score = _score_from_row(row)
        seeded_ask = _seed_last_ask(prev, row)
        rec: dict[str, Any] = {
            "symbol": sym,
            "status": status,
            "agreement": bool(row.get("agreement") if "agreement" in row else prev.get("agreement")),
            "score": score,
            "reason": reason,
            "source": src or "research",
            "structure": prev.get("structure", _EMPTY_RECORD_DEFAULTS["structure"]),
            "structure_ts": float(
                prev.get("structure_ts", _EMPTY_RECORD_DEFAULTS["structure_ts"]) or 0.0
            ),
            "last_poll_ts": float(
                prev.get("last_poll_ts", _EMPTY_RECORD_DEFAULTS["last_poll_ts"]) or 0.0
            ),
            "last_ask": seeded_ask,
            "updated_ts": float(now),
            **_admission_fields(row, prev, float(now)),
        }
        if prev.get("zone_touch_ts") is not None:
            rec["zone_touch_ts"] = prev["zone_touch_ts"]
        state[sym] = rec

    save_watch(state)
    # Subscribe quotes/indicators for every name we just put on the book.
    try:
        push_candidates_to_engine(list(state.keys()))
    except Exception:
        pass
    return state


def drop_missing(
    state: dict,
    active_symbols: set[str],
    now: float,
) -> dict:
    """Mark symbols not in *active_symbols* as invalidated; return state.

    Does not delete keys (history for events/debug); updates ``updated_ts``.
    """
    if not isinstance(state, dict):
        return {}
    active = {str(s).upper().strip() for s in (active_symbols or set()) if s}
    for sym, rec in list(state.items()):
        if not isinstance(rec, dict):
            continue
        key = str(sym or rec.get("symbol") or "").upper().strip()
        if not key or key in active:
            continue
        # Already terminal statuses stay as-is except still mark invalidated
        # when missing from research (thesis withdrawn).
        status = str(rec.get("status") or "")
        if status in ("filled", "submitted"):
            continue
        rec = dict(rec)
        rec["symbol"] = key
        rec["status"] = "invalidated"
        rec["updated_ts"] = float(now)
        state[key] = rec
        if key != sym:
            state.pop(sym, None)
    return state


def load_watch_close_state(day_key: str) -> tuple[bool, str]:
    """Persisted close-edge latch as ``(seen_open, expired_day)``.

    ``seen_open`` used to live only in the trader's in-memory ``book_state``,
    which meant a restart after the closing bell started it at False — the
    open→closed edge could then never be observed, so ``expire_open_watches``
    never ran and every ``watching`` row stayed live. The dashboard treats
    those rows as committed (``_committed_symbols``), so the momentum
    watchlist mirrored them all evening and re-stamped their ``added`` times.
    Observed 2026-08-18: the desk restarted at 19:31 and 10 names sat on the
    watchlist until the next day's first poll.

    ``seen_open`` is only meaningful for the day it was observed, so a latch
    stored under a different day reads back False. Otherwise a process started
    pre-market would inherit yesterday's "the market was open" and expire
    today's watches before the bell.

    Fails open — a missing or unreadable file must not be able to expire the
    book by itself.
    """
    day = str(day_key or "")
    try:
        raw = json.loads(WATCH_CLOSE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, ""
    if not isinstance(raw, dict):
        return False, ""
    seen = bool(raw.get("seen_open")) and str(raw.get("day") or "") == day
    return seen, str(raw.get("expired_day") or "")


def save_watch_close_state(
    day_key: str,
    seen_open: bool,
    expired_day: str,
) -> None:
    """Write the close-edge latch. Atomic, and never raises."""
    payload = {
        "day": str(day_key or ""),
        "seen_open": bool(seen_open),
        "expired_day": str(expired_day or ""),
        "ts": time.time(),
    }
    try:
        WATCH_CLOSE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = WATCH_CLOSE_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(WATCH_CLOSE_STATE_PATH)
    except OSError:
        pass


def should_expire_watches_on_close(
    *,
    market_open: bool,
    day_key: str,
    seen_open: bool,
    expired_day: str,
) -> tuple[bool, bool, str]:
    """Edge-detect RTH open → closed for watch expiry.

    Only expires after the market was observed open and then closed, and at
    most once per ET *day_key*. Pre-market closed samples do not latch
    ``expired_day`` and do not trigger expiry.

    Returns ``(should_expire, seen_open_next, expired_day_next)``.
    """
    day = str(day_key or "")
    expired = str(expired_day or "")
    if market_open:
        return False, True, expired
    # Market closed: expire only on open→closed edge, once per day.
    if seen_open and expired != day:
        return True, False, day
    return False, bool(seen_open), expired


_parse_hhmm = desk_core.parse_hhmm


def _et_now(now: float | None = None):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    t0 = float(now if now is not None else time.time())
    return datetime.fromtimestamp(t0, tz=ZoneInfo("America/New_York"))


def _et_hour_decimal(now: float | None = None) -> float | None:
    """ET hour as a decimal (9.5 == 09:30), for time-of-day slicing."""
    try:
        et = _et_now(now)
        return round(et.hour + et.minute / 60.0, 2)
    except Exception:
        return None


def _hhmm_to_hour_decimal(raw: Any, default: tuple[int, int]) -> float:
    """Parse ``HH:MM`` to ET hour decimal (9.5 == 09:30)."""
    h, m = _parse_hhmm(str(raw or ""), default)
    return float(h) + float(m) / 60.0


# Momentum + Trader Bro / research sources protected by the morning flood.
_MORNING_FLOOD_RESEARCH_SOURCES = frozenset({
    "research", "xai", "agy", "grok", "claude", "anthropic", "gemini", "google",
})


def is_morning_flood_source(row_or_src: Any) -> bool:
    """True for momentum* or research/xai/agy seats (morning flood set)."""
    if isinstance(row_or_src, dict):
        src = str(row_or_src.get("source") or "").lower().strip()
        crit = {str(c).lower() for c in (row_or_src.get("criteria") or [])}
        if src.startswith("momentum") or "momentum" in crit or "mom_open" in crit:
            return True
        if src in _MORNING_FLOOD_RESEARCH_SOURCES or "research" in crit:
            return True
        return False
    src = str(row_or_src or "").lower().strip()
    return src.startswith("momentum") or src in _MORNING_FLOOD_RESEARCH_SOURCES


def morning_flood_active(
    cfg: dict | None = None,
    now: float | None = None,
) -> bool:
    """True during the RTH morning flood window (default 09:30–11:00 ET).

    When active: momentum + Trader Bro/research names that are already
    square or pre_square bypass soft_seed_max. Far names do not take a
    keep seat while prefer-square is on. Arms unchanged.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not bool(cfg.get("ai_watch_morning_flood_enabled", True)):
        return False
    hour = _et_hour_decimal(now)
    if hour is None:
        return False
    start = _hhmm_to_hour_decimal(
        cfg.get("ai_watch_morning_flood_start", "09:30"), (9, 30))
    end = _hhmm_to_hour_decimal(
        cfg.get("ai_watch_morning_flood_end", "11:00"), (11, 0))
    if bool(cfg.get("ai_watch_morning_flood_include_pre", False)):
        start = min(start, 9.0)
    return float(start) <= float(hour) < float(end)


def _morning_flood_price_ok(row: dict, cfg: dict | None = None) -> bool:
    """Hard price floor for morning flood seats. Missing price → fail closed."""
    cfg = cfg if isinstance(cfg, dict) else {}
    px = _f_or_none(row.get("price") if isinstance(row, dict) else None)
    if px is None:
        return False
    try:
        min_px = float(cfg.get("ai_watch_min_price", 2.0) or 0.0)
    except (TypeError, ValueError):
        min_px = 2.0
    if min_px <= 0:
        return True
    return float(px) + 1e-12 >= float(min_px)


def past_eod_liquidate_time(cfg: dict | None, now: float | None = None) -> bool:
    """True on weekdays at/after ``ai_eod_liquidate_time`` ET (default 15:50).

    Used to block new paper entries once the EOD flatten window is open.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not bool(cfg.get("ai_eod_liquidate_enabled", True)):
        return False
    dt = _et_now(now)
    if dt.weekday() >= 5:
        return False
    bell_h, bell_m = _parse_hhmm(
        str(cfg.get("ai_eod_liquidate_time") or "15:50"), (15, 50))
    return (dt.hour, dt.minute) >= (bell_h, bell_m)


def watch_session_active(cfg: dict | None, now: float | None = None) -> bool:
    """True on weekdays from ``ai_watch_start_time`` (default 04:00 ET) until EOD.

    AI Watch may seed/sync and refresh structure in this window (premarket
    from 4:00). Paper *entries* still require regular-session market hours
    (see ``trading_hours_active``) and ``desk_product``.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not cfg.get("ai_watch_enabled", True):
        return False
    dt = _et_now(now)
    if dt.weekday() >= 5:
        return False
    if past_eod_liquidate_time(cfg, now):
        return False
    start_h, start_m = _parse_hhmm(
        str(cfg.get("ai_watch_start_time") or "04:00"), (4, 0))
    return (dt.hour, dt.minute) >= (start_h, start_m)


def sod_liquidate_done(cfg: dict | None, now: float | None = None) -> bool:
    """True once start-of-day flatten has run for this ET weekday (or SOD off).

    When ``ai_sod_liquidate_enabled`` is true, no new paper entries until the
    book loop has liquidated overnight leftovers at RTH open.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not bool(cfg.get("ai_sod_liquidate_enabled", True)):
        return True
    dt = _et_now(now)
    if dt.weekday() >= 5:
        return True  # no RTH session
    day_key = dt.strftime("%Y-%m-%d")
    try:
        from ai_positions import SOD_LIQUIDATE_STATE_PATH
        prev = json.loads(SOD_LIQUIDATE_STATE_PATH.read_text(encoding="utf-8"))
        return str(prev.get("last_day") or "") == day_key
    except Exception:
        return False


def trading_hours_active(
    cfg: dict | None,
    now: float | None = None,
    *,
    market_open: bool | None = None,
) -> bool:
    """True when new paper entries are allowed: RTH open, SOD flat done, pre-EOD.

    ``market_open`` is Alpaca's regular session clock when provided; if omitted
    the caller should pass it (this helper does not call the broker).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if market_open is False:
        return False
    if market_open is None:
        # Unknown — do not invent open; require explicit True for buys.
        return False
    if past_eod_liquidate_time(cfg, now):
        return False
    # Still respect watch start so we never trade before the book is live.
    if not watch_session_active(cfg, now):
        return False
    # No new entries until morning liquidate has wiped overnight positions.
    if not sod_liquidate_done(cfg, now):
        return False
    return True


def clear_watch_book(*, now: float | None = None) -> dict:
    """Wipe the AI Watch queue file entirely (EOD liquidate).

    Unlike ``expire_open_watches`` (soft status flip), this removes every
    symbol so the dashboard book goes empty. Callers must also skip
    ``sync_watch_from_source_panels`` until the next session or the book
    will immediately reseed from Mom/ST.
    """
    t0 = float(now if now is not None else time.time())
    try:
        save_watch({})
    except Exception:
        pass
    # Touch for operators/logs; empty dict is the public state.
    _ = t0
    return {}


def expire_open_watches(now: float) -> dict:
    """Mark open (watching/armed) watches as expired; save and return state.

    Terminal statuses (filled, submitted, invalidated, expired) are left
    unchanged. Used at RTH close when ``ai_watch_expire_at_close`` is set.
    """
    state = load_watch()
    if not isinstance(state, dict):
        return {}
    t0 = float(now)
    for sym, rec in list(state.items()):
        if not isinstance(rec, dict):
            continue
        key = str(sym or rec.get("symbol") or "").upper().strip()
        if not key:
            continue
        status = str(rec.get("status") or "")
        if status in _TERMINAL_STATUSES:
            continue
        # Open queue: watching / armed (and empty default as open).
        if status and status not in _ARMABLE_STATUSES:
            continue
        rec = dict(rec)
        rec["symbol"] = key
        rec["status"] = "expired"
        rec["updated_ts"] = t0
        state[key] = rec
        if key != sym:
            state.pop(sym, None)
    save_watch(state)
    return state


def expire_stale_watches_for_new_day(now: float) -> dict:
    """Expire watching/armed leftover from a prior ET calendar day.

    Uses max(updated_ts, structure_ts) in America/New_York. Records with no
    usable timestamp are treated as stale. Terminal statuses are unchanged.
    Does not latch close-edge state; safe to call every poll_once.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    state = load_watch()
    if not isinstance(state, dict):
        return {}
    t0 = float(now)
    et = ZoneInfo("America/New_York")
    today = datetime.fromtimestamp(t0, tz=et).date()
    changed = False
    for sym, rec in list(state.items()):
        if not isinstance(rec, dict):
            continue
        key = str(sym or rec.get("symbol") or "").upper().strip()
        if not key:
            continue
        status = str(rec.get("status") or "").lower().strip()
        if status in _TERMINAL_STATUSES:
            continue
        if status and status not in _ARMABLE_STATUSES:
            continue
        try:
            updated_ts = float(rec.get("updated_ts") or 0.0)
        except (TypeError, ValueError):
            updated_ts = 0.0
        try:
            structure_ts = float(rec.get("structure_ts") or 0.0)
        except (TypeError, ValueError):
            structure_ts = 0.0
        ts = max(updated_ts, structure_ts)
        if ts > 0:
            rec_day = datetime.fromtimestamp(ts, tz=et).date()
            if rec_day >= today:
                continue
        # Prior day (or no ts) → expire leftover open watch.
        rec = dict(rec)
        rec["symbol"] = key
        rec["status"] = "expired"
        rec["updated_ts"] = t0
        state[key] = rec
        if key != sym:
            state.pop(sym, None)
        changed = True
    if changed:
        save_watch(state)
    return state


def _price_under_cap(px: Any, max_price: Any) -> bool:
    if max_price is None:
        return True
    try:
        cap = float(max_price)
        if cap <= 0:
            return True
        p = float(px)
    except (TypeError, ValueError):
        return True  # unknown price: keep candidate
    return p < cap


def extreme_move_pct(cfg: dict | None = None) -> float:
    """Day-chg % at/above which an off-book name needs an explicit reason."""
    c = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(c.get("ai_watch_extreme_move_pct", 100.0) or 0.0))
    except (TypeError, ValueError):
        return 100.0


def _wire_pct_change(sym: str, rec: dict | None = None) -> float | None:
    """Live desk day % for the book wire; admit stamp as fallback."""
    live = None
    try:
        live = _desk_pct_change(sym)
    except Exception:
        live = None
    if live is not None:
        return live
    if isinstance(rec, dict):
        for key in ("pct_change", "admit_pct_change"):
            v = _pct_change_value(rec.get(key))
            if v is not None:
                return v
    return None


_RESEARCH_SOURCES = frozenset({
    "research", "xai", "agy", "google", "gemini", "anthropic", "grok", "claude",
    "a", "g", "x", "ax", "gx", "ai",
})
# Desk heat: seeds that name a symbol without carrying a thesis about it.
# A new seed source MUST be registered here. Omitted, it is not recognised as
# desk heat, `keep_research` goes False when it lands on a research row, and
# the seed overwrites that row's thesis text and score while _merge_source
# correctly keeps the source tag as "research" — a row labelled research
# wearing another seed's reason.
_DESK_SOURCES = frozenset({"momentum", "trending", "mom", "st", "stocktwits",
                           "movers"})
# "Bullish Bob LIVE" call-outs. Its own bucket, not a desk source: _merge_source
# lets research keep thesis ownership over desk heat, and a bro call should not
# be able to take a name away from a research thesis either.
_BB_LIVE_SOURCES = frozenset({"bb_live", "bro", "bb"})
# Every label the four source panels can put on a row. _sync_watch_locked drops
# anything else, so a new seed that is not listed here contributes nothing.
_PANEL_SOURCES = _DESK_SOURCES | _RESEARCH_SOURCES | _BB_LIVE_SOURCES


def _merge_source(prev_src: str, new_src: str) -> str:
    """Research beats everything less deliberate; otherwise newest wins.

    The bb_live half was documented above _BB_LIVE_SOURCES ("a bro call
    should not be able to take a name away from a research thesis either")
    and never implemented — this function did not reference the set at all,
    so `prev=research, new=bb_live` fell through to "newest wins" and the
    call took the row from the thesis.

    A bro call deliberately does NOT outrank desk heat: the seed loop calls
    it "the weakest evidence on this list" and only lets it contribute
    symbols nothing else already named. Ownership and visibility are
    separate problems — see the bb_live seed block, which tags `bro_call`
    onto a row another source owns instead of relabelling it.
    """
    p = str(prev_src or "").strip().lower()
    n = str(new_src or "").strip().lower()
    if not n:
        return prev_src or "research"
    if not p:
        return new_src or "research"
    # Research owns the row against anything less deliberate, bro included.
    if p in _RESEARCH_SOURCES and n not in _RESEARCH_SOURCES:
        return prev_src
    if n in _RESEARCH_SOURCES:
        return new_src
    return new_src or prev_src


def _momentum_has_flag(row: dict) -> bool:
    """True when Momentum Stocks would show FIRST / NEW / BURST.

    Mirrors ``static/js/tickers.js`` ``_flagsHtml``:
      FIRST  — find_it_first
      NEW    — mention_window == 1 and not mention_burst
      BURST  — mention_burst
    """
    if not isinstance(row, dict):
        return False
    if row.get("find_it_first"):
        return True
    if row.get("mention_burst"):
        return True
    try:
        mw = int(row.get("mention_window") or 0)
    except (TypeError, ValueError):
        mw = 0
    if mw == 1 and not row.get("mention_burst"):
        return True
    return False


# Same env key and default as signal_engine.py, so the engine and this module
# always read one universe. Hardcoding localhost here split the desk in two: the
# engine polled the remote box for its symbol list while this module pushed
# candidates to — and read signal_proximity from — a local dashboard the engine
# never saw. The push was a no-op, the indicator map was permanently empty, and
# should_arm_buy blocked every symbol on `no_indicators`.
DASHBOARD_URL = (os.getenv("DASHBOARD_URL") or "https://trading.jbrasfield.com").rstrip("/")
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")

# Remote hop, so the old 2s local timeouts are too tight to be a real signal.
_DASH_TIMEOUT = 4.0

# The edge in front of the remote dashboard 403s urllib's default
# "Python-urllib/x.y" agent. signal_engine.py never hit this because `requests`
# sends its own. Any non-default agent passes; identify ourselves honestly.
_DASH_UA = "trading-helper-desk/1.0"

# Bearer from POST /auth/login (DASHBOARD_USER / DASHBOARD_PASS in .env, then
# signal_engine.env). Middleware 401s /api/state without it, so momentum never
# seeded (CDTG sat on the desk and never reached the book).
#
# The login itself lives in desk_auth now. It used to live here, unlocked and
# with a forced re-login on every 401 — and since dashboard_state() runs on a
# ~2s tick, the day auth was switched on that turned one misconfiguration into
# 398 logins in a minute. desk_auth keeps one login in flight and puts a floor
# between attempts; see its module docstring.
_dash_auth = desk_auth.for_process(
    "ai_entry_watch",
    ROOT,
    default_url=DASHBOARD_URL,
    user_agent=_DASH_UA,
    log_prefix="[watch/auth]",
    timeout=_DASH_TIMEOUT,
)


def _load_dashboard_creds() -> None:
    """Resolve dashboard URL/user/pass and mirror them onto module globals."""
    global DASHBOARD_USER, DASHBOARD_PASS, DASHBOARD_URL
    url, user, password = _dash_auth.load_creds()
    DASHBOARD_URL, DASHBOARD_USER, DASHBOARD_PASS = url, user, password


def _dashboard_login(*, force: bool = False) -> str:
    """Cached Bearer token. Empty string on failure or while backing off."""
    _load_dashboard_creds()
    return _dash_auth.token(force=force)


def _dash_headers(*, json_body: bool = False) -> dict[str, str]:
    _load_dashboard_creds()
    return _dash_auth.headers(json_body=json_body)


def _dash_urlopen(url: str, *, data: bytes | None = None, method: str | None = None):
    """urllib GET/POST with env auth; one throttled re-login on 401."""
    _load_dashboard_creds()
    return _dash_auth.urlopen(url, data=data, method=method)

# Last dashboard fetch: (monotonic_ts, payload). Two callers used to issue their
# own GET (2s timeout each) on every 2s book tick, so a slow dashboard could eat
# ~4s per sync while holding nothing useful. One fetch, briefly cached, serves
# both — and carries the signal_proximity rows the inclusion gate needs.
_DASH_CACHE: tuple[float, dict] = (0.0, {})
_DASH_CACHE_TTL = 0.25

# Why the last fetch failed, surfaced as watch_meta.source_error. A bare
# `except: return []` made "dashboard is down" indistinguishable from "nothing
# is flagged", which is how momentum silently contributed zero for a whole day.
_dash_error: str = ""


def dashboard_state(*, force: bool = False) -> dict:
    """Cached GET of /api/state. Empty dict (and _dash_error set) on failure."""
    global _DASH_CACHE, _dash_error
    ts, cached = _DASH_CACHE
    mono = time.monotonic()
    if not force and cached and (mono - ts) < _DASH_CACHE_TTL:
        return cached
    try:
        import desk_io
        if desk_io.MODE == "replay":
            data = desk_io.serve_dash()
        else:
            with _dash_urlopen(f"{DASHBOARD_URL}/api/state") as resp:
                data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise TypeError(f"unexpected payload type {type(data).__name__}")
        desk_io.record_dash(data)
        _DASH_CACHE = (mono, data)
        _dash_error = ""
        return data
    except Exception as e:  # noqa: BLE001
        _dash_error = f"{type(e).__name__}: {e}"[:200]
        _DASH_CACHE = (mono, {})
        return {}


def dashboard_error() -> str:
    """Last dashboard fetch error ('' when healthy)."""
    return _dash_error


def dashboard_state_age_sec() -> float | None:
    """Seconds since the /api/state snapshot this process is reading was fetched.

    A reading's real age at decision time has two independent parts, and they
    fail independently: the age of the tape the engine computed on
    (``bars_age_sec``) and the age of THIS process's copy of that reading. A
    sub-second realtime bar read through a stalled transport is not a
    realtime decision, and only the first was ever recorded.

    Normally bounded by ``_DASH_CACHE_TTL``, which is exactly why it is worth
    logging: the value is uninteresting until the day it isn't.

    None when nothing has been fetched yet, or when the last fetch failed and
    cached an empty dict. Unknown age is not fresh — callers and slices must
    be able to tell the two apart, so this never reports 0.0 for "no idea".
    """
    ts, cached = _DASH_CACHE
    if not cached or ts <= 0:
        return None
    return round(max(0.0, time.monotonic() - ts), 2)


def _dashboard_tickers() -> list[dict]:
    rows = dashboard_state().get("tickers")
    return rows if isinstance(rows, list) else []


def _momentum_flagged_from_dashboard(max_price: Any) -> list[tuple[float, dict]]:
    """Momentum panel rows that currently show a flag (FIRST / NEW / BURST)."""
    tickers = _dashboard_tickers()
    if not tickers:
        return []
    scored: list[tuple[float, dict]] = []
    for r in tickers:
        if not isinstance(r, dict):
            continue
        if not _momentum_has_flag(r):
            continue
        s = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
        if not s or not s[0].isalpha():
            continue
        if is_levered_etp(s):
            continue
        if not _price_under_cap(r.get("price"), max_price):
            continue
        # Rank: BURST > FIRST > NEW
        rank = 9.0
        flags: list[str] = []
        if r.get("mention_burst"):
            rank = 10.0
            flags.append("BURST")
        if r.get("find_it_first"):
            rank = max(rank, 9.5)
            flags.append("FIRST")
        try:
            mw = int(r.get("mention_window") or 0)
        except (TypeError, ValueError):
            mw = 0
        if mw == 1 and not r.get("mention_burst"):
            rank = max(rank, 9.0)
            flags.append("NEW")
        reason = "momentum " + "+".join(flags) if flags else "momentum flag"
        # Carry the numbers the inclusion gate needs. Without price/pct_change
        # here every momentum row fails the price floor and direction gate on
        # missing data — the gate rejects absence rather than passing it.
        try:
            px = float(r.get("price")) if r.get("price") is not None else None
        except (TypeError, ValueError):
            px = None
        try:
            dvol = float(r.get("day_vol")) if r.get("day_vol") is not None else None
        except (TypeError, ValueError):
            dvol = None
        scored.append((rank, {
            "symbol": s,
            "trending_score": round(rank, 2),
            "score": round(rank, 2),
            "reason": reason[:40],
            "agreement": True,
            "source": "momentum",
            "price": px,
            "pct_change": _pct_change_value(r.get("pct_change")),
            "rvol": r.get("rvol"),
            "dollar_volume": (dvol * px) if (dvol and px) else None,
            "criteria": ["flag"],
        }))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored


def _pct_change_value(raw: Any) -> float | None:
    """Normalize pct_change to percent units (12.5 == +12.5%).

    Accepts either percent (12.5) or fraction (0.125). Values with |x| <= 1.5
    and non-integer-ish fractions are treated as fractions.
    """
    try:
        p = float(raw)
    except (TypeError, ValueError):
        return None
    # Heuristic: |p| <= 2 and looks fractional → convert to percent.
    if abs(p) <= 2.0 and abs(p) != 0:
        # 0.5 → 50%, 1.5 → 150%; keep 1.0 as 100% only if clearly a fraction
        # Desk/trending already use percent (e.g. 12.5, 161.47).
        pass
    return p


def _big_mover_from_dashboard(
    max_price: Any,
    min_pct: float,
) -> list[tuple[float, dict]]:
    """Momentum desk names whose day change is above *min_pct* — upside only."""
    tickers = _dashboard_tickers()
    if not tickers:
        return []
    scored: list[tuple[float, dict]] = []
    for r in tickers:
        if not isinstance(r, dict):
            continue
        pct = _pct_change_value(r.get("pct_change"))
        # Signed, not abs(): the desk is long-only (OrderSide.BUY), so a name
        # down 60% is not a candidate. abs() ranked exactly those highest.
        if pct is None or pct <= float(min_pct):
            continue
        s = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
        if not s or not s[0].isalpha():
            continue
        if is_levered_etp(s):
            _note_seed_drop("momentum", s, "levered_etp", pct=pct,
                            price=r.get("price"))
            continue
        if not _price_under_cap(r.get("price"), max_price):
            _note_seed_drop("momentum", s, "price_cap", pct=pct,
                            price=r.get("price"))
            continue
        try:
            px = float(r.get("price")) if r.get("price") is not None else None
        except (TypeError, ValueError):
            px = None
        scored.append((pct, {
            "symbol": s,
            "trending_score": round(pct, 2),
            "score": round(pct, 2),
            "reason": f"momentum chg {pct:+.0f}%",
            "agreement": True,
            "source": "momentum",
            "price": px,
            "pct_change": pct,
            "rvol": r.get("rvol"),
            "criteria": ["big_move"],
        }))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored


# The call-out stream is a running commentary, not a buy list. One symbol gets
# narrated the whole way through — "JWEL + AUUD on watch" → "retest hod" →
# "sold lotto flat" — and the dashboard keeps only the newest call per symbol
# (ingest_discord_alerts replaces same-ticker records), so the latest line IS
# the caller's current stance on the name.
#
# That makes filtering mandatory rather than a refinement. Recency is the rank
# key, and the last thing said about a symbol is usually how the trade ended,
# so unfiltered the shortlist selects *for* exits: on 2026-08-10 both live
# bb_live candidates were "sold" calls, and 12 of that morning's 34 archived
# call-outs were an exit or an explicit pass.

# Past tense = he is out. "was a"/"were" catch the post-mortems ("AUUD was a
# fun one to get us started"), which read as praise but describe a closed trade.
_BB_EXIT_PAT = re.compile(
    r"\b(?:sold|sell|selling|out|stopped|closed|trimmed|was\s+a|were)\b",
    re.I,
)
# Explicit passes. "will adjust OR avoid" is his standard disclaimer on a
# sub-$1 or into-resistance name; "not for me" and float complaints are the
# same verdict in other words.
_BB_AVOID_PAT = re.compile(
    r"(?:not\s+for\s+me|avoid|\blg\s+float\b|larger\s+float)",
    re.I,
)


def _bb_call_is_actionable(text: str) -> bool:
    """False when the caller's latest line says he is out of, or passing on, it.

    Deliberately conservative in one direction only: an unrecognised line is
    treated as actionable, because the shortlist is not an admission —
    passes_inclusion still has to clear the name on price, liquidity and
    trend. A missed filter costs one gated candidate; a missed exit would put
    the desk on the wrong side of the only person whose opinion this seed is.
    """
    t = str(text or "").strip()
    if not t:
        return True
    return not (_BB_EXIT_PAT.search(t) or _BB_AVOID_PAT.search(t))


def _bb_live_from_dashboard(
    max_price: Any,
    fresh_sec: float,
    now: float | None = None,
) -> list[tuple[float, dict]]:
    """Recent "Bullish Bob LIVE" call-outs as watch candidates.

    A call-out is (symbol, free text, timestamp) and nothing more — the caller
    naming what he is on. There is no score to rank by and no volume to gate
    on, so recency is the only thing the source itself can say: rank is seconds
    of freshness remaining, newest call first.

    Every number the inclusion gate needs is read off the desk row for the same
    symbol, never invented here. A called name the desk has no row for yields
    price=None, which passes_inclusion rejects as ``no_price`` — and that is the
    correct outcome for one tick only: the symbol still ships to the engine via
    push_candidates_to_engine, so it is quoted by the next sync and gets judged
    on real numbers instead of on the call alone.

    Freshness is measured from ``at`` (when the call was *said*, per Discord's
    own stamp) and not from ``unix`` (when OCR happened to read it). On a fresh
    start the source re-posts a whole screen at once; capture time would make an
    hour of stale calls all look current.
    """
    now = float(now if now is not None else time.time())
    try:
        fresh = float(fresh_sec or 0)
    except (TypeError, ValueError):
        fresh = 0.0
    if fresh <= 0:
        return []

    bb = dashboard_state().get("bb_live")
    history = bb.get("history") if isinstance(bb, dict) else None
    if not isinstance(history, list) or not history:
        return []

    # Desk rows carry the price/pct/rvol a call-out cannot.
    desk: dict[str, dict] = {}
    for r in _dashboard_tickers():
        if isinstance(r, dict):
            s = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
            if s:
                desk[s] = r

    scored: list[tuple[float, dict]] = []
    seen: set[str] = set()
    for c in history:
        if not isinstance(c, dict):
            continue
        s = str(c.get("ticker") or c.get("symbol") or "").upper().strip()
        if not s or not s[0].isalpha() or s in seen:
            continue
        try:
            at = float(c.get("at") or c.get("unix") or 0)
        except (TypeError, ValueError):
            continue
        if at <= 0:
            continue
        age = now - at
        # Negative age means a clock skew between the OCR box and this one, not
        # a call from the future. Treat it as brand new rather than dropping it.
        if age > fresh:
            continue
        # Mark it seen either way: a symbol whose newest call is an exit is
        # settled, and an older bullish line for the same name must not
        # resurrect it. "AUUD sold lotto - loss" ends AUUD for this session,
        # even though "AUUD retest hod with vol" is still in the history.
        seen.add(s)
        if not _bb_call_is_actionable(c.get("text")):
            continue

        row = desk.get(s) or {}
        if not _price_under_cap(row.get("price"), max_price):
            continue
        try:
            px = float(row.get("price")) if row.get("price") is not None else None
        except (TypeError, ValueError):
            px = None
        try:
            dvol = float(row.get("day_vol")) if row.get("day_vol") is not None else None
        except (TypeError, ValueError):
            dvol = None

        said = str(c.get("said") or "").strip()
        rank = max(0.0, fresh - max(0.0, age))
        scored.append((rank, {
            "symbol": s,
            "trending_score": round(rank, 2),
            "score": round(rank, 2),
            "reason": (f"bro call {said}".strip() if said else "bro call")[:40],
            "agreement": True,
            "source": "bb_live",
            "price": px,
            "pct_change": _pct_change_value(row.get("pct_change")),
            "rvol": row.get("rvol"),
            "dollar_volume": (dvol * px) if (dvol and px) else None,
            "criteria": ["bro_call"],
        }))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored


def _live_quote_map() -> tuple[dict[str, dict], dict[str, dict]]:
    """(desk rows, trending rows) keyed by symbol, for enriching a seed.

    The research seed has always done this: a seeded row carries whatever its
    SOURCE happened to record, and for a thesis that is nothing at all, so
    without a live quote every research name failed no_price / not_uptrend.
    The movers seed needs it for a different reason — its rows carry the
    producer's price and pct_change, which stop moving the instant the
    producer does. Enriched, a stale file changes which NAMES are considered
    and never what they are worth.

    Two maps rather than one merged dict so a caller can state its own
    precedence. Both are cheap: _dashboard_tickers is already cached and the
    trending file is a small local read.
    """
    desk_rows: dict[str, dict] = {}
    for r in _dashboard_tickers():
        if isinstance(r, dict):
            k = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
            if k:
                desk_rows[k] = r
    tr_by: dict[str, dict] = {}
    try:
        path = ROOT / "trending_stocks.json"
        raw_tr = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        for tr in (raw_tr.get("rows") or []):
            if not isinstance(tr, dict):
                continue
            k = str(tr.get("symbol") or tr.get("ticker") or "").upper().strip()
            if k:
                tr_by[k] = tr
    except Exception:  # noqa: BLE001
        tr_by = {}
    return desk_rows, tr_by


_LAST_POOL_CTX: dict = {}


def _cfg_hash(cfg) -> str | None:
    """Short digest of the config a pool was built with (a caller holding a
    stale startup cfg shows up as a second hash)."""
    try:
        import hashlib
        return hashlib.md5(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:8]
    except Exception:  # noqa: BLE001
        return None


def desk_candidate_rows(
    cfg: dict | None = None,
    now: float | None = None,
) -> list[dict]:
    """Momentum + trending + research + Trader Bro candidates for AI Watch.

    Rules (operator):
      • Trending: score **> min** (default 10), **or** |day chg %| > min (50),
        **or** relative volume **> min** (default 1.0 = 100% of avg).
      • Momentum: FIRST / NEW / BURST flag on the desk,
        **or** |day pct_change| above min (default 50).
      • Research: whatever is on the Grok / Anthropic boards right now.
      • Trader Bro: call-outs said within ai_watch_bb_live_fresh_sec.

    Each seed is a *shortlist*, not an admission. passes_inclusion() is
    conjunctive and still has to clear every name, and the structure poller
    still defines zone/stop before arming a buy.

    ``now`` (unix seconds) is the clock the morning-flood window is judged
    against. Default ``None`` reads the wall clock, which is what the live
    desk does; tests and replay pass it so a run is reproducible instead of
    depending on what time of day it happens to execute.

    Order matters: seeds run strongest-claim first and `seen` makes the first
    one to name a symbol own its row. Momentum and trending come first because
    their rows carry price, pct_change and rvol measured off the desk — the
    numbers the gate actually judges. Research and a bro call are reasons to
    look, and neither should overwrite a row that already has evidence in it.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    rows: list[dict] = []
    seen: set[str] = set()
    _clear_seed_drops()
    _st: dict = {}
    eq = None
    try:
        from desk_risk import dynamic_max_price
        _st = dashboard_state() or {}
        eq = float(_st.get("ai_positions", {}).get("account", {}).get("equity") or 0.0)
        max_price = dynamic_max_price(eq, cfg)
    except Exception:
        max_price = cfg.get("ai_max_price", cfg.get("claude_max_price"))
    # What this build saw, for the recorder's pool note (the 2026-09-25 pool
    # flicker: ~12 names over the price cap entered for ~11 s every ~2 min).
    _LAST_POOL_CTX.clear()
    _LAST_POOL_CTX.update({
        "dash_n": len(_st.get("tickers") or []) if isinstance(_st, dict) else None,
        "eq": eq, "max_price": max_price})
    try:
        min_pct = float(cfg.get("ai_watch_min_pct_change", 50.0) or 50.0)
    except (TypeError, ValueError):
        min_pct = 50.0
    # Gates the soft open seed below. Separate knob because min_pct governs
    # _big_mover_from_dashboard only; 0.0 keeps the shipped behaviour.
    try:
        open_seed_min_pct = float(
            cfg.get("ai_watch_open_seed_min_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        open_seed_min_pct = 0.0
    try:
        # Ratio units: 1.0 == 100% of average volume (same as desk RVOL display).
        min_rvol = float(cfg.get("ai_watch_min_rvol", 2.0) or 2.0)
    except (TypeError, ValueError):
        min_rvol = 2.0
    # One map for every seed source. Heating RVOL relief needs %R that
    # often lives on signal_proximity, not on the seed file row.
    try:
        _seed_inds = _engine_indicator_map()
    except Exception:
        _seed_inds = {}

    flood = morning_flood_active(cfg, now)
    _LAST_POOL_CTX["flood"] = bool(flood)

    if cfg.get("ai_watch_seed_momentum", True):
        try:
            n = int(cfg.get("ai_watch_seed_momentum_n", 12) or 12)
            n = max(1, n)
            scored = _momentum_flagged_from_dashboard(max_price)
            # Also include huge day movers on the momentum desk (no flag required).
            have = {r["symbol"] for _, r in scored}
            for sc, r in _big_mover_from_dashboard(max_price, min_pct):
                if r["symbol"] in have:
                    continue
                scored.append((sc, r))
            scored.sort(key=lambda t: t[0], reverse=True)
            # Morning flood: no N truncate — seat the full flagged/big-mover set.
            if flood:
                n = max(n, len(scored))
            for _, r in scored[:n]:
                if r["symbol"] in seen:
                    _note_proposal_overlap("momentum", r["symbol"], row=r)
                    continue
                seen.add(r["symbol"])
                if flood:
                    r = dict(r)
                    r["morning_flood"] = 1
                rows.append(r)
        except Exception:
            pass

    # Momentum Stocks panel → AI Watch with a soft path (no score/indicators),
    # but RVOL still applies when known — capital-quality heat, not a free pass.
    # min_price / max_price still apply so we can zone and size.
    if cfg.get("ai_watch_seed_momentum_open", True):
        try:
            n = int(cfg.get("ai_watch_seed_momentum_open_n", 10) or 10)
            n = max(1, n)
            if flood:
                # Take the whole desk panel during flood (still price/RVOL gated).
                n = max(n, 50)
            # Prefer names also on Stocktwits trending (heat overlap), then the
            # rest of the momentum panel so the book fills from the desk.
            tr_rank: dict[str, float] = {}
            try:
                path = ROOT / "trending_stocks.json"
                raw_tr = (
                    json.loads(path.read_text(encoding="utf-8"))
                    if path.exists() else {}
                )
                for i, tr in enumerate(raw_tr.get("rows") or []):
                    if not isinstance(tr, dict):
                        continue
                    k = str(tr.get("symbol") or tr.get("ticker") or "").upper().strip()
                    if not k:
                        continue
                    try:
                        sc = float(tr.get("trending_score", tr.get("score") or 0) or 0)
                    except (TypeError, ValueError):
                        sc = 0.0
                    tr_rank[k] = max(tr_rank.get(k, 0.0), sc, 1000.0 - i)
            except Exception:
                tr_rank = {}
            open_scored: list[tuple[float, dict]] = []
            for r in _dashboard_tickers():
                if not isinstance(r, dict):
                    continue
                s = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
                if not s or not s[0].isalpha():
                    continue
                if s in seen:
                    _note_proposal_overlap(
                        "momentum", s,
                        extra={
                            "price": r.get("price"),
                            "pct": _pct_change_value(r.get("pct_change")),
                            "rvol": r.get("rvol"),
                        },
                    )
                    continue
                seed_pct = _pct_change_value(r.get("pct_change"))
                if is_levered_etp(s):
                    _note_seed_drop("momentum", s, "levered_etp",
                                    pct=seed_pct, price=r.get("price"))
                    continue
                if not _price_under_cap(r.get("price"), max_price):
                    _note_seed_drop("momentum", s, "price_cap",
                                    pct=seed_pct, price=r.get("price"))
                    continue
                # Known-thin tape: hot day-move waives (same as movers).
                try:
                    rv = float(r.get("rvol")) if r.get("rvol") is not None else None
                except (TypeError, ValueError):
                    rv = None
                thin_why, seed_ind = seed_rvol_gate(
                    s, rv, seed_pct, cfg, source="momentum",
                    row=r, indicators=_seed_inds)
                if thin_why == "thin_rvol":
                    _note_seed_drop("momentum", s, "thin_rvol",
                                    pct=seed_pct, rvol=rv)
                    continue
                # This path deliberately does NOT use ai_watch_min_pct_change:
                # that knob gates _big_mover_from_dashboard, and this is the
                # soft open seed (bypass_inclusion / mom_open_soft), which is
                # where most admissions actually come from — the median one
                # lands at +8.2% against a knob that reads 50. Anyone reading
                # the config was reading a threshold that never applied here.
                # Its own knob, defaulting to 0.0 = admit as before, so this
                # is a truthful name for existing behaviour and a real dial
                # for the admission-latency work (see HANDOFF.md §5).
                # Young stream on the desk: curated mom moves fast — a name
                # that already prints can use a slightly lower day-chg floor
                # so it is not dead on arrival waiting for open_seed_min_pct.
                stream_age = None
                for _ak in ("price_age_sec", "rt_price_age_sec", "last_ask_age_sec"):
                    if r.get(_ak) is not None:
                        try:
                            stream_age = float(r.get(_ak))
                            break
                        except (TypeError, ValueError):
                            pass
                try:
                    stream_young_max = float(
                        cfg.get("ai_watch_stream_max_age_sec", 10.0) or 10.0)
                except (TypeError, ValueError):
                    stream_young_max = 10.0
                has_young_stream = (
                    stream_age is not None
                    and stream_young_max > 0
                    and stream_age <= stream_young_max
                )
                try:
                    stream_min_pct = float(
                        cfg.get("ai_watch_open_seed_stream_min_pct", 5.0) or 0.0)
                except (TypeError, ValueError):
                    stream_min_pct = 5.0
                need_pct = open_seed_min_pct
                if has_young_stream and stream_min_pct > 0:
                    if open_seed_min_pct <= 0:
                        need_pct = 0.0
                    else:
                        need_pct = min(open_seed_min_pct, stream_min_pct)
                if need_pct > 0:
                    if seed_pct is None or seed_pct < need_pct:
                        continue
                if _is_wash_look(r):
                    continue
                try:
                    px = float(r.get("price")) if r.get("price") is not None else None
                except (TypeError, ValueError):
                    px = None
                try:
                    dvol = float(r.get("day_vol")) if r.get("day_vol") is not None else None
                except (TypeError, ValueError):
                    dvol = None
                on_tr = s in tr_rank
                # Prefer trending-overlap, then |day chg| as a soft rank only.
                # Boost names that already have a young stream print so the
                # book fills with armable mom heat, not stale ETP leftovers.
                pct = seed_pct
                rank = float(tr_rank.get(s) or 0.0)
                if not on_tr:
                    rank = abs(pct or 0.0)
                if has_young_stream:
                    rank += 50.0
                reason = (
                    f"mom+trending {tr_rank[s]:.0f}"
                    if on_tr
                    else "momentum desk"
                )
                if has_young_stream:
                    reason = (reason + " stream")[:40]
                crit = ["mom_open", "mom_trending"] if on_tr else ["mom_open"]
                if has_young_stream:
                    crit = list(crit) + ["mom_stream"]
                mom_row = {
                    "symbol": s,
                    "trending_score": round(rank, 2),
                    "score": round(rank, 2),
                    "reason": reason[:40],
                    "agreement": True,
                    "source": "momentum",
                    "price": px,
                    "pct_change": pct,
                    "rvol": r.get("rvol"),
                    "dollar_volume": (dvol * px) if (dvol and px) else None,
                    # Soft seed: skip score/indicators only — not RVOL.
                    "criteria": crit,
                    "bypass_inclusion": False,
                    "mom_open_soft": True,
                }
                if isinstance(seed_ind, dict):
                    mom_row["indicator"] = seed_ind
                open_scored.append((rank, mom_row))
            open_scored.sort(key=lambda t: t[0], reverse=True)
            added = 0
            extreme_floor = extreme_move_pct(cfg)
            for _, r in open_scored:
                if added >= n:
                    # Extreme movers truncated by soft-open N must not vanish.
                    pct_left = _pct_change_value(r.get("pct_change"))
                    if (
                        extreme_floor > 0
                        and pct_left is not None
                        and pct_left + 1e-12 >= extreme_floor
                        and r["symbol"] not in seen
                    ):
                        _note_seed_drop(
                            "momentum", r["symbol"], "shortlist_cap",
                            pct=pct_left, price=r.get("price"))
                    continue
                if r["symbol"] in seen:
                    _note_proposal_overlap("momentum", r["symbol"], row=r)
                    continue
                seen.add(r["symbol"])
                if flood:
                    r = dict(r)
                    r["morning_flood"] = 1
                rows.append(r)
                added += 1
        except Exception:
            pass

    if cfg.get("ai_watch_seed_trending", True):
        try:
            path = ROOT / "trending_stocks.json"
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            tr_rows = raw.get("rows") or []
            try:
                min_score = float(cfg.get("ai_watch_trending_min_score", 5.0) or 5.0)
            except (TypeError, ValueError):
                min_score = 5.0
            if isinstance(tr_rows, list):
                n = int(cfg.get("ai_watch_seed_trending_n", 20) or 20)
                for r in tr_rows:
                    if not isinstance(r, dict):
                        continue
                    if r.get("is_crypto") is True:
                        continue
                    if r.get("is_equity") is False:
                        continue
                    s = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
                    if not s or not s[0].isalpha():
                        continue
                    if s in seen:
                        # Attribute the propose without changing first-wins ownership.
                        _note_proposal_overlap(
                            "trending", s,
                            extra={
                                "price": r.get("price"),
                                "pct": _pct_change_value(r.get("pct_change")),
                                "rvol": r.get("rvol"),
                                "score": r.get("trending_score", r.get("score")),
                            },
                        )
                        continue
                    if is_levered_etp(s):
                        continue
                    if not _price_under_cap(r.get("price"), max_price):
                        continue
                    try:
                        score = float(r.get("trending_score", r.get("score") or 0) or 0)
                    except (TypeError, ValueError):
                        score = 0.0
                    pct = _pct_change_value(r.get("pct_change"))
                    look = str(r.get("look_reason") or "").strip().upper()
                    # Trending seed: shortlist heat for the book — not a buy.
                    # WASH is always refused (red-day washout).
                    # EXT is preferred but not required unless
                    # ai_watch_require_look_ext is true (2026-08-11: hard EXT
                    # only left ~10 trending names on the book and 4 fills).
                    if look == "WASH":
                        continue
                    require_ext = bool(cfg.get("ai_watch_require_look_ext", False))
                    if require_ext and look != "EXT":
                        continue
                    # Source-specific day-change floor (default below the 50%
                    # momentum big-mover bar so real Stocktwits heat can seed).
                    try:
                        tr_min_pct = float(
                            cfg.get("ai_watch_trending_min_pct_change", 15.0)
                            or 15.0
                        )
                    except (TypeError, ValueError):
                        tr_min_pct = 15.0
                    try:
                        tr_min_rvol = float(
                            cfg.get("ai_watch_trending_min_rvol", min_rvol)
                            or min_rvol
                            or 0.0
                        )
                    except (TypeError, ValueError):
                        tr_min_rvol = min_rvol
                    rvol = None
                    for key in ("rvol", "rvol_raw"):
                        if r.get(key) is not None:
                            try:
                                rvol = float(r.get(key))
                                break
                            except (TypeError, ValueError):
                                pass
                    # rvol is a ratio (1.0 = 100% of avg). Also accept percent-like values.
                    if rvol is not None and rvol > 10.0:
                        # e.g. 150 meaning 150% → 1.5x
                        rvol = rvol / 100.0
                    # Known-thin: hot day-move waives (same helper as movers).
                    # Engine %R on the wire can relieve the floor before the
                    # name is wiped for lacking an indicator on this file row.
                    thin_why, seed_ind = seed_rvol_gate(
                        s, rvol, pct, cfg, source="trending",
                        row=r, indicators=_seed_inds)
                    if thin_why == "thin_rvol":
                        _note_seed_drop("trending", s, "thin_rvol",
                                        pct=pct, rvol=rvol, score=score)
                        continue
                    # Need at least one claim: score, day move, or elevated rvol.
                    score_ok = score > min_score
                    pct_ok = pct is not None and pct >= tr_min_pct
                    rvol_ok = (
                        rvol is not None
                        and tr_min_rvol > 0
                        and rvol >= tr_min_rvol
                    )
                    # Heating relief already cleared thin_rvol. Count that
                    # as the rvol claim so no_claim does not undo it.
                    if (
                        not rvol_ok
                        and rvol is not None
                        and isinstance(seed_ind, dict)
                        and rising_heat_quality(
                            {"symbol": s, "indicator": seed_ind}, cfg)
                    ):
                        try:
                            heat_floor = float(
                                cfg.get("ai_watch_heating_min_rvol", 1.25) or 0.0)
                        except (TypeError, ValueError):
                            heat_floor = 1.25
                        if heat_floor > 0 and float(rvol) + 1e-12 >= heat_floor:
                            rvol_ok = True
                    # Long-only: refuse red days when we know the change.
                    if pct is not None and pct <= 0:
                        _note_seed_drop("trending", s, "red", pct=pct)
                        continue
                    # Always need a numeric claim (score / day move / rvol),
                    # even when EXT is optional. Leaving this behind
                    # require_look_ext let low-heat TREND clutter (AI +1% /
                    # score 3, RIVN/TGTX/RUN/EBS) occupy the book while
                    # real heat (NTSK +10%, BULL score 6+) had to share
                    # slots. EXT stays optional — do not flip
                    # ai_watch_require_look_ext (historical: ~10 names /
                    # 4 fills when hard-EXT).
                    if not (score_ok or pct_ok or rvol_ok):
                        _note_seed_drop("trending", s, "no_claim",
                                        pct=pct, rvol=rvol, score=score)
                        continue
                    seen.add(s)
                    crit: list[str] = []
                    if score_ok:
                        crit.append("score")
                    if pct is not None and pct > 0:
                        crit.append("uptrend")
                    if look == "EXT":
                        crit.append("ext")
                    if rvol_ok:
                        crit.append("rvol")
                    if not crit:
                        crit.append("trending")
                    chg_s = f" chg {pct:+.1f}%" if pct is not None else ""
                    ext_s = " EXT" if look == "EXT" else ""
                    reason = f"trending{ext_s} score {score:.1f}{chg_s}"
                    tr_row = {
                        "symbol": s,
                        "trending_score": round(score, 2),
                        "score": round(score, 2),
                        "reason": reason[:48],
                        "agreement": True,
                        "source": "trending",
                        "pct_change": pct,
                        "rvol": rvol,
                        "look_reason": look or None,
                        "price": r.get("price"),
                        "dollar_volume": (
                            float(r["vol_session"]) * float(r["price"])
                            if r.get("vol_session") and r.get("price")
                            else None
                        ),
                        "criteria": crit,
                    }
                    if isinstance(seed_ind, dict):
                        tr_row["indicator"] = seed_ind
                    rows.append(tr_row)
                    if len([x for x in rows if x.get("source") == "trending"]) >= max(1, n):
                        break
        except Exception:
            pass

    # Alpaca movers. The only seed that is not sentiment: it ranks what moved
    # and what traded, so it fails differently from Stocktwits heat, a Discord
    # mention or a research thesis. movers_screener.py has already dropped
    # warrants and applied the price band — the filtering that needs a network
    # call belongs in the producer, not in a poll that runs every few seconds.
    #
    # rvol may legitimately be None here: the producer refuses to divide by a
    # dormant 20-day average (QNRX printed 1281x against one), and an unknown
    # ratio must not read as a big one. A None simply makes no rvol claim.
    if cfg.get("ai_watch_seed_movers", True):
        try:
            path = ROOT / "movers_stocks.json"
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            mv_rows = raw.get("rows") or []
            try:
                max_age = float(cfg.get("ai_movers_max_age_sec", 900.0) or 0.0)
            except (TypeError, ValueError):
                max_age = 900.0
            age = time.time() - float(raw.get("ts") or 0)
            # A frozen file is worse than no file: it seeds this morning's
            # movers into this afternoon's book. Absence over a stale claim.
            if max_age > 0 and raw.get("ts") and age > max_age:
                mv_rows = []
            if isinstance(mv_rows, list):
                n = int(cfg.get("ai_watch_seed_movers_n", 12) or 12)
                try:
                    mv_min_pct = float(
                        cfg.get("ai_watch_movers_min_pct_change", 10.0) or 10.0)
                except (TypeError, ValueError):
                    mv_min_pct = 10.0
                mv_desk, mv_tr = ((None, None)
                                  if not cfg.get("ai_watch_movers_enrich", True)
                                  else _live_quote_map())
                for r in mv_rows:
                    if not isinstance(r, dict):
                        continue
                    s = str(r.get("symbol") or "").upper().strip()
                    if not s or not s[0].isalpha():
                        continue
                    if s in seen:
                        _note_proposal_overlap(
                            "movers", s,
                            extra={
                                "price": r.get("price"),
                                "pct": r.get("pct_change"),
                                "rvol": r.get("rvol"),
                            },
                        )
                        continue
                    # Defense in depth: movers_screener already drops these,
                    # but a stale movers_stocks.json must not re-admit them.
                    if is_levered_etp(s):
                        continue
                    # Live quote first, the file second. The file's numbers are
                    # as old as the last producer write, and the gates below
                    # decide on them — so a name that popped at 09:35 and has
                    # since faded must be judged on what it is worth NOW, not
                    # on the moment it earned its place in the ranking.
                    live = (mv_desk or {}).get(s) or {}
                    tr = (mv_tr or {}).get(s) or {}
                    px_src = r.get("price")
                    pct_src = r.get("pct_change")
                    src_used = "file"
                    for cand in (live, tr):
                        if not cand:
                            continue
                        c_px = cand.get("price")
                        c_pct = _pct_change_value(cand.get("pct_change"))
                        if c_px is not None and c_pct is not None:
                            px_src, pct_src = c_px, c_pct
                            src_used = "desk" if cand is live else "trending"
                            break
                    if not _price_under_cap(px_src, max_price):
                        _note_seed_drop("movers", s, "price_cap", price=px_src)
                        continue
                    try:
                        mv_min_px = float(
                            cfg.get("ai_watch_movers_min_price", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        mv_min_px = 0.0
                    try:
                        px_f = float(px_src) if px_src is not None else None
                    except (TypeError, ValueError):
                        px_f = None
                    if mv_min_px > 0 and (px_f is None or px_f < mv_min_px):
                        _note_seed_drop(
                            "movers", s, "below_min_price", price=px_src)
                        continue
                    pct = _pct_change_value(pct_src)
                    if pct is None or pct < mv_min_pct:
                        _note_seed_drop(
                            "movers", s, "pct_low", pct=pct,
                            file_pct=_pct_change_value(r.get("pct_change")))
                        continue
                    # rvol is NOT enriched, deliberately. The producer computes
                    # it from SIP daily bars on both sides; the desk's reading
                    # is IEX. They are different statistics that both render as
                    # "x", and swapping one for the other would build a ratio
                    # out of two feeds — the mismatch that makes a volume ratio
                    # meaningless, and one this screener already walked into
                    # once.
                    rvol = None
                    if r.get("rvol") is not None:
                        try:
                            rvol = float(r["rvol"])
                        except (TypeError, ValueError):
                            rvol = None
                    # Known-thin tape: movers use a lower floor than the desk
                    # general min_rvol, and a hot day-move waives it so
                    # BIAF/LABX-class +20% names reach the book (2026-09-04).
                    thin_why, seed_ind = seed_rvol_gate(
                        s, rvol, pct, cfg, source="movers",
                        row=r, indicators=_seed_inds)
                    if thin_why == "thin_rvol":
                        _note_seed_drop("movers", s, "thin_rvol",
                                        pct=pct, rvol=rvol)
                        continue
                    try:
                        mv_min_dv = float(
                            cfg.get("ai_watch_movers_min_dollar_volume", 0.0)
                            or 0.0)
                    except (TypeError, ValueError):
                        mv_min_dv = 0.0
                    dvol = r.get("dollar_volume")
                    try:
                        dvol_f = float(dvol) if dvol is not None else None
                    except (TypeError, ValueError):
                        dvol_f = None
                    if dvol_f is None and px_f is not None:
                        # Recover $vol from share volume when the file only
                        # carries day volume.
                        for vk in ("vol_session", "day_vol", "volume"):
                            try:
                                sh = float(r.get(vk)) if r.get(vk) is not None else None
                            except (TypeError, ValueError):
                                sh = None
                            if sh is not None and sh > 0:
                                dvol_f = sh * px_f
                                break
                    if mv_min_dv > 0 and (dvol_f is None or dvol_f < mv_min_dv):
                        _note_seed_drop(
                            "movers", s, "thin_dollar_volume",
                            dollar_volume=dvol_f, price=px_src)
                        continue
                    seen.add(s)
                    row = dict(r)
                    row["symbol"] = s
                    row["source"] = "movers"
                    row["rvol"] = rvol
                    row["pct_change"] = pct
                    row["price"] = px_src
                    row["quote_src"] = src_used
                    if dvol_f is not None:
                        row["dollar_volume"] = dvol_f
                    if isinstance(seed_ind, dict):
                        row["indicator"] = seed_ind
                    rows.append(row)
                    if len([x for x in rows
                            if x.get("source") == "movers"]) >= max(1, n):
                        break
        except Exception:
            pass

    # AI Research boards. No numeric gate here on purpose: a research row is a
    # thesis, and the board has already been through the research pass's own
    # filters. Price / day-change come from Momentum desk first, then Trending
    # file — without that enrichment every research name fails no_price /
    # not_uptrend even when the thesis is live on the board.
    if cfg.get("ai_watch_seed_research", True):
        try:
            n = max(1, int(cfg.get("ai_watch_seed_research_n", 12) or 12))
            # Morning flood: seat every Trader Bro / research board row.
            if flood:
                n = max(n, 10_000)
            # Shared with the movers seed — one implementation, so the two
            # cannot drift into two different ideas of "the live price".
            desk_rows, tr_by = _live_quote_map()
            added = 0
            for r in research_candidate_rows():
                if added >= n:
                    break
                s = str(r.get("symbol") or "").upper().strip()
                if not s:
                    continue
                if is_levered_etp(s):
                    continue
                live = desk_rows.get(s) or {}
                tr = tr_by.get(s) or {}
                # Prefer live desk quote; fall back to trending snapshot.
                px_src = live.get("price") if live.get("price") is not None else tr.get("price")
                pct_src = (
                    live.get("pct_change")
                    if live.get("pct_change") is not None
                    else tr.get("pct_change")
                )
                rvol_src = live.get("rvol") if live.get("rvol") is not None else tr.get("rvol")
                if not _price_under_cap(px_src, max_price):
                    continue
                try:
                    px = float(px_src) if px_src is not None else None
                except (TypeError, ValueError):
                    px = None
                # Morning flood: missing price fail-closed (do not seat).
                if flood and px is None:
                    continue
                pct_f = _pct_change_value(pct_src)
                # Direction is decided ONCE, by ai_watch_require_uptrend in
                # the inclusion gate — not here as well.
                #
                # This used to drop any research name that was red on the day,
                # and it was hardcoded rather than a knob, so it survived the
                # operator turning every day-change floor off on 2026-08-28.
                # Six of the seven names on the research panel that afternoon
                # were negative — PURR -5.66%, ASST -4.63%, FIG -3.41%, SRPT
                # -2.18%, BULL -1.46% — and all six were dropped before the
                # gate ever saw them, leaving one research candidate.
                #
                # Momentum and trending are momentum sources, where the sign
                # is part of the signal. Research is a THESIS list: "Q2 EPS
                # $1.38 vs est, guidance raised" does not stop being a thesis
                # because the stock is red today. Refusing the whole list on
                # the day's sign throws away the reason the source exists, and
                # a filter that lives in code rather than config cannot be
                # seen or switched off.
                try:
                    dvol = float(live.get("day_vol")) if live.get("day_vol") is not None else None
                except (TypeError, ValueError):
                    dvol = None
                if dvol is None and tr.get("vol_session") is not None and px:
                    try:
                        dvol = float(tr.get("vol_session"))
                    except (TypeError, ValueError):
                        dvol = None
                # Known-thin tape never occupies a research slot. Unknown
                # abstains — a thesis with no rvol yet still seeds. Hot-move
                # waive applies so a green research name is not wiped by
                # borderline rvol the same way movers were.
                try:
                    rv = float(rvol_src) if rvol_src is not None else None
                except (TypeError, ValueError):
                    rv = None
                thin_why, seed_ind = seed_rvol_gate(
                    s, rv, pct_f, cfg, source="research",
                    row=live or tr, indicators=_seed_inds)
                if thin_why == "thin_rvol":
                    _note_seed_drop("research", s, "thin_rvol",
                                    pct=pct_f, rvol=rv)
                    continue
                if s in seen:
                    # Mom/movers already own the row — do not let research
                    # re-claim a stream-ready momentum seat. Still attribute.
                    _note_proposal_overlap(
                        str(r.get("source") or "research"),
                        s,
                        extra={
                            "price": px,
                            "pct": pct_f,
                            "rvol": rvol_src,
                        },
                    )
                    continue
                seen.add(s)
                added += 1
                row = dict(r)
                row.update({
                    "price": px,
                    "pct_change": pct_f,
                    "rvol": rvol_src,
                    "dollar_volume": (dvol * px) if (dvol and px) else None,
                    "criteria": ["research"],
                })
                if isinstance(seed_ind, dict):
                    row["indicator"] = seed_ind
                if flood:
                    row["morning_flood"] = 1
                rows.append(row)
        except Exception:
            pass

    # Trader Bro call-outs. Last seed: a call is the weakest evidence on this
    # list, so it only ever contributes symbols nothing else already named.
    if cfg.get("ai_watch_seed_bb_live", True):
        try:
            n = max(1, int(cfg.get("ai_watch_seed_bb_live_n", 6) or 6))
            fresh = float(cfg.get("ai_watch_bb_live_fresh_sec", 900.0) or 0.0)
            added = 0
            for _, r in _bb_live_from_dashboard(max_price, fresh):
                s = str(r.get("symbol") or "").upper().strip()
                if not s:
                    continue
                if s in seen:
                    # Another source already named it, and a call must not
                    # take ownership of a row it is the weakest evidence
                    # for. But dropping the call silently is why the source
                    # looked dead: DAIC was called by Trader Bro on 8/26,
                    # momentum happened to name it in the same pass, and the
                    # row reached the book as `momentum` with criteria
                    # ['big_move', 'uptrend'] — nothing on it said a human
                    # had called it out. Ownership and visibility are
                    # different questions, so tag the criteria and leave the
                    # source alone.
                    owner_src = None
                    for prev in rows:
                        if str(prev.get("symbol") or "").upper().strip() != s:
                            continue
                        crit = list(prev.get("criteria") or [])
                        if "bro_call" not in crit:
                            crit.append("bro_call")
                            prev["criteria"] = crit
                        owner_src = str(prev.get("source") or "") or None
                        break
                    _note_proposal_overlap(
                        str(r.get("source") or "bb_live"),
                        s,
                        row=r,
                        owner=owner_src,
                    )
                    continue
                # Cap NEW symbols only. `continue` rather than `break` so a
                # call further down the list can still tag a row above it.
                if added >= n:
                    continue
                seen.add(s)
                added += 1
                rows.append(r)
        except Exception:
            pass

    # Observe-only: every shortlist survivor is a seed kept proposal.
    for r in rows:
        try:
            sym = str(r.get("symbol") or "").upper().strip()
            if not sym:
                continue
            src = str(r.get("source") or "").strip().lower() or "unknown"
            _note_proposal(
                stage="seed",
                symbol=sym,
                proposer=src,
                decision="kept",
                reason=None,
                owner=None,
                row=r if isinstance(r, dict) else None,
                cfg=cfg,
            )
        except Exception:
            pass

    # Extreme movers on the desk that never made the shortlist must still
    # carry a reason (price_cap / levered / shortlist_cap / …). Silent miss
    # is the failure mode the operator called out for +100% names.
    try:
        _audit_extreme_desk_movers(
            cfg,
            seated={str(r.get("symbol") or "").upper() for r in rows if r},
            max_price=max_price,
        )
    except Exception:
        pass

    try:
        rows = apply_day_roster(rows, cfg)
    except Exception:
        pass
    _MOMENTUM_SYMS.clear()
    _MOMENTUM_SYMS.update(
        str(r.get("symbol") or "").upper() for r in rows
        if isinstance(r, dict) and str(r.get("source") or "").lower().startswith("momentum"))
    return rows


# Day roster: today's Movers / Trending / Research nominations, kept eligible
# after the rotating top-N list drops them. On 2026-09-24 13:10-15:40, 79 of
# 250 in-band -50 crosses (32%) were on names a source had named earlier that
# day but no longer listed at the cross (30 had been seated); 6 crosses opened.
# The book rebuilds from the current lists every 2 s, so it forgot them.
_DAY_ROSTER: dict[str, dict] = {}
_DAY_ROSTER_KEY = {"day": ""}
_ROSTER_SOURCES = frozenset({"movers", "trending", "research", "agy", "xai", "grok"})


def _roster_eligible(row: dict) -> bool:
    src = str(row.get("source") or "").strip().lower()
    return src in _ROSTER_SOURCES or "research" in (row.get("criteria") or [])


def apply_day_roster(rows: list[dict], cfg: dict | None,
                     now: float | None = None) -> list[dict]:
    """Add today's earlier source nominations that the lists no longer carry.

    ``ai_watch_day_roster`` (default off). A roster row is the name's last
    seed row with a CURRENT quote from the desk / trending file; with no
    current quote its price is None, so inclusion refuses it (no stale
    price). Its old indicator is dropped so inclusion reads the live engine.
    Every admission gate and the arm still apply. At most
    ``ai_watch_day_roster_max`` roster rows, most recently listed first.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not bool(cfg.get("ai_watch_day_roster", False)):
        return rows
    t = float(now if now is not None else time.time())
    day = _et_now(t).strftime("%Y-%m-%d")
    if _DAY_ROSTER_KEY["day"] != day:
        _DAY_ROSTER.clear()
        _DAY_ROSTER_KEY["day"] = day
    have = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        s = str(r.get("symbol") or "").upper().strip()
        if not s:
            continue
        have.add(s)
        if _roster_eligible(r):
            _DAY_ROSTER[s] = {"row": dict(r), "last": t}
    try:
        cap = int(cfg.get("ai_watch_day_roster_max", 40) or 0)
    except (TypeError, ValueError):
        cap = 40
    extra = sorted(((v["last"], s, v["row"]) for s, v in _DAY_ROSTER.items()
                    if s not in have), key=lambda x: x[0], reverse=True)[:max(0, cap)]
    if not extra:
        return rows
    desk_rows, tr_by = _live_quote_map()
    out = list(rows)
    for last, s, old in extra:
        row = dict(old)
        live, tr = desk_rows.get(s) or {}, tr_by.get(s) or {}
        px = live.get("price") if live.get("price") is not None else tr.get("price")
        row["price"] = _f_or_none(px)
        pct_src = live.get("pct_change") if live.get("pct_change") is not None else tr.get("pct_change")
        pct = _pct_change_value(pct_src)
        for k in ("pct_change", "pct"):
            if k in row:
                row[k] = pct
        row.pop("indicator", None)
        row["criteria"] = list(row.get("criteria") or []) + ["day_roster"]
        row["day_roster"] = True
        row["roster_last_listed"] = last
        out.append(row)
    return out


# symbol -> consecutive qualifying polls, for admission dwell.
_admit_ticks: dict[str, int] = {}

# symbol -> monotonic ts of the last engine push (debounce; see below).
_pushed_at: dict[str, float] = {}

# Last sync's rejections, for the wire (why a name is NOT on the book).
_last_rejected: list[dict] = []
# Seed-path drops (before inclusion): source -> reason -> count / samples.
# Cleared at the start of each desk_candidate_rows pass; snapshotted into
# admit_funnel.json so a full movers panel with an empty book is diagnosable.
_seed_drop_counts: dict[str, dict[str, int]] = {}
_seed_drop_samples: dict[str, list[dict]] = {}
_SEED_DROP_SAMPLE_CAP = 8


def last_rejected() -> list[dict]:
    """Most recent inclusion-gate rejections: [{symbol, reason, criteria}]."""
    return list(_last_rejected)


def _clear_seed_drops() -> None:
    _seed_drop_counts.clear()
    _seed_drop_samples.clear()


def _note_proposal(
    *,
    stage: str,
    symbol: str,
    proposer: str,
    decision: str,
    reason: str | None = None,
    owner: str | None = None,
    row: dict | None = None,
    extra: dict | None = None,
    cfg: dict | None = None,
) -> None:
    """Observe-only proposal ledger write. Fail-open. Never raises."""
    try:
        cfg_l = cfg if isinstance(cfg, dict) else _push_cfg()
        if not bool(cfg_l.get("ai_proposal_ledger_enabled", True)):
            return
        import proposal_ledger as _pl
        _pl.log_proposal(
            stage=stage,
            symbol=symbol,
            proposer=proposer,
            decision=decision,
            reason=reason,
            owner=owner,
            row=row,
            extra=extra,
            cfg=cfg_l,
        )
    except Exception:
        pass


def _note_proposal_overlap(
    proposer: str,
    symbol: str,
    *,
    row: dict | None = None,
    extra: dict | None = None,
    owner: str | None = None,
) -> None:
    """Second source proposed a symbol already claimed this pass — attribute it.

    Does not change shortlist ownership. Counts as a seed ``kept`` proposal for
    scorecard attribution (passed seed filters; lost first-wins seat).
    """
    own = owner
    if own is None and isinstance(row, dict):
        own = str(row.get("source") or "") or None
    _note_proposal(
        stage="seed",
        symbol=symbol,
        proposer=proposer,
        decision="kept",
        reason=None,
        owner=own,
        row=row,
        extra=extra,
    )


def _note_seed_drop(
    source: str,
    symbol: str,
    reason: str,
    **extra,
) -> None:
    src = str(source or "seed").strip().lower() or "seed"
    why = str(reason or "unknown").strip() or "unknown"
    bucket = _seed_drop_counts.setdefault(src, {})
    bucket[why] = int(bucket.get(why) or 0) + 1
    samples = _seed_drop_samples.setdefault(src, [])
    if len(samples) < _SEED_DROP_SAMPLE_CAP:
        row = {"symbol": str(symbol or "").upper(), "reason": why, "source": src}
        for k, v in extra.items():
            if v is not None:
                row[k] = v
        samples.append(row)
    # Observe-only refused-side ledger (uncapped). Fail-open.
    try:
        cfg_l = _push_cfg()
        if bool(cfg_l.get("ai_admit_ledger_enabled", True)) and bool(
            cfg_l.get("ai_admit_ledger_seed", True)
        ):
            import admit_ledger as _al
            feat = {k: v for k, v in extra.items() if v is not None}
            _al.log_refuse(
                stage="seed",
                symbol=symbol,
                reason=why,
                source=src,
                extra=feat or None,
                cfg=cfg_l,
            )
    except Exception:
        pass
    # Attributed proposal ledger (state-change + heartbeat). Additive.
    feat = {k: v for k, v in extra.items() if v is not None}
    _note_proposal(
        stage="seed",
        symbol=symbol,
        proposer=src,
        decision="dropped",
        reason=why,
        owner=None,
        extra=feat or None,
    )


def seed_drop_snapshot() -> dict:
    """Copy of the last desk_candidate_rows seed-drop tallies."""
    return {
        "counts": {s: dict(c) for s, c in _seed_drop_counts.items()},
        "samples": {s: list(v) for s, v in _seed_drop_samples.items()},
    }


def _seed_drop_reason_for(symbol: str) -> tuple[str | None, float | None, float | None]:
    """Latest seed-drop reason/pct/price for *symbol* this sync, if any."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None, None, None
    for samples in _seed_drop_samples.values():
        for row in reversed(samples or []):
            if str(row.get("symbol") or "").upper() != sym:
                continue
            return (
                str(row.get("reason") or "") or None,
                _f_or_none(row.get("pct")),
                _f_or_none(row.get("price")),
            )
    return None, None, None


def _audit_extreme_desk_movers(
    cfg: dict,
    *,
    seated: set[str],
    max_price: Any = None,
) -> None:
    """Ensure every desk name ≥ extreme_move_pct has a seed-drop reason if off shortlist.

    Classifies leftovers the seed loops never touched (or silently skipped
    before logging existed). Fail-open; never raises into trading.
    """
    floor = extreme_move_pct(cfg)
    if floor <= 0:
        return
    seated_u = {str(s or "").upper() for s in (seated or set()) if s}
    try:
        tickers = _dashboard_tickers()
    except Exception:
        tickers = []
    for r in tickers:
        if not isinstance(r, dict):
            continue
        s = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
        if not s or not s[0].isalpha():
            continue
        if s in seated_u:
            continue
        pct = _pct_change_value(r.get("pct_change"))
        if pct is None or pct + 1e-12 < floor:
            continue
        why, _, _ = _seed_drop_reason_for(s)
        if why:
            continue
        # Classify what the seed path would have done.
        if is_levered_etp(s):
            _note_seed_drop("momentum", s, "levered_etp",
                            pct=pct, price=r.get("price"))
            continue
        if not _price_under_cap(r.get("price"), max_price):
            _note_seed_drop("momentum", s, "price_cap",
                            pct=pct, price=r.get("price"))
            continue
        if pct <= 0:
            _note_seed_drop("momentum", s, "not_uptrend",
                            pct=pct, price=r.get("price"))
            continue
        try:
            rv = float(r.get("rvol")) if r.get("rvol") is not None else None
        except (TypeError, ValueError):
            rv = None
        thin, _ind = seed_rvol_gate(
            s, rv, pct, cfg, source="momentum",
            row=r, indicators=None)
        if thin:
            _note_seed_drop("momentum", s, thin, pct=pct, rvol=rv,
                            price=r.get("price"))
            continue
        # Cleared seed filters but still not shortlisted — capacity / claim.
        _note_seed_drop("momentum", s, "shortlist_miss",
                        pct=pct, price=r.get("price"), rvol=rv)


def extreme_off_book_rows(
    *,
    kept_symbols: list[str] | None = None,
    rejected: list[dict] | None = None,
    cfg: dict | None = None,
    limit: int = 12,
) -> list[dict]:
    """Operator-facing list: extreme day-movers not kept, each with a reason."""
    floor = extreme_move_pct(cfg)
    if floor <= 0:
        return []
    kept = {str(s or "").upper() for s in (kept_symbols or []) if s}
    out: list[dict] = []
    seen: set[str] = set()

    # Inclusion rejects with extreme pct.
    for r in rejected or []:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym or sym in kept or sym in seen:
            continue
        pct = _pct_change_value(r.get("pct_change"))
        if pct is None:
            pct = _pct_change_value(r.get("pct"))
        if pct is None or pct + 1e-12 < floor:
            continue
        why = str(r.get("reason") or "inclusion_reject").strip() or "inclusion_reject"
        seen.add(sym)
        out.append({
            "symbol": sym,
            "pct": round(float(pct), 2),
            "reason": why,
            "stage": "inclusion",
            "source": str(r.get("source") or "") or None,
        })

    # Seed-drop samples at/above the floor.
    for src, samples in (_seed_drop_samples or {}).items():
        for row in samples or []:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper().strip()
            if not sym or sym in kept or sym in seen:
                continue
            pct = _f_or_none(row.get("pct"))
            if pct is None or pct + 1e-12 < floor:
                continue
            seen.add(sym)
            out.append({
                "symbol": sym,
                "pct": round(float(pct), 2),
                "reason": str(row.get("reason") or "seed_drop"),
                "stage": "seed",
                "source": str(row.get("source") or src or "") or None,
                "price": _f_or_none(row.get("price")),
            })

    out.sort(key=lambda r: -(r.get("pct") or 0.0))
    return out[: max(0, int(limit or 0))]


def _push_cfg() -> dict:
    try:
        from config import load_config
        return load_config() or {}
    except Exception:
        return {}


# Symbols the momentum seed is currently proposing (for the push filter,
# which sees symbols only). Refreshed by desk_candidate_rows.
_MOMENTUM_SYMS: set[str] = set()


def _min_price_for(source, cfg: dict | None, default: float = 1.0) -> float:
    """The band floor for this row's source.

    ai_watch_momentum_min_price (unset = the shared ai_watch_min_price) lets
    the curated Discord momentum names be tested from $1 while Movers,
    Trending and Research keep the shared floor. 2026-09-25, user request.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    src = str(source or "").strip().lower()
    if src.startswith("momentum") and cfg.get("ai_watch_momentum_min_price") is not None:
        try:
            return max(0.0, float(cfg.get("ai_watch_momentum_min_price")))
        except (TypeError, ValueError):
            pass
    try:
        return float(cfg.get("ai_watch_min_price", default) or 0.0)
    except (TypeError, ValueError):
        return default


def _spread_gate_max(source, cfg: dict | None) -> float:
    """SIP spread ceiling for this source (0 = no spread gate).

    ai_watch_momentum_spread_exempt: the $1-5 Discord momentum names cannot
    pass a 0.20% gate on tick size alone (1 cent on $3 is 0.33%); exempt
    them for the momentum test so their real cost can be measured. User
    request 2026-09-25. Other sources keep the gate.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if (str(source or "").strip().lower().startswith("momentum")
            and bool(cfg.get("ai_watch_momentum_spread_exempt", False))):
        return 0.0
    try:
        return float(cfg.get("ai_watch_max_sip_spread_pct", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _push_band_filter(symbols: list[str]) -> list[str]:
    """With the day roster on, spend engine / Finnhub slots on tradeable names.

    2026-09-24 afternoon: the engine sat at its 44-name push cap and the
    ticker log at 46-56 against Finnhub's 50, while ~10 of the 44 were
    outside the $20-$100 band and could never be bought; roster names then
    had no slot and no tape. Drops a name only when its current price is
    KNOWN and out of band; seated names (incl. held) and unpriced names
    (research seeds) are always kept.
    """
    cfg = _push_cfg()
    slot_pri = bool(cfg.get("ai_watch_slot_priority", False))
    if not (bool(cfg.get("ai_watch_day_roster", False)) or slot_pri):
        return symbols
    lo = _f_or_none(cfg.get("ai_watch_min_price")) or 0.0
    lo_mom = _min_price_for("momentum", cfg, default=lo)
    hi = _f_or_none(cfg.get("ai_max_price")) or 0.0
    try:
        watch = load_watch() or {}
    except Exception:  # noqa: BLE001
        watch = {}
    desk_rows, tr_by = _live_quote_map()
    keep = []
    for s in symbols:
        if s in watch:
            keep.append(s)
            continue
        r = desk_rows.get(s) or tr_by.get(s) or {}
        px = _f_or_none(r.get("price"))
        floor = lo_mom if s in _MOMENTUM_SYMS else lo
        if px is not None and ((floor > 0 and px + 1e-12 < floor) or (hi > 0 and px >= hi)):
            continue
        if slot_pri and _slot_unseatable(s, cfg):
            continue
        keep.append(s)
    return keep


# Book-server rank per candidate, refreshed each book sync while
# ai_watch_slot_priority is on; orders who gets the engine's free slots.
_SLOT_RANK: dict[str, float] = {}


def _slot_unseatable(sym: str, cfg: dict) -> bool:
    """ai_watch_slot_priority: a KNOWN reason the door would refuse this name.

    Cached values only (no requests on the push path); unknown never drops.
    Gap below -ai_watch_gap_down_block_pct, or 16-min-old SIP spread above
    ai_watch_max_sip_spread_pct — the same numbers admit_arm_gates uses.
    """
    block = _f_or_none(cfg.get("ai_watch_gap_down_block_pct")) or 0.0
    hit = _GAP_CACHE.get(sym)
    if block > 0 and hit and hit[0] is not None and float(hit[0]) < -block:
        return True
    max_sp = _spread_gate_max("momentum" if sym in _MOMENTUM_SYMS else "", cfg)
    hit = _SIP_SPREAD_CACHE.get(sym)
    if max_sp > 0 and hit and hit[0] is not None and float(hit[0]) > max_sp:
        return True
    return False


def push_candidates_to_engine(symbols: list[str]) -> dict:
    """Ask the signal engine to start computing indicators for *symbols*.

    The engine only evaluates its own watchlist, which is fed by Discord
    mentions — so the trending names on this book had no indicator data at all
    and an indicator gate would reject everything.

    Continuously re-assert EVERY live entry_watch / entry_book / seed symbol
    onto the dashboard ticker log with src=book (book-wide — not a hardcoded
    list). A delta-only push left names "known" in signal_state after an
    ai_trader-only restart while the ticker log briefly emptied, so the engine
    lost on_desk, expired them, and Finnhub unsubscribed — stale_quote arms.
    """
    # Preserve caller order — desk_candidate_rows ranks by score, and the cap
    # below truncates. Alphabetising first meant a capped push sent the
    # A-names rather than the best candidates, so the strongest setups could
    # sit on the book with no indicator data and be rejected as
    # "indicators_faded" — indistinguishable from a real fade.
    seen: set[str] = set()
    wanted: list[str] = []
    for s in symbols:
        t = str(s or "").upper().strip()
        if not t.isalpha() or not (2 <= len(t) <= 5) or t in seen:
            continue
        seen.add(t)
        wanted.append(t)
    wanted = _push_band_filter(wanted)
    if not wanted:
        return {"pushed": 0, "known": 0}
    known = set(_engine_indicator_map())
    missing = [s for s in wanted if s not in known]

    # Hard cap applies to net-new engine adds only. Refresh of already-known
    # book names always proceeds so the ticker log stays seeded.
    try:
        cap = int(_push_cfg().get("ai_watch_engine_push_max", 32) or 0)
    except (TypeError, ValueError):
        cap = 32
    capped = False
    if cap > 0 and missing:
        room = max(0, cap - len(known))
        if room <= 0:
            missing = []
            capped = True
        elif len(missing) > room:
            try:
                watch = load_watch() or {}
            except Exception:
                watch = {}

            def _admit_ts(sym: str) -> float:
                rec = watch.get(sym) if isinstance(watch, dict) else None
                if not isinstance(rec, dict):
                    return 0.0
                try:
                    return float(rec.get("admit_ts") or 0.0)
                except (TypeError, ValueError):
                    return 0.0

            if bool(_push_cfg().get("ai_watch_slot_priority", False)):
                # Seated first (newest admit), then the book-server runway rank.
                missing = sorted(missing, key=lambda s: (
                    _admit_ts(s) > 0, _admit_ts(s), _SLOT_RANK.get(s, float("-inf"))),
                    reverse=True)[:room]
            else:
                missing = sorted(missing, key=_admit_ts, reverse=True)[:room]

    # Debounce. Missing names: wait ~2 scan intervals (engine catch-up).
    # Already-known book names: re-assert every ~30s so a blip cannot leave
    # them off the ticker log / Finnhub priority set for long.
    now = time.monotonic()
    try:
        hold_missing = float(_push_cfg().get("scan_interval_sec", 60) or 60) * 2.0
    except (TypeError, ValueError):
        hold_missing = 120.0
    hold_refresh = 30.0

    to_push: list[str] = []
    for m in missing:
        if m not in _pushed_at or (now - _pushed_at[m]) > hold_missing:
            to_push.append(m)
    for s in wanted:
        # Refresh is for names the engine ALREADY knows. Testing `s in missing`
        # leaked every name the cap had just truncated out of `missing` back
        # into the push, so ai_watch_engine_push_max never bound: the cap
        # dropped a name from the net-new list and this loop pushed it anyway.
        # That is the subscription budget the cap exists to protect (~50
        # concurrent Finnhub WS subs), and overflow is silent — no trades, no
        # forming bars, no indicator state, refused later as a fade.
        if s not in known:
            continue
        if s not in _pushed_at or (now - _pushed_at[s]) > hold_refresh:
            to_push.append(s)
    # Dedupe preserve order
    seen_p: set[str] = set()
    ordered: list[str] = []
    for t in to_push:
        if t not in seen_p:
            seen_p.add(t)
            ordered.append(t)
    to_push = ordered

    if not to_push:
        out = {"pushed": 0, "known": len(known), "debounced": True}
        if capped:
            out["capped"] = True
        return out
    for m in to_push:
        _pushed_at[m] = now
    try:
        with _dash_urlopen(
            f"{DASHBOARD_URL}/api/tickers/add-bulk",
            # src="book" marks these as data subscriptions, not momentum
            # candidates — the Momentum panel filters them out so pushing the
            # whole book does not bury the panel it seeds from.
            data=json.dumps({"tickers": to_push, "src": "book"}).encode("utf-8"),
            method="POST",
        ):
            pass
        out = {"pushed": len(to_push), "known": len(known)}
        if capped:
            out["capped"] = True
        return out
    except Exception:
        out = {"pushed": 0, "known": len(known), "error": True}
        if capped:
            out["capped"] = True
        return out


_ENGINE_RT_CACHE: tuple = (0.0, {}, 0.0)  # (mono_ts, tickers, file_mtime)
_ENGINE_RT_TTL = 0.5  # seconds — signal_state.json write cadence ~1s


def _engine_rt_print(symbol: str) -> tuple[float, float] | None:
    """Dated engine rt_price/rt_price_age_sec, or None.

    Dashboard merge can still lose a young socket print to REST/undated
    between loops (ASST/PPBT Sep2: realtime MACD while quote path REST).
    decision_price/live_print must see the engine tape directly so a known
    young age cannot be displaced by an undated REST ask.

    Age is advanced by time since the signal_state.json mtime: the file stores
    age-at-write, and between engine writes a raw read understates true age.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None
    global _ENGINE_RT_CACHE
    now = time.time()
    _cache = _ENGINE_RT_CACHE
    ts = float(_cache[0] or 0.0)
    tickers = _cache[1] if len(_cache) > 1 else {}
    if not isinstance(tickers, dict):
        tickers = {}
    file_mtime = float(_cache[2] or 0.0) if len(_cache) >= 3 else 0.0
    if (now - ts) > _ENGINE_RT_TTL or not tickers:
        try:
            from pathlib import Path as _P
            raw = _P(__file__).resolve().parent.joinpath("signal_state.json")
            data = json.loads(raw.read_text(encoding="utf-8"))
            tickers = data.get("tickers") if isinstance(data, dict) else {}
            if not isinstance(tickers, dict):
                tickers = {}
            try:
                file_mtime = float(raw.stat().st_mtime)
            except Exception:
                file_mtime = now
            _ENGINE_RT_CACHE = (now, tickers, file_mtime)
        except Exception:
            return None
    sp = tickers.get(sym)
    if not isinstance(sp, dict):
        return None
    try:
        px = float(sp.get("rt_price") or 0)
        age = float(sp.get("rt_price_age_sec"))
    except (TypeError, ValueError):
        return None
    if px <= 0 or age < 0:
        return None
    # Age-at-write → age-now using file mtime as write clock.
    if file_mtime > 0:
        age = age + max(0.0, now - file_mtime)
    return px, age


def live_print(symbol: str) -> tuple[float, float | None] | None:
    """Dashboard tape print: ``(price, age_sec_or_None)``.

    ``price_age_sec`` is the observation age. ``price_ts`` is a write clock
    and must not be used for freshness. Age None means the desk has a number
    but cannot prove it is live — callers must not treat that as fresh.

    Young dated engine ``rt_*`` (age known and ≤ decision_max_age_sec) always
    wins for the decision last_ask — book-wide, not ticker-specific. A lagging
    dashboard merge / overlay print must not displace a live engine tape
    (GTLB Sep2 11:50 ET: eng≈2.9s while desk stale_tape≈19.9). When engine is
    older than the ceiling, it still wins over undated dash or a strictly
    older dash age. Arms stay gated by decision_max_age_sec unchanged.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None
    dash_px: float | None = None
    dash_age: float | None = None
    for r in _dashboard_tickers():
        if not isinstance(r, dict):
            continue
        if str(r.get("ticker") or r.get("symbol") or "").upper().strip() != sym:
            continue
        try:
            px = float(r.get("price") or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px <= 0:
            break
        try:
            dash_age = float(r.get("price_age_sec"))
        except (TypeError, ValueError):
            dash_age = None
        dash_px = px
        break
    eng = _engine_rt_print(sym)
    if eng is not None:
        epx, eage = eng
        try:
            ceiling = float(decision_max_age_sec(None))
        except Exception:
            ceiling = 15.0
        # Young dated engine always wins for decision last_ask.
        if eage <= ceiling:
            return epx, eage
        # Past ceiling: still prefer over undated dash or older dash.
        if dash_px is None or dash_age is None or eage < float(dash_age):
            return epx, eage
    if dash_px is not None:
        return dash_px, dash_age
    return None


def stream_quote(symbol: str) -> tuple[float, float] | None:
    """(last_trade_price, age_sec) from the real-time feed, or None.

    Source is the dashboard's ticker row, whose ``price`` is fed primarily by
    the Finnhub WebSocket (``_price_loop``) with Alpaca as fallback. We read it
    off the /api/state payload this module already fetches and caches rather
    than opening a second WS from this process: FINNHUB_STATE is per-process,
    and the free tier caps subscriptions at 50 symbols shared across the desk.
    Candidates get subscribed automatically because we push them into the
    engine's ticker list (see push_candidates_to_engine).

    Only returns when age is known. Unknown age is not "fresh".
    """
    got = live_print(symbol)
    if got is None or got[1] is None:
        return None
    return got[0], float(got[1])


def decision_max_age_sec(cfg: dict | None) -> float:
    """The tape-age ceiling, in seconds.

    cfg=None means "look it up", NOT "use the default". Every production
    caller of _row_tape_stale passes no cfg — _poller_blocked, derive_blocker
    and apply_tape_blocker have none in scope — so `(cfg or {})` silently
    resolved the operator's setting to the 8.0 literal. The knob was raised to
    30.0 in bot_config.json on 8/26 and never took effect anywhere.

    Cost, measured 2026-08-27 mid-session: gate age ran p50 14.0s, so 8s
    admitted 41.7% of rows where 30s admits 58.3%. A sixth of the book was
    being refused as "stale quote" against a ceiling nobody had chosen.
    load_config() is stamp-cached, so this is a stat() on the hot path.
    """
    if cfg is None:
        cfg = _push_cfg()
    try:
        v = float((cfg or {}).get("ai_watch_decision_max_age_sec", 8.0) or 8.0)
    except (TypeError, ValueError):
        v = 8.0
    return v if v > 0 else 8.0


def _ask_max_dev_pct(cfg: dict | None) -> float:
    """How far a REST ask may sit from the last print before it is disbelieved.

    Percent of the tape price. 5% is generous — a fast name genuinely moves
    between a print and a quote — while still catching the 2026-08-21 failures
    (USDE +12.9%, JUNS +8.3%) and leaving the honest ones alone (BKKT +1.0%,
    TGTX +0.7%). 0 disables the check.
    """
    try:
        v = float((cfg or {}).get("ai_decision_ask_max_dev_pct", 5.0))
    except (TypeError, ValueError):
        v = 5.0
    return max(0.0, v)


# Cross-checked IEX quote as a second fresh price source (barebones step 1).
# On 2026-09-25, 43% of RTH arm checks were refused tape_only; SIP had traded
# within 15 s on 99% of them — IEX sees a few percent of all trades, so a
# liquid name's last IEX PRINT ages while its IEX QUOTE keeps updating. The
# quote alone is unsafe (thin IEX books: p90 error 330 bp vs the SIP ask), but
# a quote no older than QUOTE_MAX_AGE_SEC that sits within QUOTE_PRINT_AGREE_BP
# of a print no older than QUOTE_PRINT_MAX_AGE_SEC cleared 60% of refusals at
# a median 0.0 bp / p90 3 bp error against the SIP ask
# (tools/studies/freshness_study.py, 123 refusals).
QUOTE_MAX_AGE_SEC = 5.0
QUOTE_PRINT_AGREE_BP = 30.0
QUOTE_PRINT_MAX_AGE_SEC = 60.0
FRESH_PRICE_SRCS = ("stream", "quote")


def price_src_fresh(src: str | None) -> bool:
    """True for a price source the arm may trust when young: a stream print,
    or a cross-checked quote. One predicate for every arm/seat gate."""
    return str(src or "").strip().lower() in FRESH_PRICE_SRCS


def cross_checked_quote(
    symbol: str,
    tape: tuple[float, float | None] | None,
    cfg: dict | None,
    now: float | None = None,
) -> tuple[float, str, float] | None:
    """``(ask, "quote", quote_age)`` when the cached IEX quote is young and
    agrees with a recent print; else None. Cache only — the poll primes one
    batched quote call for the whole book, so this adds no request."""
    cfg = cfg if isinstance(cfg, dict) else {}
    if not bool(cfg.get("ai_watch_quote_freshness", True)):
        return None
    if tape is None or not tape[0] or tape[0] <= 0 or tape[1] is None:
        return None
    if float(tape[1]) > QUOTE_PRINT_MAX_AGE_SEC:
        return None
    try:
        import ai_trading as gt
        hit = gt._cached_quote(symbol)
        q_age = gt.cached_quote_age_sec(symbol, now)
    except Exception:  # noqa: BLE001
        return None
    if not hit or not hit[0] or q_age is None or q_age > QUOTE_MAX_AGE_SEC:
        return None
    ask = float(hit[0])
    if ask <= 0 or abs(ask / float(tape[0]) - 1.0) * 1e4 > QUOTE_PRINT_AGREE_BP:
        return None
    return ask, "quote", float(q_age)


def decision_price(
    symbol: str,
    cfg: dict | None,
    now: float | None = None,
) -> tuple[float | None, str, float | None]:
    """Price used to arm or flatten — never a leftover ``last_ask``.

    Returns ``(price, src, age_sec)`` where src is ``stream``, ``rest``,
    ``stale_tape``, or ``none``.

    Fresh dashboard/engine tape (age ≤ max) wins: that is the print the
    operator sees. When a dated WS/engine trade EXISTS but is older than
    the ceiling, return ``stale_tape`` — never disguise it as ``rest``.
    Painting rest while Finnhub holds a trade made AEHG/AOUT look
    unsubscribed (need stream) on 2026-09-04 while SCAN showed the print.
    """
    max_age = decision_max_age_sec(cfg)
    tape = live_print(symbol)
    if tape is not None:
        px, age = tape
        if age is not None and age <= max_age and px > 0:
            return px, "stream", age
    q = cross_checked_quote(symbol, tape, cfg, now)
    if q is not None:
        return q
    ask_f = 0.0
    try:
        import ai_trading as gt
        ask = gt._latest_ask(symbol)
        ask_f = float(ask) if ask is not None else 0.0
    except Exception:
        ask_f = 0.0
    # Dated WS/engine tape present but old: honest stale_tape beats REST.
    # REST remains available only when there is no dated tape at all.
    if tape is not None and tape[0] and tape[0] > 0 and tape[1] is not None:
        return tape[0], "stale_tape", tape[1]
    if ask_f > 0:
        # Cross-check the REST ask against the last print before trusting it.
        # Nothing did, and on 2026-08-21 the quote on thin names ran far above
        # the tape: USDE asked 7.97 against 7.18 traded (+12.9%), JUNS 9.40
        # against 8.52 (+8.3%). Everything downstream is derived from this
        # number, so a bad one poisons the lot — the synth stop comes out at
        # ask x 0.95 and lands ABOVE the live print, which is why 62 of the
        # day's 84 entry_fail refusals read "tape $8.40 already through stop
        # $8.93". JUNS retried 34 times because the quote never corrected.
        # It also inflates spread_r, which is the record ai_max_spread_r is
        # about to be set from: JUNS 5.96R and USDE 4.96R are mostly this
        # artifact rather than genuinely 500%-wide books.
        #
        # A quote this far from the tape is not a wide market, it is a wrong
        # number. Fall through to stale_tape, which already must not arm.
        # 0 disables the check.
        # (Tape-without-age already returned above; this path is REST-only.)
        try:
            rest_age = gt.cached_quote_age_sec(symbol)
        except Exception:  # noqa: BLE001
            rest_age = None
        return ask_f, "rest", rest_age
    if tape is not None and tape[0] > 0:
        return tape[0], "stale_tape", tape[1]
    return None, "none", None


def apply_decision_price(rec: dict, cfg: dict | None, now: float) -> tuple[float, str, float | None]:
    """Stamp *rec* with a realtime decision print. ``(price, src, age)``."""
    px, src, age = decision_price(rec.get("symbol") or "", cfg, now)
    if px and px > 0:
        rec["last_ask"] = float(px)
        note_px_ring(rec, float(px), now)
        rec["last_ask_src"] = src
        rec["last_ask_age_sec"] = age
        # The quote's OWN unix time, derived from the age we just measured
        # against the clock that measured it: quote_ts = now - age, exactly.
        # Storing the timestamp rather than only the age is what lets any
        # later reader recompute a correct age instead of republishing a
        # number that was true once. signal_engine already does this for
        # rt_price_age_sec ("recomputed per write"); the watch record did not,
        # so every publish landing between a record rebuild and its next
        # pricing had no age to show and the row was labelled "no quote age"
        # — 5 of 11 rows at 12:23 ET, while the arm gate, which runs straight
        # after pricing, was seeing the real age the whole time.
        #
        # Only set when the age is provable. Unprovable must stay unprovable:
        # inventing a timestamp here is how a stale print comes to look fresh.
        # Kept in a symbol-keyed map, NOT on the record. poll_once rebuilds the
        # full record every cycle, so anything stamped on it is discarded
        # before the next publish can read it — which is why storing the
        # timestamp per-record changed nothing: rows still published
        # src="stream" with age=None, a pair both stream writers now make
        # impossible at write time. The clock has to outlive the record.
        _sym_k = str(rec.get("symbol") or "").upper().strip()
        # The age was measured at the wall clock of THIS call (the dashboard
        # reports it as of its response), not at `now`, the poll's start.
        # poll_once prices ~30 names in turn and ran 17-36 s on 2026-09-25, so
        # stamping `now - age` made every print look older by however long the
        # poll had been running: the desk logged a median 24 s where the
        # dashboard said 10.7 s and IEX had printed 6.7 s before, and 43% of
        # RTH arm checks were refused tape_only.
        _measured_at = time.time()
        if age is not None:
            rec["last_ask_ts"] = _measured_at - float(age)
            if _sym_k:
                _set_quote_ts(_sym_k, _measured_at - float(age))
        else:
            rec.pop("last_ask_ts", None)
            if _sym_k:
                _LAST_QUOTE_TS.pop(_sym_k, None)
        # After the clock is stamped: drop sticky tape-data refuses so the
        # file/paint cannot keep "stale quote" beside stream+young (BIAF/SNDG).
        if str(src or "").strip().lower() == "stream":
            clear_tape_data_block_if_stream_fresh(rec, cfg)
        rec["last_trade"] = float(px) if src in ("stream", "stale_tape") else rec.get("last_trade")
    return (float(px) if px else 0.0), src, age


def row_quote_age_sec(rec: dict, now: float | None = None) -> float | None:
    """Age of this record's quote, recomputed against the clock right now.

    Prefers ``last_ask_ts`` — the quote's own time — so the answer is correct
    whenever it is asked, not only at the instant of pricing. Falls back to the
    stored age for records written before the timestamp existed, and returns
    None when neither is available, because unprovable is a real answer here.
    """
    if not isinstance(rec, dict):
        return None
    ts = _num_or_none(rec.get("last_ask_ts"))
    if ts is None:
        # The record is rebuilt every poll, so its own stamp is routinely gone
        # by publish time. The symbol-keyed map outlives the rebuild.
        sym = str(rec.get("symbol") or "").upper().strip()
        if sym:
            ts = _num_or_none(_LAST_QUOTE_TS.get(sym))
    if ts is not None and ts > 0:
        return max(0.0, (time.time() if now is None else float(now)) - ts)
    return _num_or_none(rec.get("last_ask_age_sec"))


def refresh_arm_market_data(
    rec: dict,
    cfg: dict | None,
    now: float,
    *,
    gt: Any = None,
    sig: dict | None = None,
) -> tuple[float, str, float | None, float | None]:
    """Force latest quote + EXH/RSI when a watch becomes buy-ready.

    The poll's ``prime_quotes`` batch can be up to ``_QUOTE_TTL_SEC`` old, and
    the pre-place recheck used to re-read that same cache. Bust it, pull NBBO
    again, restamp the decision print and live indicators, then return
    ``(ask, src, age, bid)`` for a fresh ``should_arm_buy``.
    """
    if not isinstance(rec, dict):
        return 0.0, "none", None, None
    sym = str(rec.get("symbol") or "").upper().strip()
    if not sym:
        return 0.0, "none", None, None
    if gt is None:
        import ai_trading as gt  # noqa: PLW0621
    try:
        refresh = getattr(gt, "refresh_quotes_now", None)
        if callable(refresh):
            refresh([sym])
        else:
            inv = getattr(gt, "invalidate_quotes", None)
            if callable(inv):
                inv([sym])
            prime = getattr(gt, "prime_quotes", None)
            if callable(prime):
                prime([sym])
    except Exception:
        pass
    ask_f, src, age = apply_decision_price(rec, cfg, now)
    if ask_f > 0:
        try:
            ensure_live_exhaustion(rec, ask_f, cfg, now, sig=sig)
        except Exception:
            pass
        try:
            refresh_engine_rsi(rec, sig)
            refresh_engine_macd(rec, sig)
        except Exception:
            pass
    bid_f: float | None = None
    try:
        hit = gt._cached_quote(sym)
    except Exception:
        hit = None
    if hit is not None and hit[1] is not None:
        try:
            cached_bid = float(hit[1])
            bid_f = cached_bid if cached_bid > 0 else None
        except (TypeError, ValueError):
            bid_f = None
    if bid_f is None:
        try:
            bid = gt._latest_bid(sym)
            bid_f = float(bid) if bid is not None else None
        except Exception:
            bid_f = None
    if bid_f is not None and bid_f <= 0:
        bid_f = None
    return ask_f, src, age, bid_f


def stream_says_far_from_zone(
    rec: dict,
    cfg: dict,
) -> tuple[bool, float | None]:
    """True when the live tape puts price clearly outside this record's zone.

    Used purely to *skip* a REST quote, never to arm: the socket carries
    trades, not quotes. A print can land at the bid while the ask is still
    above the zone, so substituting last-trade for ask would arm on a price the
    order cannot actually get — the "price left the entry zone" failure class.
    The real ask is still fetched for anything near the band.

    Below the band: do **not** treat an armable pullback overshoot as "far".
    Skipping REST there used to stamp ``below_zone`` and never run the arm
    gate on names that should still fire.
    """
    if not bool(cfg.get("ai_watch_stream_enabled", True)):
        return False, None
    if arm_at_last(cfg):
        # Tape is the entry. Never skip the quote because last is above a
        # leftover pullback band.
        return False, None
    structure = rec.get("structure") if isinstance(rec.get("structure"), dict) else None
    levels = _structure_levels(structure) if structure else None
    if levels is None:
        return False, None          # no zone yet — we need a real quote
    entry_low, entry_high, stop, _t, _rr = levels

    got = stream_quote(rec.get("symbol"))
    if got is None:
        return False, None
    px, age = got
    try:
        max_age = float(cfg.get("ai_watch_stream_max_age_sec", 10.0) or 0.0)
    except (TypeError, ValueError):
        max_age = 10.0
    if max_age > 0 and age > max_age:
        return False, px            # stale tape — fall back to REST

    try:
        margin = max(0.0, float(
            cfg.get("ai_watch_stream_skip_margin_pct", 1.0) or 0.0)) / 100.0
    except (TypeError, ValueError):
        margin = 0.01
    lo = min(entry_low, entry_high) * (1.0 - margin)
    hi = max(entry_low, entry_high) * (1.0 + margin)
    if px > hi:
        return True, px
    # In or below the band: stay on the arm path. The planned stop is not
    # a "far" floor — it only exists after the position is open.
    return False, px


def refresh_engine_macd(rec: dict, sig: dict | None) -> bool:
    """Stamp the engine's current MACD onto a watch record.

    MACD is the entry lever since 8/26, and it was the only lever with no
    refresh of its own: the full record is rebuilt in poll_once on
    ai_watch_poll_sec, so between polls the gate decided on a reading that
    old while a current one sat on the wire — exactly the problem
    refresh_engine_rsi was written to fix for RSI, on the same 2s sync.

    Cheap for the same reason: the wire is already cached, so this is a dict
    lookup per symbol.

    Returns True when a value was written. A sig with no gap writes nothing
    rather than blanking what the record has — "the engine has not computed
    it yet" is not "the gap is gone".
    """
    if not isinstance(rec, dict) or not isinstance(sig, dict):
        return False
    gap = sig.get("macd_gap") if sig.get("macd_gap") is not None else sig.get("macd_hist")
    if gap is None:
        return False
    ind = rec.get("indicator")
    if not isinstance(ind, dict):
        ind = {}
        rec["indicator"] = ind
    ind["macd_gap"] = gap
    ind["macd_hist"] = gap
    for k in ("macd_fast", "macd_slow", "macd_sep_ratio", "macd_bull",
              "macd_cross", "macd_ok", "macd_gap_rising",
              "macd_gap_falling", "macd_gap_prev"):
        ind[k] = sig.get(k)
    # Provenance and age of the bars this reading was drawn on. MACD became
    # the entry lever on 8/26 and was the only lever with neither — %R has
    # ai_watch_require_live_pctr and RSI has ai_watch_require_realtime_rsi,
    # both gating on exactly these two facts. bars_src flips per ticker
    # mid-session (20 recoveries and 27 fallbacks across 18 symbols on
    # 2026-08-20), so without it one gate silently alternates between the
    # Finnhub tape and a REST fallback up to 60s old.
    ind["macd_src"] = sig.get("bars_src")
    ind["macd_age_sec"] = sig.get("bars_age_sec")
    return True


def refresh_engine_rsi(rec: dict, sig: dict | None) -> bool:
    """Stamp the engine's current CM RSI-2 onto a watch record.

    The engine recomputes RSI-2 every second — _check_proximity injects the
    live price as the forming bar's close, which is what stops the reading
    freezing until the next bar closes. But the book only picked that up in
    poll_once, on ai_watch_poll_sec (20s), because the 2s desk sync carries
    the previous indicator dict forward untouched. So the arm gate was reading
    an RSI up to twenty seconds stale while a current one sat on the wire.

    %R already avoids this: the sync calls ensure_live_exhaustion every cycle.
    This is the same idea for the RSI half, and it is cheap — the wire is
    already cached for 1.5s, so a sync costs one dict lookup per symbol.

    Returns True when a value was written.
    """
    if not isinstance(rec, dict) or not isinstance(sig, dict):
        return False
    if sig.get("cm_rsi") is None:
        return False
    ind = rec.get("indicator")
    if not isinstance(ind, dict):
        ind = {}
        rec["indicator"] = ind
    ind["cm_rsi"] = sig.get("cm_rsi")
    ind["cm_rsi_rising"] = bool(sig.get("cm_rsi_rising"))
    ind["cm_rsi_low"] = bool(sig.get("cm_rsi_low"))
    ind["cm_rsi_green"] = bool(sig.get("cm_rsi_green"))
    ind["cm_ok"] = bool(sig.get("cm_ok"))
    ind["cm_rsi_src"] = sig.get("bars_src")
    ind["cm_rsi_age_sec"] = sig.get("bars_age_sec")
    return True


def _engine_exh_fresh(sig: dict, cfg: dict) -> bool:
    """True when the engine %R is from Finnhub trades and young enough to use."""
    if str(sig.get("bars_src") or "").strip().lower() != "realtime":
        return False
    if sig.get("pctr") is None:
        return False
    try:
        age = float(sig.get("bars_age_sec"))
    except (TypeError, ValueError):
        return False
    try:
        cap = float(cfg.get("ai_watch_engine_exh_max_age_sec", 8.0) or 8.0)
    except (TypeError, ValueError):
        cap = 8.0
    return 0.0 <= age <= max(0.5, cap)


def refresh_engine_exh(rec: dict, sig: dict | None, cfg: dict | None,
                       now: float) -> bool:
    """Stamp the engine's Finnhub %R onto the watch record.

    Same job as refresh_engine_rsi: the 2s sync must not recompute EXH from
    sampled last prints when a tick-true reading is already on the wire.
    Returns True when the engine value was written.
    """
    if not isinstance(rec, dict) or not isinstance(sig, dict):
        return False
    cfg = cfg if isinstance(cfg, dict) else {}
    if not _engine_exh_fresh(sig, cfg):
        return False
    ind = rec.get("indicator")
    if not isinstance(ind, dict):
        ind = {}
        rec["indicator"] = ind
    pctr = _f_or_none(sig.get("pctr"))
    if pctr is None:
        return False
    ind["pctr"] = float(pctr)
    ind["pctr_rising"] = bool(sig.get("pctr_rising"))
    ind["pctr_falling"] = bool(sig.get("pctr_falling"))
    if sig.get("pctr_slow") is not None:
        ind["pctr_slow"] = _f_or_none(sig.get("pctr_slow"))
    ind["pctr_slow_rising"] = bool(sig.get("pctr_slow_rising"))
    ind["pctr_slow_falling"] = bool(sig.get("pctr_slow_falling"))
    ind["pctr_ob"] = bool(sig.get("pctr_ob"))
    ind["pctr_tight"] = bool(sig.get("pctr_tight"))
    if sig.get("pctr_gap") is not None:
        ind["pctr_gap"] = _f_or_none(sig.get("pctr_gap"))
    ind["pctr_src"] = "live"
    ind["pctr_px_src"] = "engine"
    ind["pctr_ts"] = float(now)
    return True


# Block codes cm_rsi_allows_buy can produce. Carried-forward values for these
# go stale the moment the RSI behind them moves.
_RSI_BLOCK_PREFIXES = ("no_rsi_data", "rsi_extended", "rsi_not_rising",
                       "rsi_below_band", "rsi_not_realtime")


def _restamp_rsi_block(rec: dict, cfg: dict, now: float) -> None:
    """Re-decide an RSI block against the RSI the row is now showing.

    The arm gate only runs in poll_once, on ai_watch_poll_sec (20s), and the
    2s sync carries block_code forward untouched. Once the sync started
    refreshing the RSI every 2s, that left a window where the State column
    contradicted the RSI column beside it: a row could read "no rsi data" next
    to a perfectly good 89.9, because the block was decided one cycle earlier
    when the engine had not computed the name yet.

    Only RSI-family codes are restamped, and only when the gate is enabled —
    this is not a place to re-run the whole arm decision, just to stop one
    label outliving the number it describes.
    """
    if not bool(cfg.get("ai_watch_arm_require_cm_rsi", False)):
        return
    code = str(rec.get("block_code") or "").strip().lower()
    if not code.startswith(_RSI_BLOCK_PREFIXES):
        return
    ok, why = cm_rsi_allows_buy(rec, cfg)
    if ok:
        # The reason it was held on no longer applies. Clear rather than
        # invent a new one — the next poll runs the full gate.
        rec["block_code"] = None
        rec["block_reason"] = None
        rec["blocker"] = None
    elif why != code:
        rec["block_code"] = why
        rec["blocker"] = format_blocker(why)
        rec["block_reason"] = rec["blocker"]
        rec["block_ts"] = float(now)


ENGINE_HEARTBEAT = ROOT / "signal_state.json"

# ── name-level gates: SIP spread and the opening gap ────────────────────────
# Cached per symbol so the arm path makes at most one data call per name per
# TTL. Both fail CLOSED at the gate: a name we cannot price is not traded.
_SIP_SPREAD_CACHE: dict[str, tuple[float | None, float]] = {}
_GAP_CACHE: dict[str, tuple[float | None, float]] = {}
_DATA_CLIENT = None


def _data_client():
    global _DATA_CLIENT
    if _DATA_CLIENT is None:
        from config import load_config
        import alpaca_api as aa
        full = load_config() or {}
        _DATA_CLIENT = aa.connect_data_client({
            "api_key": full.get("api_key"), "secret_key": full.get("secret_key")})
    return _DATA_CLIENT


def sip_spread_pct(sym: str, *, now: float | None = None, ttl: float = 180.0,
                   delay_min: float = 16.0, fetch=None) -> float | None:
    """Median SIP (ask-bid)/mid, %, over the minute ending delay_min ago.

    Live SIP is not on this plan; historical SIP is, once 15 minutes old. A
    name's spread 16 minutes back predicts the one we pay: for names at
    <= 0.05% then, 99% were <= 0.10% at entry (2026-09-14..23, 673 entries).
    """
    sym = str(sym or "").upper()
    t = float(now if now is not None else time.time())
    hit = _SIP_SPREAD_CACHE.get(sym)
    if hit is not None and t - hit[1] < ttl:
        return hit[0]
    val = None
    try:
        if fetch is not None:
            quotes = fetch(sym, t - delay_min * 60)
        else:
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockQuotesRequest
            end = _dt.fromtimestamp(t - delay_min * 60, tz=_tz.utc)
            q = _data_client().get_stock_quotes(StockQuotesRequest(
                symbol_or_symbols=sym, start=end - _td(seconds=60), end=end,
                feed=DataFeed.SIP, limit=5000))
            quotes = [(float(r.bid_price), float(r.ask_price))
                      for r in (q.data.get(sym) or [])]
        spreads = sorted((a - b) / ((a + b) / 2) * 100 for b, a in quotes
                         if b and a and a >= b)
        if spreads:
            val = spreads[len(spreads) // 2]
    except Exception:
        val = None
    _SIP_SPREAD_CACHE[sym] = (val, t)
    _record_input("sip_spread", sym, val, t)
    return val


def _record_input(kind: str, sym: str, val, t: float, **extra) -> None:
    """Session recorder: an external-data value the desk just computed, so an
    off-hours replay reads what the desk saw instead of re-fetching it."""
    try:
        import session_recorder as _rec
        _rec.record_input(kind, sym, val, ts=t, **extra)
    except Exception:  # noqa: BLE001
        pass


_RVOL_PACE_CACHE: dict[str, tuple[float | None, float]] = {}
_AVG_VOL_CACHE: dict[str, tuple[float | None, str]] = {}
_RVOL_OBS_LOGGED: dict[str, float] = {}


def rvol_pace_sip(sym: str, *, now: float | None = None, ttl: float = 120.0,
                  delay_min: float = 16.0, fetch=None) -> float | None:
    """Today's SIP volume pace vs this stock's own 20-day normal, delayed 16 min.

    pace = SIP volume 09:30 -> (now - 16 min)
           / (20-day avg daily SIP volume x expected share of a day by then)
    One feed on both sides (SIP; live IEX volume must not feed an SIP-built
    ratio). The plan serves SIP once 15 minutes old, so this is None before
    ~09:46. tools/studies/runway_target_study.py: pace >= 1.64 took +2%-before
    --1% runway from 4% to 16% (z +13.8, both halves); vol_trail_book_study:
    the one arm on those names only, +0.046%/trade in both halves (thin).
    fetch(sym, t_end) -> (volume_so_far, avg_daily_volume) replaces the
    network for tests.
    """
    sym = str(sym or "").upper()
    t = float(now if now is not None else time.time())
    hit = _RVOL_PACE_CACHE.get(sym)
    if hit is not None and t - hit[1] < ttl:
        return hit[0]
    val = None
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        t_end = t - delay_min * 60
        end_et = _dt.fromtimestamp(t_end, tz=et)
        mins = end_et.hour * 60 + end_et.minute - 570
        if mins >= 1:
            if fetch is not None:
                so_far, avg = fetch(sym, t_end)
            else:
                so_far, avg = _rvol_pace_inputs(sym, t_end, end_et)
            import tools.morning_funnel as _mf
            frac = _mf.expected_fraction(mins)
            if so_far and avg and frac > 0:
                val = float(so_far) / (float(avg) * frac)
    except Exception:
        val = None
    _RVOL_PACE_CACHE[sym] = (val, t)
    _record_input("rvol_pace", sym, val, t)
    return val


def _rvol_pace_inputs(sym: str, t_end: float, end_et) -> tuple[float | None, float | None]:
    from datetime import timedelta as _td, timezone as _tz
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    cl = _data_client()
    open_et = end_et.replace(hour=9, minute=30, second=0, microsecond=0)
    mb = cl.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=sym, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=open_et.astimezone(_tz.utc), end=end_et.astimezone(_tz.utc),
        feed=DataFeed.SIP)).data.get(sym) or []
    so_far = sum(float(b.volume) for b in mb)
    day = open_et.strftime("%Y-%m-%d")
    cached = _AVG_VOL_CACHE.get(sym)
    if cached is not None and cached[1] == day:
        avg = cached[0]
    else:
        db = cl.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym, timeframe=TimeFrame.Day,
            start=(open_et - _td(days=45)).astimezone(_tz.utc),
            end=(open_et - _td(minutes=1)).astimezone(_tz.utc),
            feed=DataFeed.SIP)).data.get(sym) or []
        prior = [float(b.volume) for b in db
                 if b.timestamp.astimezone(end_et.tzinfo).date() < open_et.date()]
        avg = sum(prior[-20:]) / 20 if len(prior) >= 20 else None
        _AVG_VOL_CACHE[sym] = (avg, day)
    return so_far, avg


def _rvol_pace_gate(record: dict, cfg: dict, now: float | None) -> tuple[bool, str]:
    """Observe (log + stamp) and/or enforce the volume-pace gate at the arm pass."""
    observe = bool(cfg.get("ai_watch_rvol_pace_observe", False))
    try:
        need = float(cfg.get("ai_watch_min_rvol_pace", 0) or 0)
    except (TypeError, ValueError):
        need = 0.0
    if not observe and need <= 0:
        return True, ""
    sym = str(record.get("symbol") or "").upper()
    pace = rvol_pace_sip(sym, now=now) if sym else None
    record["rvol_pace_sip"] = pace
    if observe:
        t = float(now if now is not None else time.time())
        if t - _RVOL_OBS_LOGGED.get(sym, 0.0) >= 60:
            _RVOL_OBS_LOGGED[sym] = t
            try:
                import ai_positions as cp
                obs_min = float(cfg.get("ai_watch_rvol_pace_observe_min", 1.64) or 1.64)
                cp.log_event("rvol_pace_observe", symbol=sym,
                             rvol_pace=(round(pace, 3) if pace is not None else None),
                             would_block=(pace is None or pace < obs_min),
                             threshold=obs_min, enforced=need > 0)
            except Exception:
                pass
    if need > 0:
        if pace is None:
            return False, "rvol_pace_unknown"
        if pace < need:
            record["block_detail"] = f"volume pace {pace:.2f}x < {need:g}x"
            return False, "rvol_pace_low"
    return True, ""


def open_gap_pct(sym: str, *, now: float | None = None, fetch=None) -> float | None:
    """Today's official open vs yesterday's close, %.

    SIP first: yesterday's daily close and today's 09:30 minute-bar open,
    which the plan serves once 15 minutes old (from ~09:46). Before that the
    live IEX snapshot stands in — its "open" is the first IEX print, which
    ran 0.6% off the official open on ACMR 2026-09-23 (-1.01% vs -1.52%), so
    only the SIP value is cached for the day; IEX is re-asked after 60s.
    Names that gapped down > 1% went +1% before -1% only 46% of the time all
    day (z -3.7, both halves, 2026-09-14..23).
    """
    sym = str(sym or "").upper()
    t = float(now if now is not None else time.time())
    day = time.strftime("%Y-%m-%d", time.localtime(t))
    hit = _GAP_CACHE.get(sym)
    if hit is not None and hit[0] is not None:
        val, ts, src = hit if len(hit) == 3 else (hit[0], hit[1], "sip")
        if time.strftime("%Y-%m-%d", time.localtime(ts)) == day and (
                src == "sip" or t - ts < 60):
            return val
    val, src = None, "sip"
    try:
        if fetch is not None:
            day_open, prev_close = fetch(sym)
        else:
            day_open, prev_close = _gap_inputs_sip(sym, t)
            if day_open is None or prev_close is None:
                src = "iex"
                from alpaca.data.enums import DataFeed
                from alpaca.data.requests import StockSnapshotRequest
                snap = _data_client().get_stock_snapshot(StockSnapshotRequest(
                    symbol_or_symbols=sym, feed=DataFeed.IEX)).get(sym)
                day_open = float(snap.daily_bar.open)
                prev_close = float(snap.previous_daily_bar.close)
        if day_open and prev_close:
            val = (float(day_open) / float(prev_close) - 1) * 100
    except Exception:
        val = None
    _GAP_CACHE[sym] = (val, t, src)
    _record_input("open_gap", sym, val, t, src=src)
    return val


_DAY_HIGH_CACHE: dict[str, tuple[float | None, float]] = {}


def day_high_iex(sym: str, *, now: float | None = None, ttl: float = 120.0) -> float | None:
    """Today's regular-session high so far from the desk's 1m IEX bars.

    For the room-below-HOD arm gate: at the -50 cross, names >= ~2.6% below
    their day high made +0.26% net per trade held out (t 3.0) vs +0.02% for
    all crosses, and beat the baseline in both halves
    (tools/studies/name_selection_study.py, 2026-09-25). IEX highs can sit a
    little under the SIP high, which makes the gate stricter, not looser.
    None when unknown (no bars since 09:30).
    """
    sym = str(sym or "").upper()
    t = float(now if now is not None else time.time())
    hit = _DAY_HIGH_CACHE.get(sym)
    if hit is not None and t - hit[1] < ttl:
        return hit[0]
    val = None
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        import alpaca_api as aa
        from config import load_config
        et = ZoneInfo("America/New_York")
        open_t = _dt.fromtimestamp(t, et).replace(hour=9, minute=30, second=0,
                                                  microsecond=0).timestamp()
        bar_cfg = {**(load_config() or {}), "bar_timeframe": "1Min", "bar_count": 420}
        df = aa.fetch_bars(_data_client(), sym, bar_cfg)
        if df is not None and len(df):
            idx = [ts.timestamp() for ts in df.index]
            highs = [float(h) for h, ts in zip(df["high"], idx) if open_t <= ts <= t - 60]
            val = max(highs) if highs else None
    except Exception:  # noqa: BLE001
        val = None
    _DAY_HIGH_CACHE[sym] = (val, t)
    _record_input("day_high", sym, val, t)
    return val


def room_below_hod_refusal(record: dict, sym: str, ask, cfg: dict,
                           *, now: float | None = None) -> str | None:
    """'near_hod' / 'hod_unknown' when ai_watch_min_room_below_hod_pct is on
    and price is not at least that far under today's high; None to allow."""
    try:
        need = float(cfg.get("ai_watch_min_room_below_hod_pct", 0) or 0)
    except (TypeError, ValueError):
        need = 0.0
    if need <= 0 or not sym:
        return None
    hod = day_high_iex(sym, now=now)
    try:
        px = float(ask or 0)
    except (TypeError, ValueError):
        px = 0.0
    if not hod or px <= 0:
        return "hod_unknown"
    room = (px / float(hod) - 1) * 100
    if room > -need:
        record["block_detail"] = f"{room:+.2f}% from day high (need <= -{need:g}%)"
        return "near_hod"
    return None


_ASYNC_GATES_BOUND = False


def bind_async_gates(*, name: str = "ew-gate-warm", max_age: float = 600.0,
                     idle_sec: float = 2.0, keep_sec: float = 300.0) -> bool:
    """Make sip_spread_pct / open_gap_pct / rvol_pace_sip non-blocking.

    Each one blocks on Alpaca when its cache is cold or stale. On a book or
    snapshot thread that stalled the book 6-16 s at a time (2026-09-25) and
    aged every seated price past the 15 s rule. Bound, a read returns the
    last value (<= max_age old, same day; None when cold, which the gates
    already treat as unknown) and queues the name; one daemon thread calls
    the real function for names read in the last keep_sec, so refreshes keep
    the live TTLs. Idempotent; returns True when it bound.
    """
    global sip_spread_pct, open_gap_pct, rvol_pace_sip, day_high_iex, _ASYNC_GATES_BOUND
    if _ASYNC_GATES_BOUND:
        return False
    real = (sip_spread_pct, open_gap_pct, rvol_pace_sip, day_high_iex)
    want: dict[str, float] = {}
    lock = threading.Lock()

    def _cached(cache: dict, gap: bool):
        def _read(sym, *args, **kwargs):
            s = str(sym or "").upper()
            if not s:
                return None
            t = time.time()
            with lock:
                want[s] = t
            hit = cache.get(s)
            if not hit or hit[0] is None:
                return None
            ts = float(hit[1])
            if gap:
                same = time.strftime("%Y-%m-%d", time.localtime(ts)) == \
                    time.strftime("%Y-%m-%d", time.localtime(t))
                return hit[0] if same else None
            return hit[0] if t - ts <= max_age else None
        return _read

    sip_spread_pct = _cached(_SIP_SPREAD_CACHE, False)
    open_gap_pct = _cached(_GAP_CACHE, True)
    rvol_pace_sip = _cached(_RVOL_PACE_CACHE, False)
    day_high_iex = _cached(_DAY_HIGH_CACHE, True)          # same-day values only

    last: dict[tuple[str, int], float] = {}

    def _warm() -> None:
        while True:
            t = time.time()
            with lock:
                for s in [s for s, at in want.items() if t - at > keep_sec]:
                    want.pop(s, None)
                syms = sorted(want)
            for s in syms:
                for i, fn in enumerate(real):
                    # Each returns from its own cache when fresh; the floor stops
                    # a name whose lookup keeps failing (gap None) re-asking every pass.
                    if time.time() - last.get((s, i), 0.0) < 30.0:
                        continue
                    last[(s, i)] = time.time()
                    try:
                        fn(s)
                    except Exception:
                        pass
            time.sleep(idle_sec)

    threading.Thread(target=_warm, daemon=True, name=name).start()
    _ASYNC_GATES_BOUND = True
    return True


def _gap_inputs_sip(sym: str, t: float) -> tuple[float | None, float | None]:
    """(today's 09:30 SIP open, yesterday's SIP close) or Nones if not yet served."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from zoneinfo import ZoneInfo
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    et = ZoneInfo("America/New_York")
    now_et = _dt.fromtimestamp(t, tz=et)
    open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et < open_et + _td(minutes=16):
        return None, None
    cl = _data_client()
    mb = cl.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=sym, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=open_et.astimezone(_tz.utc), end=(open_et + _td(minutes=1)).astimezone(_tz.utc),
        feed=DataFeed.SIP)).data.get(sym) or []
    db = cl.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=sym, timeframe=TimeFrame.Day,
        start=(open_et - _td(days=7)).astimezone(_tz.utc),
        end=(open_et - _td(minutes=1)).astimezone(_tz.utc),
        feed=DataFeed.SIP)).data.get(sym) or []
    # Daily bars are stamped at 00:00 ET, so "end before 09:29" still returns
    # TODAY's bar (with today's close). Take the last bar dated before today.
    today = open_et.date()
    prior = [b for b in db if b.timestamp.astimezone(et).date() < today]
    if not mb or not prior:
        return None, None
    return float(mb[0].open), float(prior[-1].close)


def engine_heartbeat_age(now: float | None = None,
                         path: Path | None = None) -> float | None:
    """Seconds since the signal engine last wrote its state file, or None.

    Same heartbeat tools/watchdog.py uses to restart a wedged engine.
    """
    try:
        mtime = (path or ENGINE_HEARTBEAT).stat().st_mtime
    except OSError:
        return None
    return max(0.0, float(now if now is not None else time.time()) - mtime)


def _engine_indicator_map() -> dict[str, dict]:
    """symbol -> signal-engine indicator record, off the /api/state wire."""
    out: dict[str, dict] = {}
    for r in _dashboard_tickers():
        if not isinstance(r, dict):
            continue
        sym = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
        sp = r.get("signal_proximity")
        if sym and isinstance(sp, dict):
            out[sym] = sp
    return out


def hot_move_rvol_waive_pct(cfg: dict | None = None) -> float:
    """Day-chg % at/above which known-thin RVOL no longer blocks admission.

    2026-09-04 midday: movers panel held BIAF +52% / LABX +23% / CBRG +23%
    with SIP rvol 0.8–1.8x against ``ai_watch_min_rvol=2.0``, so the seed
    wiped every real momentum name before inclusion. Unknown rvol still
    abstains; known-thin below the floor still refuses *unless* the day
    move clears this waive. 0 disables the waive.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(
            cfg.get("ai_watch_hot_move_rvol_waive_pct", 20.0) or 0.0))
    except (TypeError, ValueError):
        return 20.0


def _admit_min_rvol(source: str, cfg: dict) -> float:
    """RVOL floor for admission/seed, source-aware.

    Movers get ``ai_watch_movers_min_rvol`` (default 1.0) — SIP movers rvol
    runs structurally lower than the desk's IEX ratio, and a shared 2.0
    floor emptied the movers shortlist on 2026-09-04. Trending keeps its
    own floor. Momentum / research use ``ai_watch_min_rvol``.
    """
    src = str(source or "").strip().lower()
    try:
        if src == "movers":
            return max(0.0, float(
                cfg.get("ai_watch_movers_min_rvol", 1.0) or 0.0))
        if src == "trending":
            return max(0.0, float(
                cfg.get("ai_watch_trending_min_rvol",
                         cfg.get("ai_watch_min_rvol", 2.0))
                or 0.0))
        return max(0.0, float(cfg.get("ai_watch_min_rvol", 2.0) or 0.0))
    except (TypeError, ValueError):
        return 2.0


def seed_indicator_record(
    symbol: str,
    row: dict | None = None,
    indicators: dict | None = None,
) -> dict:
    """Copy *row* and attach the %R reading seed RVOL relief should see.

    Seed loops used to call ``rvol_blocks_admit`` with no record. Heating
    relief then only fired when the seed file already carried ``indicator``,
    so a name whose %R lived on the desk wire (``signal_proximity``) was
    dropped ``thin_rvol`` before inclusion could apply the softer floor or
    the longer tape-age ceiling.
    """
    sym = str(symbol or "").upper().strip()
    rec: dict = dict(row) if isinstance(row, dict) else {}
    if sym:
        rec["symbol"] = sym
    ind = rec.get("indicator") if isinstance(rec.get("indicator"), dict) else None
    if ind is None:
        sp = rec.get("signal_proximity")
        if isinstance(sp, dict) and (
            sp.get("pctr") is not None
            or sp.get("pctr_slow") is not None
            or sp.get("pctr_rising") is not None
            or sp.get("pctr_slow_rising") is not None
        ):
            ind = sp
    if ind is None and sym and isinstance(indicators, dict):
        mapped = indicators.get(sym)
        if isinstance(mapped, dict):
            ind = mapped
    if isinstance(ind, dict):
        # Copy so a later stamp cannot mutate the dashboard payload.
        rec["indicator"] = dict(ind)
    return rec


def seed_rvol_gate(
    symbol: str,
    rvol: float | None,
    pct: float | None,
    cfg: dict,
    *,
    source: str,
    row: dict | None = None,
    indicators: dict | None = None,
) -> tuple[str | None, dict | None]:
    """``(thin_rvol or None, indicator or None)`` for a seed row.

    The indicator is the one relief was judged on, ready to stamp onto the
    shortlist row so inclusion sees the same reading.
    """
    rec = seed_indicator_record(symbol, row, indicators)
    why = rvol_blocks_admit(rvol, pct, cfg, source=source, record=rec)
    ind = rec.get("indicator") if isinstance(rec.get("indicator"), dict) else None
    return why, ind


def rvol_blocks_admit(
    rvol: float | None,
    pct: float | None,
    cfg: dict,
    *,
    source: str = "",
    record: dict | None = None,
) -> str | None:
    """Return ``thin_rvol`` when known-thin RVOL should refuse, else None.

    Unknown (None) abstains. Hot day-move waives the floor so +20% movers
    are not wiped by a 1.5x SIP ratio. Rising-heat quality (both lines
    rising + tight + heat band) may use ``ai_watch_heating_min_rvol``.
    """
    min_rvol = _admit_min_rvol(source, cfg)
    if record is not None and rising_heat_quality(record, cfg):
        try:
            heat_floor = float(cfg.get("ai_watch_heating_min_rvol", 1.25) or 0.0)
        except (TypeError, ValueError):
            heat_floor = 1.25
        if heat_floor > 0:
            min_rvol = min(float(min_rvol), heat_floor) if min_rvol > 0 else heat_floor
    if min_rvol <= 0 or rvol is None:
        return None
    try:
        rv = float(rvol)
    except (TypeError, ValueError):
        return None
    if rv + 1e-12 >= min_rvol:
        return None
    waive = hot_move_rvol_waive_pct(cfg)
    if waive > 0 and pct is not None and float(pct) + 1e-12 >= waive:
        return None
    return "thin_rvol"


_ADMIT_GATE_CODES = frozenset({"spread_wide", "gapped_down", "below_min_price",
                                "above_max_price"})


def admit_arm_gates_enabled(cfg: dict | None) -> bool:
    return bool((cfg or {}).get("ai_watch_admit_arm_gates", False))


def admit_arm_gates(row: dict, cfg: dict | None, *, now: float | None = None,
                    spread_fn=None, gap_fn=None) -> tuple[bool, str]:
    """The arm's hard gates, run at the door. ``(ok, reason)``.

    2026-09-24: the book held QMCO (SIP spread 0.47%), TEM, NBIS ($240) and
    CLF ($12.74), refused at the arm on every poll, while PFE and KR waited
    outside. Seats only have value if the arm can fire on them.

    Same numbers as the arm (sip_spread_pct, open_gap_pct and their caches),
    so admit and arm cannot disagree. Two deliberate differences:
      * unknown spread or gap ABSTAINS here (the arm still refuses it), so a
        slow data read can not empty the book;
      * the spread check sits out until the delayed SIP data covers the session
        (09:30 + ai_movers_sip_delay_min): before that it would judge
        premarket spreads, the 09:30-09:46 spread_wide wall.
    Price band applies to every source, including research seeds, which skip
    the arm-ready pre-check.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not admit_arm_gates_enabled(cfg) or not isinstance(row, dict):
        return True, ""
    sym = str(row.get("symbol") or "").upper().strip()
    t = float(now if now is not None else time.time())
    px = _f_or_none(row.get("price"))
    if px is None:
        px = _f_or_none(row.get("last_ask"))
    if px is not None:
        lo = _min_price_for(row.get("source"), cfg, default=0.0)
        hi = _f_or_none(cfg.get("ai_max_price")) or 0.0
        if lo > 0 and px + 1e-12 < lo:
            return False, "below_min_price"
        if hi > 0 and px + 1e-12 >= hi:
            return False, "above_max_price"
    if not sym:
        return True, ""
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _Z
        et = _dt.fromtimestamp(t, _Z("America/New_York"))
        mins_open = et.hour * 60 + et.minute - 570
    except Exception:  # noqa: BLE001
        mins_open = 999
    delay = _f_or_none(cfg.get("ai_movers_sip_delay_min"))
    delay = 15.0 if delay is None else delay
    max_sp = _spread_gate_max(row.get("source"), cfg)
    if max_sp > 0 and mins_open >= delay + 1:
        try:
            sp = (spread_fn or sip_spread_pct)(sym, now=t)
        except Exception:  # noqa: BLE001
            sp = None
        if sp is not None and sp > max_sp:
            return False, "spread_wide"
    block = _f_or_none(cfg.get("ai_watch_gap_down_block_pct")) or 0.0
    if block > 0:
        try:
            g = (gap_fn or open_gap_pct)(sym, now=t)
        except Exception:  # noqa: BLE001
            g = None
        if g is not None and g < -block:
            return False, "gapped_down"
    return True, ""


def passes_inclusion(
    row: dict,
    cfg: dict,
    *,
    indicators: dict[str, dict] | None = None,
) -> tuple[bool, list[str], str]:
    """Strict, conjunctive admission test. Returns (ok, criteria_met, reject).

    The old rule OR'd four criteria and admitted on any one. In practice three
    of them could never fire — rvol was None on every trending row, nothing hit
    the 50% move bar, and momentum was contributing nothing — so the entire
    book was selected by Stocktwits popularity alone, and four of six admitted
    names were *down* on the day on a long-only desk.

    Every gate here must pass. A candidate with no indicator data is rejected,
    not admitted: absence is not a pass.
    """
    if not isinstance(row, dict):
        return False, [], "bad_row"
    if _is_wash_look(row):
        return False, list(row.get("criteria") or []), "look_wash"
    sym = str(row.get("symbol") or "").upper().strip()
    met = list(row.get("criteria") or [])
    if sym and is_levered_etp(sym):
        return False, met, "levered_etp"
    if sym and _dead_reentry_blocked(sym, time.time(), cfg):
        return False, met, "dead_reentry"
    # Reseed cool after a stale_timeout drop — only while tape is still dead.
    # A young stream clears the cool (reseed_allowed_stream); otherwise refuse
    # as stale_timeout_reseed_block so logs are not confused with the drop.
    if sym and _stale_timeout_blocked(sym, time.time(), cfg=cfg, row=row):
        return False, met, "stale_timeout_reseed_block"
    if sym and _consume_reseed_stream_clear(sym):
        # Cool lifted by a live print — keep going; criteria note for logs.
        if "reseed_allowed_stream" not in met:
            met = list(met) + ["reseed_allowed_stream"]
    # Same-day strike demote after repeated no_stream_trade drops. Unlike
    # stale_timeout_reseed, a young stream does NOT clear this — seats stay
    # free until the next ET calendar day (A2 occupancy hygiene).
    if sym and _no_stream_strike_demoted(sym, time.time(), cfg):
        return False, met, "no_stream_strike_demote"
    gate_ok, gate_why = admit_arm_gates(row, cfg)
    if not gate_ok:
        return False, met, gate_why
    # Admission range-position filter. Off by default (cap 0), so this only
    # writes down what it WOULD have refused until out-of-sample days say
    # whether the 2026-09-05 gradient holds. Fails open and swallows its own
    # exceptions: an admission experiment must not be able to empty the book.
    try:
        import admission_filter
        _rp = admission_filter.check(row, cfg, time.time(),
                                     ew=sys.modules[__name__])
        if _rp:
            return False, met, _rp
    except Exception:
        pass

    source = str(row.get("source") or "").strip().lower()
    is_research = source in _RESEARCH_SOURCES
    # Attach live indicators early so rising-heat quality can relieve
    # tape-age / RVOL (BENF 2026-09-23: both lines rising, blocked stale).
    _ind_early = None
    if isinstance(indicators, dict) and sym:
        _ind_early = indicators.get(sym)
    if not isinstance(_ind_early, dict):
        _ind_early = row.get("indicator") if isinstance(
            row.get("indicator"), dict) else None
    _rec_quality = dict(row)
    if isinstance(_ind_early, dict):
        _rec_quality["indicator"] = _ind_early
    # Prefer not admitting names with no / dead tape over filling the book
    # with permanent stale_quote rows (AEHG/AOUT/LABX-class after ba79b10).
    # Movers get a tighter ceiling so thin +20% names need a live print.
    # Rising-heat quality may use a longer ceiling — the %R lines are live.
    tape_max = (
        movers_admit_max_tape_age_sec(cfg) if source == "movers"
        else admit_max_tape_age_sec(cfg)
    )
    if rising_heat_quality(_rec_quality, cfg):
        try:
            heat_tape = float(
                cfg.get("ai_watch_heating_admit_max_tape_age_sec", 300.0) or 0.0)
        except (TypeError, ValueError):
            heat_tape = 300.0
        if heat_tape > 0:
            tape_max = max(float(tape_max), heat_tape)
    if tape_max > 0 and sym:
        try:
            tape = live_print(sym)
        except Exception:
            tape = None
        if tape is None or tape[1] is None:
            # Row may already carry a dated ask from the seed producer.
            row_age = _f_or_none(row.get("price_age_sec"))
            if row_age is None:
                row_age = _f_or_none(row.get("last_ask_age_sec"))
            if row_age is None:
                return False, met, "no_tape"
            if float(row_age) > tape_max:
                return False, met, "stale_tape_admit"
        elif float(tape[1]) > tape_max:
            return False, met, "stale_tape_admit"

    price = row.get("price")
    try:
        price_f = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_f = None
    min_price = _min_price_for(source, cfg, default=1.0)
    if source == "movers":
        try:
            mv_min_px = float(cfg.get("ai_watch_movers_min_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            mv_min_px = 0.0
        if mv_min_px > 0:
            min_price = max(min_price, mv_min_px)
    # Two different facts under one label. A name with no price still cannot be
    # admitted — nothing downstream can size or zone it — but calling that
    # "below_min_price" reports a penny stock that was screened out, which is a
    # verdict about the name rather than about the feed. On 2026-08-07 all 111
    # below_min_price rejects had price=None and not one had a real price under
    # $1; the list included PLTR, ABNB, NET, TEAM and VST, and it fired in
    # bursts of the entire shortlist at once during quote outages. Read as
    # intended, the gate scorecard was scoring a price filter that never
    # actually rejected anything on price.
    #
    # Reporting only: the admission decision is unchanged, including the case
    # where a zero floor disables the check entirely and a priceless row passes
    # to be judged by the gates below.
    if min_price > 0:
        if price_f is None:
            # Research theses have no desk quote until they sit on the book
            # (Finnhub subscribe is book-membership). Rejecting no_price
            # here is a deadlock: never admitted → never quoted → never
            # admitted. Other sources still fail closed.
            if not is_research:
                return False, met, "no_price"
        elif price_f < min_price:
            return False, met, "below_min_price"

    # Soft Momentum-open seed: skip score/indicators only. RVOL (when known)
    # and uptrend still apply — thin tape must not occupy the book.
    mom_soft = (
        bool(row.get("mom_open_soft"))
        or "mom_open" in met
        or bool(row.get("bypass_inclusion"))  # legacy flag: no longer free pass
    )
    if mom_soft and "mom_open" not in met:
        met.append("mom_open")

    min_dv = float(cfg.get("ai_min_dollar_volume", 0.0) or 0.0)
    if source == "movers":
        try:
            mv_dv = float(
                cfg.get("ai_watch_movers_min_dollar_volume", 0.0) or 0.0)
        except (TypeError, ValueError):
            mv_dv = 0.0
        if mv_dv > 0:
            min_dv = max(min_dv, mv_dv)
    if min_dv > 0:
        dv = row.get("dollar_volume")
        try:
            dv_f = float(dv) if dv is not None else None
        except (TypeError, ValueError):
            dv_f = None
        if dv_f is None or dv_f < min_dv:
            return False, met, "thin_dollar_volume"
        met.append("liquidity")

    # Long-only: must be up on the day. Research with no print yet abstains
    # (same idea as unknown rvol) so the thesis can sit and get a quote.
    # FLOAT. Measured over 507 closed trades on 2026-08-28, joining each to
    # its arm-time features and its own max favourable excursion:
    #
    #                    n     medMFE   reached +0.25R
    #   every trade     507    +0.041        10%
    #   float < 20M      33    +0.186        45%
    #   float >= 50M    438    +0.039         8%
    #
    # 438 of 507 trades this desk has ever taken were in names over 50M float,
    # and those names barely move: their median best moment was +0.039R, about
    # a fifth of a percent of price, against a trail that needs more than that
    # to clear the fill. Float is the strongest single admission filter found.
    #
    # A float we cannot read does NOT refuse — the lookup is a cached Finnhub
    # profile call and an outage must not empty the book. 0 disables.
    try:
        max_float_m = float(cfg.get("ai_watch_max_float_m", 0) or 0)
    except (TypeError, ValueError):
        max_float_m = 0.0
    if max_float_m > 0:
        try:
            import float_feed
            fl = float_feed.float_shares(str(row.get("symbol") or ""))
        except Exception:  # noqa: BLE001
            fl = None
        if fl is not None and fl > max_float_m:
            return False, met, "float_too_big"
        if fl is not None:
            met.append("low_float")

    if bool(cfg.get("ai_watch_require_uptrend", True)):
        pct = _pct_change_value(row.get("pct_change"))
        if is_research:
            if pct is not None and pct <= 0:
                return False, met, "not_uptrend"
            if pct is not None and pct > 0:
                met.append("uptrend")
        else:
            if pct is None or pct <= 0:
                return False, met, "not_uptrend"
            met.append("uptrend")

    # Known-thin RVOL refuses; unknown abstains. Momentum, trending, movers
    # and research share this — a 0.72x name occupying the book is a slot
    # taken from something that might actually dislocate (WOOF 0.72 / MOVE
    # 0.06 on 2026-09-03). Research with no reading yet still sits (the
    # quote arrives after admit). Same rule apply_look_highlights already
    # uses: "unknown rvol neither passes nor blocks."
    #
    # Hot day-move (≥ ai_watch_hot_move_rvol_waive_pct) waives the floor so
    # BIAF +52% / LABX +23% are not wiped by SIP rvol 0.8–1.8x while the
    # movers panel is full of real momentum (2026-09-04 midday).
    if source in ("momentum", "trending", "movers") or mom_soft or is_research:
        src_for_rvol = source or ("momentum" if mom_soft else "")
        rvol_f = _f_or_none(row.get("rvol"))
        pct_for_rvol = _pct_change_value(row.get("pct_change"))
        ind_for_rvol = None
        if isinstance(indicators, dict) and sym:
            ind_for_rvol = indicators.get(sym)
        if not isinstance(ind_for_rvol, dict):
            ind_for_rvol = row.get("indicator") if isinstance(
                row.get("indicator"), dict) else None
        rec_for_rvol = dict(row)
        if isinstance(ind_for_rvol, dict):
            rec_for_rvol["indicator"] = ind_for_rvol
        thin = rvol_blocks_admit(
            rvol_f, pct_for_rvol, cfg, source=src_for_rvol,
            record=rec_for_rvol)
        if thin:
            return False, met, thin
        if rvol_f is not None:
            floor = _admit_min_rvol(src_for_rvol, cfg)
            heat_relief = False
            if rising_heat_quality(rec_for_rvol, cfg):
                try:
                    heat_floor = float(
                        cfg.get("ai_watch_heating_min_rvol", 1.25) or 0.0)
                except (TypeError, ValueError):
                    heat_floor = 1.25
                if heat_floor > 0:
                    floor = (
                        min(float(floor), heat_floor) if floor > 0 else heat_floor
                    )
                    heat_relief = True
            if floor > 0 and rvol_f + 1e-12 >= floor:
                met.append("rvol")
                if heat_relief:
                    met.append("heating_rvol_relief")
            elif (
                floor > 0
                and hot_move_rvol_waive_pct(cfg) > 0
                and pct_for_rvol is not None
                and pct_for_rvol + 1e-12 >= hot_move_rvol_waive_pct(cfg)
            ):
                met.append("hot_move_rvol_waive")

    def _arm_ready_inclusion_refuse() -> str | None:
        """Refuse reason when arm-ready keep gate fails; None if ok/N/A."""
        if not (
            admit_require_arm_ready(cfg)
            and _row_needs_arm_ready_gate(row)
            and not bool(row.get("scout_only"))
        ):
            if isinstance(row, dict) and row.get("admit_chg_band") is None:
                pct_b = _pct_change_value(row.get("pct_change"))
                row["admit_chg_band"] = classify_admit_chg_band(pct_b, cfg)
            return None
        ready, why = stamp_arm_ready_fields(
            row, cfg, indicators=indicators, now=time.time())
        if ready:
            if "arm_ready" not in met:
                met.append("arm_ready")
            return None
        pct = _pct_change_value(row.get("pct_change"))
        if pct is None:
            pct = _pct_change_value(row.get("admit_pct_change"))
        _, _, soft_max = admit_chg_band_bounds(cfg)
        if (
            pct is not None
            and float(pct) > soft_max
            and not admit_pullback_ok(row, cfg)
        ):
            return "arm_ready_chg_band"
        return f"arm_ready_{why or 'fail'}"

    # Soft mom_open path: after price + rvol (+ uptrend above), admit without
    # score / EXT / indicator gates. Arm-ready keep gate still applies.
    if mom_soft:
        refuse = _arm_ready_inclusion_refuse()
        if refuse:
            return False, met, refuse
        met = list(dict.fromkeys(met))
        return True, met, ""

    refuse = _arm_ready_inclusion_refuse()
    if refuse:
        return False, met, refuse

    # Trending admission: day green (uptrend above), never WASH. EXT is
    # optional unless ai_watch_require_look_ext is true. Score is not required
    # when the seed came in via rvol / day-move (2026-08-11 conversion gap).
    if source == "trending":
        look_raw = row.get("look_reason")
        look = str(look_raw or "").strip().upper() if look_raw is not None else ""
        if look == "WASH":
            return False, met, "look_wash"
        if bool(cfg.get("ai_watch_require_look_ext", False)):
            if look != "EXT":
                return False, met, "not_ext"
            met.append("ext")
            if "score" not in met and "rvol" not in met and "uptrend" not in met:
                return False, met, "low_score"
        elif look == "EXT":
            met.append("ext")

    if bool(cfg.get("ai_watch_require_indicators", True)):
        sig = (indicators or {}).get(sym)
        if not isinstance(sig, dict):
            return False, met, "no_indicators"
        if sig.get("sell_signal"):
            return False, met, "sell_signal"
        try:
            prox = float(sig.get("proximity_pct") or 0)
        except (TypeError, ValueError):
            prox = 0.0
        if prox < float(cfg.get("ai_watch_min_proximity", 67) or 0):
            return False, met, f"proximity_{prox:.0f}"
        met.append("bullish")
        # ADX is not published by the engine yet; when it is, gate it here on
        # ai_watch_min_adx. Until then the three-indicator state carries the
        # trend-strength judgement.
        adx = sig.get("adx")
        min_adx = float(cfg.get("ai_watch_min_adx", 0) or 0)
        if min_adx > 0 and adx is not None:
            try:
                if float(adx) < min_adx:
                    return False, met, f"adx_{float(adx):.0f}"
                met.append("adx")
            except (TypeError, ValueError):
                pass

    # De-dupe, order-preserving: the shortlist already tags criteria it matched
    # on, and the gates above append the same names when they independently
    # confirm one. A doubled "rvol" is only cosmetic on the book but lands in
    # the entry feature vector, where slicing reads criteria as a set.
    met = list(dict.fromkeys(met))
    return True, met, ""


def apply_inclusion_gate(
    rows: list[dict],
    cfg: dict,
    *,
    indicators: dict[str, dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Filter shortlist rows through passes_inclusion + admission dwell.

    Dwell exists because the book is rebuilt every 2s: a name that blinked
    below threshold for one tick was deleted outright, taking its frozen zone
    and structure_ts with it, then re-admitted moments later with the zone
    re-anchored to a worse price.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    indicators = indicators if indicators is not None else _engine_indicator_map()
    need = max(1, int(cfg.get("ai_watch_admit_ticks", 2) or 1))
    kept: list[dict] = []
    rejected: list[dict] = []
    seen: set[str] = set()
    kept_syms: set[str] = set()
    last_reject: dict[str, dict] = {}
    row_by_sym: dict[str, dict] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym or sym in kept_syms:
            continue
        seen.add(sym)
        row_by_sym[sym] = row
        ok, met, why = passes_inclusion(row, cfg, indicators=indicators)
        if not ok:
            last_reject[sym] = {"symbol": sym, "reason": why, "criteria": met}
            continue
        last_reject.pop(sym, None)
        ticks = _admit_ticks.get(sym, 0) + 1
        _admit_ticks[sym] = ticks
        if ticks < need:
            last_reject[sym] = {
                "symbol": sym, "reason": f"dwell_{ticks}/{need}", "criteria": met,
            }
            continue
        out = dict(row)
        out["criteria"] = met
        kept.append(out)
        kept_syms.add(sym)
        # Observe-only proposal ledger — inclusion kept.
        src_k = str(row.get("source") or "").strip().lower()
        _note_proposal(
            stage="inclusion",
            symbol=sym,
            proposer=src_k or "unknown",
            decision="kept",
            reason=None,
            owner=src_k or None,
            row=out,
            extra={
                "criteria": met,
                "arm_ready": out.get("arm_ready"),
                "arm_ready_reason": out.get("arm_ready_reason"),
                "admit_pct_change": out.get("admit_pct_change"),
                "admit_chg_band": out.get("admit_chg_band"),
                "scout_only": bool(out.get("scout_only")),
            },
            cfg=cfg,
        )
    for rec in last_reject.values():
        rejected.append(rec)
        why = str(rec.get("reason") or "")
        if not why.startswith("dwell_"):
            _admit_ticks.pop(rec["symbol"], None)
        # Observe-only refused-side ledger (uncapped). Fail-open.
        try:
            if bool(cfg.get("ai_admit_ledger_enabled", True)) and bool(
                cfg.get("ai_admit_ledger_inclusion", True)
            ):
                import admit_ledger as _al
                sym_r = str(rec.get("symbol") or "")
                src_row = row_by_sym.get(sym_r) or {}
                _al.log_refuse(
                    stage="inclusion",
                    symbol=sym_r,
                    reason=why,
                    source=str(src_row.get("source") or ""),
                    row=src_row if isinstance(src_row, dict) else None,
                    extra={"criteria": rec.get("criteria")},
                    cfg=cfg,
                )
        except Exception:
            pass
        # Attributed proposal ledger — inclusion dropped (incl. dwell_*).
        try:
            sym_r = str(rec.get("symbol") or "")
            src_row = row_by_sym.get(sym_r) or {}
            src_p = str(src_row.get("source") or "").strip().lower()
            _note_proposal(
                stage="inclusion",
                symbol=sym_r,
                proposer=src_p or "unknown",
                decision="dropped",
                reason=why,
                owner=src_p or None,
                row=src_row if isinstance(src_row, dict) else None,
                extra={"criteria": rec.get("criteria")},
                cfg=cfg,
            )
        except Exception:
            pass
    for gone in [s for s in _admit_ticks if s not in seen]:
        _admit_ticks.pop(gone, None)
    return kept, rejected


def research_candidate_rows() -> list[dict]:
    """Current AI Research board rows (Grok + Anthropic), as watch candidates.

    Also reads seed-rank boards (``seed_rank_*.json``): those are AI ranks of
    the non-AI seed union and must land on the watchlist without placing
    orders. Free-research boards stay first so an open thesis is not silently
    replaced by a ranker row when both list the same name; seed-rank fills
    gaps and refreshes on its own schedule.
    """
    out: list[dict] = []
    seen: set[str] = set()
    # Grok last among free-research so it wins on overlap. Seed-rank boards
    # follow so they auto-admit without stomping an active free-research row.
    for path, default_src in (
        (ROOT / "agy_suggestions.json", "agy"),
        (ROOT / "claude_suggestions.json", "agy"),  # legacy filename
        (ROOT / "suggestions.json", "agy"),
        (ROOT / "grok_suggestions.json", "xai"),
        # Canonical seed-rank watch feed is seed_rank_gx (legacy ax). Per-model
        # boards may be audit-only (watch_facing=false) under union publish.
        (ROOT / "seed_rank_gx.json", "agy"),
        (ROOT / "seed_rank_ax.json", "agy"),  # legacy agreement filename
        (ROOT / "seed_rank_agy.json", "agy"),
        (ROOT / "seed_rank_claude.json", "agy"),  # legacy filename
        (ROOT / "seed_rank_grok.json", "xai"),
    ):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        # Stale seed-rank boards must not re-seed yesterday's list.
        if str(raw.get("kind") or "") == "seed_rank":
            if raw.get("watch_facing") is False:
                continue
            try:
                age = time.time() - float(raw.get("ts") or 0)
            except (TypeError, ValueError):
                age = 1e9
            # Slightly over one hourly slot so a missed tick does not wipe the board.
            if age > 75 * 60:
                continue
        file_src = str(raw.get("source") or default_src).lower().strip()
        if file_src in ("xai", "grok"):
            src_label = "xai"
        elif file_src in ("agy", "google", "gemini", "anthropic", "claude"):
            src_label = "agy"
        else:
            src_label = default_src
        rows = raw.get("rows") or raw.get("suggestions") or raw.get("items") or []
        if not isinstance(rows, list):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            s = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
            if not s or not s[0].isalpha() or s in seen:
                continue
            seen.add(s)
            reason = str(r.get("reason") or r.get("summary") or "research")[:80]
            # Seed-rank published rows stay watch-admissible (agreement gate
            # unchanged). Per-row source_mark / second_opinion live on the board.
            row_src = str(r.get("primary_source") or r.get("source") or src_label).lower()
            if row_src in ("xai", "grok"):
                row_src_label = "xai"
            elif row_src in ("agy", "google", "gemini", "both"):
                row_src_label = "agy" if row_src != "both" else src_label
            else:
                row_src_label = src_label
            out.append({
                "symbol": s,
                "trending_score": _score_from_row(r),
                "score": _score_from_row(r),
                "reason": reason,
                "agreement": True,
                "source": row_src_label,
            })
    return out

def research_universe_symbols() -> set[str]:
    """Symbols currently on AI Research boards (Grok + Anthropic wires)."""
    return {
        str(r.get("symbol") or "").upper()
        for r in research_candidate_rows()
        if r.get("symbol")
    }


def live_panel_universe(cfg: dict | None = None) -> set[str]:
    """Symbols allowed on AI Watch — the union of the four enabled seeds."""
    cfg = cfg if isinstance(cfg, dict) else {}
    live: set[str] = set()
    for r in desk_candidate_rows(cfg):
        if isinstance(r, dict):
            s = str(r.get("symbol") or "").upper().strip()
            if s:
                live.add(s)
    return live


def sync_watch_from_source_panels(
    cfg: dict | None = None,
    now: float | None = None,
) -> dict:
    """Rebuild AI Watch from the four source panels.

    Operator rules:
      • Trending: score > min, day change up, LOOK=EXT (never WASH).
      • Momentum only if the desk row has FIRST / NEW / BURST.
      • Research: whatever the Grok / Anthropic boards currently list.
      • Trader Bro: call-outs inside ``ai_watch_bb_live_fresh_sec``.

    Each seed has its own on/off flag (``ai_watch_seed_*``); turning one off
    removes that panel from the book without touching the others.

    Structure / in-flight submitted-filled state is preserved when the symbol
    remains. Everything else is dropped from the book file.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    t0 = float(now if now is not None else time.time())
    _record_desk_event("sync", t0)

    # A1: warm Finnhub for raw panel symbols (incl. seed-drop near-misses)
    # before the filtered shortlist is built — subscribe clock starts early.
    try:
        maybe_prewarm_panel_streams(cfg, now=t0)
    except Exception:
        pass

    # Candidate rows come from the dashboard over HTTP (two GETs, 2s timeout
    # each) — do that *outside* the lock so a slow/absent dashboard cannot stall
    # poll_once behind us for seconds at a time.
    candidates = desk_candidate_rows(cfg)

    # Continuous soft seed (trending/movers/momentum/research scout refresh).
    # Interval gated; prefers EXH 15–45. Merges into the same inclusion pipeline.
    try:
        seen_syms = {
            str(r.get("symbol") or "").upper().strip()
            for r in candidates if isinstance(r, dict)
        }
        _inds = _engine_indicator_map()
        soft_rows, soft_fired = maybe_soft_seed_rows(
            cfg, now=t0, seen=seen_syms, indicators=_inds)
        # Book server: shadow logs a ranked would-be book; live replaces
        # soft-seed intake with Movers+Trending+Research ranked seats.
        try:
            import book_server as _bs
            _bs_mode = _bs.mode(cfg)
            if _bs_mode in ("shadow", "live"):
                _pool = list(candidates) + list(soft_rows or [])
                try:
                    _max_seats = int(cfg.get("ai_book_server_max_seats", 12) or 12)
                except (TypeError, ValueError):
                    _max_seats = 12
                _live_syms = [
                    str(r.get("symbol") or "").upper()
                    for r in (load_watch() or {}).values()
                    if isinstance(r, dict)
                    and str(r.get("status") or "") not in ("invalidated", "expired")
                ]
                # Pace only from what this process already measured for the
                # arm (no new bars requests); movers rows carry their own.
                _paces = {
                    s: v for s, (v, ts) in list(_RVOL_PACE_CACHE.items())
                    if v is not None and t0 - ts < 300.0
                }
                _would = _bs.shadow_tick(
                    _pool, cfg=cfg, now=t0, live_book=_live_syms,
                    max_seats=_max_seats, indicators=_inds, paces=_paces)
                if bool(cfg.get("ai_watch_slot_priority", False)):
                    _SLOT_RANK.clear()
                    _SLOT_RANK.update({
                        str(r.get("symbol") or "").upper(): float(r["_book_server_priority"])
                        for r in _bs.rank_candidates(
                            _pool, cfg=cfg, now=t0, limit=10_000,
                            indicators=_inds, paces=_paces)})
                if _bs_mode == "live" and _would:
                    soft_rows = _would
                    soft_fired = True
                    for _r in soft_rows:
                        _r["criteria"] = list(_r.get("criteria") or []) + [
                            "book_server"]
                        _r["soft_seed"] = True
        except Exception:
            pass
        if soft_fired and soft_rows:
            candidates = list(candidates) + soft_rows
            try:
                ensure_watch_stream([r.get("symbol") for r in soft_rows], cfg=cfg)
            except Exception:
                pass
    except Exception:
        pass

    # Session recorder: who was nominated when, and the config in force, so a
    # replay can rebuild the book. Transitions / changes only; fails open.
    try:
        import learn_stamps
        import session_recorder as _rec
        _f = sys._getframe(1)
        _rec.record_source_set(candidates, ts=t0, note={
            **_LAST_POOL_CTX, "thread": threading.current_thread().name,
            "caller": f"{_f.f_code.co_name}<{_f.f_back.f_code.co_name if _f.f_back else ''}",
            "cfg_hash": _cfg_hash(cfg)})
        _rec.record_config_snap(
            cfg, git_sha=learn_stamps.git_version(),
            fingerprint=learn_stamps.config_fingerprint(cfg))
    except Exception:
        pass

    # Make sure the engine is computing indicators for everything on the
    # shortlist, then admit only what clears the strict conjunctive gate.
    try:
        push_candidates_to_engine([r.get("symbol") for r in candidates])
    except Exception:
        pass
    # Subscribe Finnhub (+ priority) for the shortlist BEFORE inclusion so
    # passes_inclusion's live_print tape-age check is not empty. Without this,
    # ensure_watch_stream only ran after a name was kept → midday no_tape /
    # no_price / stale_tape_admit emptied the book (2026-09-11). Post-admit
    # ensure_watch_stream below still runs. Dead seats still drop via
    # no_stream_trade / stale_timeout after grace.
    try:
        ensure_watch_stream([r.get("symbol") for r in candidates])
    except Exception:
        pass
    try:
        # Keep the pre-gate rows: the gate returns rejects as {symbol, reason,
        # criteria} only, and scoring a reject needs the price and features it
        # was rejected WITH.
        by_symbol = {
            str(r.get("symbol") or "").upper(): r
            for r in candidates if isinstance(r, dict)
        }
        try:
            _incl_inds = _engine_indicator_map()
        except Exception:
            _incl_inds = {}
        candidates, rejected = apply_inclusion_gate(
            candidates, cfg, indicators=_incl_inds)
        # Tag warming after inclusion (admit ≠ arm). Quota protects seats later.
        try:
            tag_warming_on_candidates(
                candidates, cfg, indicators=_engine_indicator_map())
        except Exception:
            pass
        # A name over its daily attempt cap must not be re-admitted. The poll
        # drops it, but seeding runs on its own cadence and put it straight
        # back: BULL was dropped for attempt_cap at 12:14:02, :16, :28, :40
        # and :53 on 2026-08-28 — five times in under a minute, 137 admit /
        # drop / entry events in a session. The cap has to hold HERE, at
        # admission, or the two mechanisms just fight each other and spend
        # quotes, poll slots and log lines doing it.
        try:
            cap = int(cfg.get("ai_watch_max_entries_per_symbol_day", 0) or 0)
        except (TypeError, ValueError):
            cap = 0
        if cap > 0:
            kept = []
            for r in candidates:
                sym = str((r or {}).get("symbol") or "").upper().strip()
                if sym and _entries_today(sym) >= cap:
                    rejected.append({"symbol": sym, "reason": "attempt_cap",
                                     "criteria": {}})
                    continue
                kept.append(r)
            candidates = kept
        _last_rejected.clear()
        _last_rejected.extend(rejected)
        _log_rejects(rejected, by_symbol, cfg, t0)
        try:
            write_admit_funnel(
                candidates=list(by_symbol.values()),
                kept=candidates,
                rejected=rejected,
                now=t0,
            )
        except Exception:
            pass
    except Exception:
        pass

    with _WATCH_LOCK:
        return _sync_watch_locked(candidates, t0, cfg)


def write_admit_funnel(
    *,
    candidates: list[dict],
    kept: list[dict],
    rejected: list[dict],
    now: float | None = None,
) -> dict:
    """Persist seed-drop + inclusion reject tallies for the midday funnel dig.

    Written to ``REPORT_DIR/admit_funnel.json`` every sync so a full movers
    panel with a tiny watch book is attributable without grepping logs.
    """
    from collections import Counter

    t0 = float(now if now is not None else time.time())
    seed = seed_drop_snapshot()
    by_src = Counter(
        str(r.get("source") or "") for r in (candidates or []) if isinstance(r, dict))
    kept_src = Counter(
        str(r.get("source") or "") for r in (kept or []) if isinstance(r, dict))
    rej_reasons = Counter(
        str(r.get("reason") or "") for r in (rejected or []) if isinstance(r, dict))
    kept_symbols = [
        str(r.get("symbol") or "").upper()
        for r in (kept or []) if isinstance(r, dict) and r.get("symbol")
    ]
    warming_n = sum(
        1 for r in (kept or [])
        if isinstance(r, dict)
        and str(r.get("seat_role") or "").lower() == "warming"
    )
    pin_n = sum(
        1 for r in (kept or [])
        if isinstance(r, dict)
        and str(r.get("seat_role") or "").lower() == "pin"
    )
    # Scouts == warming seats in the funnel; sync later stamps live book counts.
    scout_n = warming_n
    extreme_off = extreme_off_book_rows(
        kept_symbols=kept_symbols,
        rejected=list(rejected or []),
        cfg=_push_cfg(),
        limit=12,
    )
    payload = {
        "ts": round(t0, 2),
        "n_candidates": len(candidates or []),
        "n_kept": len(kept or []),
        "n_rejected": len(rejected or []),
        "warming_n": warming_n,
        "pin_n": pin_n,
        "scout_n": scout_n,
        "candidates_by_source": dict(by_src),
        "kept_by_source": dict(kept_src),
        "inclusion_reject_reasons": dict(rej_reasons),
        "seed_drops": seed,
        "kept_symbols": kept_symbols,
        # +100% (or knob) day-movers not on the book — each with a reason.
        "extreme_off_book": extreme_off,
        "extreme_move_pct": extreme_move_pct(_push_cfg()),
    }
    path = REPORT_DIR / "admit_funnel.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass
    # Also stamp a compact event so the day journal sees the funnel.
    # kept_symbols must land here too — json alone is not enough for digs.
    try:
        import ai_positions as cp
        top_seed = {
            f"{src}:{why}": n
            for src, bucket in (seed.get("counts") or {}).items()
            for why, n in (bucket or {}).items()
        }
        cp.log_event(
            "admit_funnel",
            n_candidates=payload["n_candidates"],
            n_kept=payload["n_kept"],
            kept_n=payload["n_kept"],
            n_rejected=payload["n_rejected"],
            warming_n=warming_n,
            pin_n=pin_n,
            scout_n=scout_n,
            kept_symbols=kept_symbols[:16],
            inclusion=dict(rej_reasons.most_common(8)),
            seed_drops=dict(sorted(top_seed.items(), key=lambda kv: -kv[1])[:12]),
        )
    except Exception:
        pass
    return payload


def _sync_watch_locked(candidates: list[dict], t0: float, cfg: dict | None = None) -> dict:
    """Rebuild step of ``sync_watch_from_source_panels`` — caller holds the lock."""
    old = load_watch()
    if not isinstance(old, dict):
        old = {}

    # All four panels: momentum (flagged), trending (score>min), research and
    # Trader Bro call-outs. The filter stays because `candidates` is whatever
    # desk_candidate_rows produced — an unlabelled or unknown source has no
    # panel behind it and must not reach the book.
    merged: dict[str, dict] = {}
    for r in candidates:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym:
            continue
        src = str(r.get("source") or "").lower()
        if src not in _PANEL_SOURCES:
            continue
        if is_levered_etp(sym):
            continue
        cfg_d = cfg if isinstance(cfg, dict) else {}
        if _dead_reentry_blocked(sym, t0, cfg_d):
            continue
        if _stale_timeout_blocked(sym, t0, cfg=cfg_d, row=r):
            continue
        if _no_stream_strike_demoted(sym, t0, cfg_d):
            continue
        _consume_reseed_stream_clear(sym)  # cool may have just cleared
        merged[sym] = r

    # Empty sources at startup: keep prior state to avoid wipe race.
    if not merged and old:
        cfg_d = cfg if isinstance(cfg, dict) else {}
        cleaned = dict(old)
        for key, rec in list(cleaned.items()):
            if not isinstance(rec, dict):
                continue
            status = str(rec.get("status") or "").lower().strip()
            if status in ("submitted", "filled"):
                continue
            if is_levered_etp(key) or _dead_reentry_blocked(key, t0, cfg_d):
                cleaned.pop(key, None)
        if cleaned != old:
            save_watch(cleaned)
        return cleaned

    # One wire read for the whole sync, not one per symbol. _dashboard_tickers
    # is cached for _DASH_CACHE_TTL anyway, so this is a dict build, but doing
    # it inside the loop would rebuild it for every candidate.
    _sync_indicators = _engine_indicator_map()

    new_state: dict[str, Any] = {}
    for sym, row in merged.items():
        prev = old.get(sym) if isinstance(old.get(sym), dict) else {}
        prev_status = str(prev.get("status") or "").lower().strip()
        if prev_status in ("submitted", "filled"):
            status = prev_status
        else:
            status = "watching"
        seeded_ask = _seed_last_ask(prev, row)
        rec = {
            "symbol": sym,
            "status": status,
            "agreement": True,
            "score": _score_from_row(row),
            "reason": str(row.get("reason") or prev.get("reason") or "")[:80],
            "source": str(row.get("source") or prev.get("source") or "research"),
            "structure": prev.get("structure", _EMPTY_RECORD_DEFAULTS["structure"]),
            "structure_ts": float(
                prev.get("structure_ts", _EMPTY_RECORD_DEFAULTS["structure_ts"]) or 0.0
            ),
            "last_poll_ts": float(
                prev.get("last_poll_ts", _EMPTY_RECORD_DEFAULTS["last_poll_ts"]) or 0.0
            ),
            # Carry poller ask when present; otherwise seed from the shortlist
            # row so the UI is not blank and the next structure pass has a print.
            "last_ask": seeded_ask,
            "updated_ts": t0,
            **_admission_fields(row, prev, float(t0)),
        }
        # Poller timing state must survive this rebuild. Without it the 2s desk
        # sync wiped live %R (and the last block reason) every cycle, so the
        # wire stayed exhaustion_state=unknown even though poll_once had just
        # stamped pctr — and in-zone names refused under require_exhaustion_data.
        #
        # arm_streak/arm_streak_poll are here for the same reason and were the
        # same bug: this rebuild runs every 2s and the arm poll every ~13s, so
        # a counter it did not carry could never reach two. See _arm_streak.
        if isinstance(prev.get("indicator"), dict) and prev["indicator"]:
            rec["indicator"] = dict(prev["indicator"])
        for k in (
            "block_code", "block_reason", "block_ts", "block_detail",
            "exh_was_overbought", "exh_was_oversold", "os_square_since", "left_os_since",
            "pctr_fall_since", "last_trade", "last_ask_src",
            # Keep the quote clock across the 2s rebuild — dropping age/ts
            # left stale_tape rows with age=None while engine still had 3–14m
            # prints (AEHG/LABX), which made UI and drop logic disagree.
            "last_ask_age_sec", "last_ask_ts", "price_age_sec", "price_src",
            "zone_touch_ts", "arm_streak", "arm_streak_poll",
            "confirm_ask", "confirm_ask_ts", "confirm_px_src",
            "arm_confirm_rsi_max",
            "stale_feed_since", "stale_tape_streak",
            "seat_role", "pin_stream_ready_sec", "pin_dead_since",
            "unarmable_since", "scout_until", "scout_only",
            "arm_ready", "arm_ready_reason", "admit_chg_band",
            "exh_seat_class", "exh_seat_class_admit", "pctr_gap",
            "far_exh_since", "square_since",
        ):
            if prev.get(k) is not None:
                rec[k] = prev[k]
        # Fresh soft-seed / inclusion stamps win over stale prev.
        for k in ("arm_ready", "arm_ready_reason", "admit_chg_band",
                  "scout_only", "scout_until", "exh_seat_class", "pctr_gap"):
            if row.get(k) is not None:
                rec[k] = row[k]
        # Admit class freezes on first *known* seat; unknown may upgrade.
        maybe_freeze_exh_seat_class_admit(rec)
        # Seat roles: pin wins over warming; warming scout prefers fresh tag.
        prev_role = str(prev.get("seat_role") or "").strip().lower()
        row_role = str(row.get("seat_role") or "").strip().lower()
        if prev_role == "pin":
            rec["seat_role"] = "pin"
        elif row_role == "warming":
            rec["seat_role"] = "warming"
        elif str(rec.get("seat_role") or "").lower() == "warming":
            exh = exhaustion_pct(rec)
            ind = rec.get("indicator") if isinstance(rec.get("indicator"), dict) else {}
            rising = bool(ind["pctr_rising"]) if "pctr_rising" in ind else None
            if not is_warming_exh_profile(
                exh, rising, cfg if isinstance(cfg, dict) else {},
                allow_unknown=exh is None,
                row=rec, ind=ind,
            ):
                rec.pop("seat_role", None)
        # Attach a zone immediately on admission — do not wait up to 20s for
        # poll_once REST. Mom/ST names were stuck on "no zone" until then.
        try:
            from config import load_config as _lc
            cfg_z = _lc() or {}
            ask_for_zone = _positive_price(seeded_ask)
            tape = live_print(sym)
            max_age = decision_max_age_sec(cfg_z)
            if (
                tape is not None
                and tape[1] is not None
                and tape[1] <= max_age
                and tape[0] > 0
            ):
                rec["last_ask"] = float(tape[0])
                rec["last_ask_src"] = "stream"
                rec["last_ask_age_sec"] = tape[1]
                # Keep quote clock in map (same as apply_decision_price /
                # book_table_rows). Sync used to leave a stale _LAST_QUOTE_TS
                # beside a fresh stream print → book paint restamped stale.
                rec["last_ask_ts"] = float(t0) - float(tape[1])
                _set_quote_ts(str(sym).upper().strip(),
                              float(t0) - float(tape[1]))
                # Sync copies block_code from prev then may stamp stream —
                # clear sticky stale_quote here or the file keeps both.
                clear_tape_data_block_if_stream_fresh(rec, cfg_z)
                ask_for_zone = float(tape[0])
            if ask_for_zone and not _structure_usable(rec.get("structure")):
                ensure_offset_zone_if_needed(rec, ask_for_zone, cfg_z, t0)
            # Refresh %R on the same print we would arm with, every 2s sync.
            if ask_for_zone:
                ensure_live_exhaustion(
                    rec, ask_for_zone, cfg_z, t0,
                    sig=_sync_indicators.get(sym))
            # And the RSI half, from the wire the engine refreshes every
            # second. Without this the book's RSI is a poll_once artefact,
            # up to ai_watch_poll_sec (20s) behind the reading the operator
            # is watching move on the chart.
            refresh_engine_macd(rec, _sync_indicators.get(sym))
            if refresh_engine_rsi(rec, _sync_indicators.get(sym)):
                _restamp_rsi_block(rec, cfg_z, t0)
        except Exception:
            pass
        # When the panels last offered this name. The grace below measures
        # from here rather than from admission, so a name the panels keep
        # offering never ages out of it.
        rec["last_candidate_ts"] = t0
        new_state[sym] = rec

    # Force Finnhub priority + subscribe for every live watch name (and
    # especially brand-new admits) so AEHG/AOUT are not left on REST while
    # SCAN already prints. Cheap when already subscribed.
    try:
        newly = [
            s for s in new_state
            if s not in old or not isinstance(old.get(s), dict)
        ]
        ensure_watch_stream(list(new_state.keys()) or newly)
    except Exception:
        pass

    # Keep in-flight paper entries even if they left the panels (still managing).
    # Also keep daily A/X duel champions (research) — desk-only sync would drop them.
    # Elite-6 pins stay on the book off-panel (soft-seed thrash must not evict).
    _pin_keep_syms: list[str] = []
    for sym, rec in old.items():
        if not isinstance(rec, dict):
            continue
        key = str(sym or rec.get("symbol") or "").upper().strip()
        if not key or key in new_state:
            continue
        status = str(rec.get("status") or "").lower().strip()
        is_duel = bool(rec.get("duel") or rec.get("duel_source"))
        is_pin = str(rec.get("seat_role") or "").strip().lower() == "pin"

        # ADMISSION GRACE. The book is rebuilt from THIS cycle's candidates,
        # so a name that momentarily fails one inclusion filter loses its row
        # — and with it the zone structure, the admit stamp and the arm
        # streak. Marginal names flicker: rvol crossing 2.0 or pct_change
        # crossing zero drops and re-adds the same symbol every cycle.
        #
        # Survivable while arming took one good poll. Not now:
        # ai_watch_arm_confirm_ticks wants CONSECUTIVE agreeing polls, and a
        # name that leaves the book between two can never accumulate any — the
        # confirmation would quietly exclude the borderline names it was never
        # aimed at. This keeps the ROW alive, not the verdict; every gate
        # still runs on every poll. 0 disables.
        if status not in ("submitted", "filled") and not is_duel and not is_pin:
            if is_levered_etp(key):
                continue
            try:
                _grace = float((cfg or {}).get("ai_watch_admit_grace_sec", 0) or 0)
            except (TypeError, ValueError):
                _grace = 0.0
            cfg_g = cfg if isinstance(cfg, dict) else {}
            if (
                _grace > 0
                and not _dead_reentry_blocked(key, t0, cfg_g)
                and not _stale_timeout_blocked(key, t0, cfg=cfg_g, row=rec)
                and not _no_stream_strike_demoted(key, t0, cfg_g)
            ):
                _seen = _f_or_none(rec.get("last_candidate_ts"))
                if _seen is not None and (t0 - _seen) <= _grace:
                    new_state[key] = dict(rec)
                    continue

        if status in ("submitted", "filled") or is_duel or is_pin:
            if status in ("invalidated", "expired") and not is_duel and not is_pin:
                continue
            if (
                status not in ("submitted", "filled")
                and (
                    is_levered_etp(key)
                    or _dead_reentry_blocked(
                        key, t0, cfg if isinstance(cfg, dict) else {}
                    )
                )
            ):
                continue
            kept = dict(rec)
            kept["symbol"] = key
            new_state[key] = kept
            if is_pin and status not in ("submitted", "filled"):
                _pin_keep_syms.append(key)

    # Promote / demote pins by metrics; ensure stream on promote + pin re-keep.
    _pin_events: list = []
    try:
        import ai_positions as _cp_pin
    except Exception:  # noqa: BLE001
        _cp_pin = None
    try:
        _promoted = _apply_pin_roles(
            new_state, cfg=cfg if isinstance(cfg, dict) else {},
            now=t0, events=_pin_events, cp=_cp_pin)
    except Exception:  # noqa: BLE001
        _promoted = []
    for _psym in _pin_keep_syms:
        _prec = new_state.get(_psym)
        if isinstance(_prec, dict) and str(
            _prec.get("seat_role") or ""
        ).strip().lower() == "pin":
            try:
                _log_pin_event(
                    _pin_events, _cp_pin, "pin_keep", symbol=_psym,
                    reason="off_panel", rec=_prec)
            except Exception:  # noqa: BLE001
                pass
    try:
        _stream_syms = list(dict.fromkeys(
            list(_promoted or []) + list(_pin_keep_syms or [])))
        if _stream_syms:
            ensure_watch_stream(_stream_syms)
    except Exception:
        pass

    # Stamp pin/scout counts onto admit_funnel when present (cheap).
    try:
        _pin_n = sum(
            1 for r in new_state.values()
            if isinstance(r, dict)
            and str(r.get("seat_role") or "").lower() == "pin"
        )
        _scout_n = sum(
            1 for r in new_state.values()
            if isinstance(r, dict)
            and str(r.get("seat_role") or "").lower() == "warming"
        )
        _funnel_path = REPORT_DIR / "admit_funnel.json"
        if _funnel_path.is_file():
            _funnel = json.loads(_funnel_path.read_text(encoding="utf-8"))
            if isinstance(_funnel, dict):
                _funnel["pin_n"] = _pin_n
                _funnel["scout_n"] = _scout_n
                _tmp = _funnel_path.with_suffix(".json.tmp")
                _tmp.write_text(json.dumps(_funnel, indent=2), encoding="utf-8")
                _tmp.replace(_funnel_path)
    except Exception:
        pass

    save_watch(new_state)
    # Book membership is the universe that needs live quotes + indicators.
    # Candidate push above only covers the shortlist *before* admission; names
    # already on the book (held, duel, sticky last_ask) need the same wire.
    try:
        push_candidates_to_engine(list(new_state.keys()))
    except Exception:
        pass
    return new_state


def prune_desk_watches(
    cfg: dict | None = None,
    now: float | None = None,
) -> dict:
    """Compatibility wrapper — full sync is the source of truth now."""
    return sync_watch_from_source_panels(cfg=cfg, now=now)


def rebuild_watch_from_book(
    rows: list[dict],
    cfg: dict,
    now: float,
) -> dict:
    """After research/open-bell: re-mirror all four source panels.

    ``rows`` is accepted for API compatibility; the live board files, desk heat
    and the dashboard's bb_live history are the source of truth (see
    ``sync_watch_from_source_panels``).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    # Optional: ensure latest research rows hit the wire before sync
    # (caller already wrote suggestions files).
    _ = rows
    return sync_watch_from_source_panels(cfg=cfg, now=now)


def _bar_seconds(cfg: dict) -> float:
    try:
        bar_sec = float(cfg.get("ai_watch_db_bar_seconds", 60.0) or 60.0)
    except (TypeError, ValueError):
        bar_sec = 60.0
    return bar_sec if bar_sec > 0 else 60.0


def _rte_fast_length(cfg: dict) -> int:
    try:
        length = int(cfg.get("rte_fast_length", 21) or 21)
    except (TypeError, ValueError):
        length = 21
    return max(2, length)


def _rte_slow_length(cfg: dict) -> int:
    try:
        length = int(cfg.get("rte_slow_native_length", 112) or 112)
    except (TypeError, ValueError):
        length = 112
    return max(2, length)


def tv_exh_rsi_enabled(cfg: dict | None) -> bool:
    """True when the desk uses both %R lines then CM RSI (no MACD, no zone).

    Missing key is off so unit-test cfg dicts keep the old heat gate.
    Live bot_config / DEFAULT_CONFIG set the flag on.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    return bool(cfg.get("ai_watch_tv_exh_rsi"))


def _cached_ohlc_stamps(symbol: str, cfg: dict, now: float) -> list[float] | None:
    """Bar timestamps aligned with ``symbol_ohlc``, or None if unknown."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None
    try:
        refresh = float(cfg.get("ai_watch_db_bar_refresh_sec", 120.0) or 120.0)
    except (TypeError, ValueError):
        refresh = 120.0
    max_age = max(60.0, refresh * 3.0)
    with _ohlc_cache_lock:
        hit = _ohlc_ts_cache.get(sym)
        rows = _ohlc_cache.get(sym)
        if not hit or (now - hit[0]) >= max_age:
            return None
        stamps = list(hit[1])
        ohlc = list(rows[1]) if rows and (now - rows[0]) < max_age else []
    if not stamps or (ohlc and len(stamps) != len(ohlc)):
        return None
    return stamps


def clock_window_rows(
    symbol: str,
    cfg: dict,
    now: float,
    *,
    rows: list[tuple[float, float, float]] | None = None,
    length: int | None = None,
) -> tuple[list[tuple[float, float, float]], float | None]:
    """Bars that actually sit in the last N minutes of the 1m window.

    Last-N-*prints* is not a 21-minute %R: on IEX a thin name's 21 prints can
    span an hour, and Williams %R then reports position in that hour's range.
    That is what produced OMER-class ``98% OB`` readings that do not match a
    1-minute %R(21) chart.

    When timestamps exist, keep only bars whose stamp is within
    ``(length-1) * bar_seconds * slack`` of the newest bar. Slack default 1.25
    allows a couple of missing minutes. The list may be shorter than
    ``length`` — ``live_exhaustion`` then uses a range %R instead of inventing
    a 21-bar window from older prints.
    """
    length = int(length) if length is not None else _rte_fast_length(cfg)
    length = max(2, length)
    bar_sec = _bar_seconds(cfg)
    try:
        slack = float(cfg.get("ai_watch_exhaustion_clock_slack", 1.25) or 1.25)
    except (TypeError, ValueError):
        slack = 1.25
    slack = max(1.0, slack)
    stream_got: tuple[list, float | None] | None = None
    if rows is None and _stream_bars_live(cfg):
        try:
            import stream_bars
            filled, fspan = stream_bars.filled_clock_rows(
                symbol, now, length, slack)
            if filled:
                stream_got = (filled, fspan)
                if len(filled) >= length:
                    return filled, fspan
        except Exception:
            stream_got = None
    if rows is None:
        rows = symbol_ohlc(symbol, cfg, now)
    stamps = _cached_ohlc_stamps(symbol, cfg, now)
    if stamps and len(stamps) == len(rows) and rows:
        horizon = (length - 1) * bar_sec * slack
        newest = float(stamps[-1])
        cutoff = newest - horizon
        paired = [
            (r, float(ts)) for r, ts in zip(rows, stamps)
            if ts is not None and float(ts) + 1e-9 >= cutoff
        ]
        span = (
            paired[-1][1] - paired[0][1]
            if len(paired) >= 2 else None
        )
        iex_rows = [r for r, _ts in paired]
        if stream_got and len(stream_got[0]) >= len(iex_rows):
            return stream_got
        return iex_rows, span

    # No stamps: last-N-prints plus the existing stretch cap.
    if stream_got and len(stream_got[0]) >= min(length, max(len(rows), 1)):
        return stream_got
    if len(rows) < length:
        return list(rows), None
    try:
        mult = float(cfg.get("ai_watch_exhaustion_max_window_mult", 3.0) or 0.0)
    except (TypeError, ValueError):
        mult = 3.0
    if mult > 0:
        span = window_span_sec(symbol, length, cfg, now)
        if span is not None and span > (length - 1) * bar_sec * mult:
            if stream_got:
                return stream_got
            return [], span
    return list(rows), window_span_sec(symbol, length, cfg, now)


def _raw_percent_r(hh: float, ll: float, close: float) -> float | None:
    span = hh - ll
    if span <= 0:
        return None
    return -100.0 * (hh - close) / span


def _live_percent_r_line(
    rows: list[tuple[float, float, float]],
    price: float,
    length: int,
    ewm_span: float,
    eps: float,
    *,
    min_range: int,
) -> tuple[float, bool, bool, str] | None:
    """(smoothed %R, rising, falling, src) against *price* as the live close."""
    if len(rows) < min_range:
        return None
    px = float(price)
    if len(rows) < length:
        hh = max([r[0] for r in rows] + [px])
        ll = min([r[1] for r in rows] + [px])
        live_raw = _raw_percent_r(hh, ll, px)
        if live_raw is None:
            return None
        prev_raw = _raw_percent_r(hh, ll, rows[-1][2])
        if prev_raw is None:
            prev_raw = live_raw
        return (
            live_raw,
            live_raw > prev_raw + eps,
            live_raw < prev_raw - eps,
            "clock_range",
        )
    series: list[float] = []
    for i in range(length - 1, len(rows)):
        win = rows[i - length + 1:i + 1]
        hh = max(r[0] for r in win)
        ll = min(r[1] for r in win)
        v = _raw_percent_r(hh, ll, win[-1][2])
        if v is not None:
            series.append(v)
    if not series:
        return None
    alpha = 2.0 / (max(1.0, float(ewm_span)) + 1.0)
    sm = series[0]
    for v in series[1:]:
        sm = alpha * v + (1.0 - alpha) * sm
    prev_sm = sm
    win = rows[-(length - 1):] if length > 1 else []
    hh = max([r[0] for r in win] + [px]) if win else px
    ll = min([r[1] for r in win] + [px]) if win else px
    live_raw = _raw_percent_r(hh, ll, px)
    if live_raw is None:
        return None
    live_sm = alpha * live_raw + (1.0 - alpha) * prev_sm
    return (
        live_sm,
        live_sm > prev_sm + eps,
        live_sm < prev_sm - eps,
        "live",
    )


def cm_rsi_trend_lookback(cfg: dict) -> int:
    """Bars back that "rising" is judged over. The engine's trend_lookback.

    strategy_three_indicator.DEFAULT_PARAMS["trend_lookback"] is 2 and the
    engine overrides it from THREE_IND_TREND_LOOKBACK, which load_config()
    never sees. So this defaults to 2 and takes an override off the desk
    config for the sims, which run with no engine at all.
    """
    for key in ("cm_rsi_trend_lookback", "trend_lookback"):
        raw = cfg.get(key)
        if raw is None:
            continue
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            continue
    return 2


def cm_rsi_series(closes: list[float], period: int) -> list[float]:
    """Wilder-RMA RSI over *closes*, the whole series.

    Extracted from ``live_cm_rsi`` unchanged so the shadow path that runs
    on stream-built bars uses the SAME arithmetic rather than a second
    copy that can drift. The series matters, not just its last value:
    "rising" needs the reading trend_lookback bars back, and RMA smoothing
    carries the entire history, so a shorter slice would not reproduce it.
    """
    period = max(2, int(period))
    alpha = 1.0 / period
    up = 0.0
    down = 0.0
    series: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        if i == 1:
            up, down = gain, loss
        else:
            up = alpha * gain + (1.0 - alpha) * up
            down = alpha * loss + (1.0 - alpha) * down
        if down == 0:
            series.append(100.0)
        elif up == 0:
            series.append(0.0)
        else:
            series.append(100.0 - (100.0 / (1.0 + up / down)))
    return series


def live_cm_rsi(
    symbol: str,
    price: float,
    cfg: dict,
    now: float,
) -> tuple[float, bool, bool] | None:
    """(RSI-2, green, rising) with the live print as the latest close.

    Green is Connors: close > SMA(200) and close < SMA(5) and RSI < 10.
    SMA(200) needs a full window; without it green is False but RSI still
    publishes so the 30-second trigger can fire.

    ``rising`` is the LEVEL's own direction — RSI now against RSI
    ``trend_lookback`` bars back on this same series, which is what
    strategy_three_indicator._rising does to publish cm_rsi_rising. It is
    returned here rather than derived by the caller because the whole point of
    the local path is that level and direction come off one series; splitting
    them across two frames is the bug the caller's comment describes.
    """
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None
    if px <= 0:
        return None
    rows = symbol_ohlc(symbol, cfg, now)
    if len(rows) < 3:
        return None
    closes = [float(r[2]) for r in rows] + [px]
    try:
        period = max(2, int(cfg.get("cm_rsi_length", 2) or 2))
    except (TypeError, ValueError):
        period = 2
    # The whole RSI series, not just its last value: "rising" needs the reading
    # from trend_lookback bars back, and recomputing it from a shorter slice
    # would not reproduce it — RMA smoothing carries the entire history.
    series = cm_rsi_series(closes, period)
    if not series:
        return None
    rsi = series[-1]
    sma5 = sum(closes[-5:]) / 5.0 if len(closes) >= 5 else None
    sma200 = sum(closes[-200:]) / 200.0 if len(closes) >= 200 else None
    green = bool(
        sma200 is not None and sma5 is not None
        and px > sma200 and px < sma5 and rsi < 10.0
    )
    # Flat is not rising, and too short a series is not rising either — both
    # match _rising (strict >, and False when the lookback index is negative).
    look = cm_rsi_trend_lookback(cfg)
    rising = len(series) > look and series[-1] > series[-1 - look]
    return float(rsi), green, bool(rising)


def _ema_series(values: list[float], span: int) -> list[float]:
    if not values or span <= 0:
        return []
    alpha = 2.0 / (float(span) + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1.0 - alpha) * out[-1])
    return out


def macd_series(
    closes: list[float],
    cfg: dict,
) -> tuple[list[float], list[float], list[float]] | None:
    """(fast line, signal line, histogram) over *closes*, or None if short.

    Extracted from live_macd so a replay can rebuild the same histogram the
    desk armed on. Direction (macd_gap_rising / macd_gap_falling) is
    published by the signal engine, and the sims run with no engine behind
    them — tools/sim_rstop_path.py has to derive it from the bars. A second
    copy of this arithmetic in tools/ would drift from the one the desk
    trades, which is why cm_rsi_series is shared the same way.
    """
    try:
        fast_p = int(cfg.get("macd_fast", 12) or 12)
        slow_p = int(cfg.get("macd_slow", 26) or 26)
        sig_p = int(cfg.get("macd_signal", 9) or 9)
    except (TypeError, ValueError):
        fast_p, slow_p, sig_p = 12, 26, 9
    if len(closes) < max(slow_p + sig_p, 20):
        return None
    ema_fast = _ema_series(closes, fast_p)
    ema_slow = _ema_series(closes, slow_p)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = _ema_series(macd_line, sig_p)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist


def live_macd(
    symbol: str,
    price: float,
    cfg: dict,
    now: float,
) -> dict | None:
    """Compute real-time MACD (fast line, slow signal line, histogram gap, std, sep_ratio, bull status)
    with the live trade print folded as the forming minute close.
    """
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None
    if px <= 0:
        return None
    rows = symbol_ohlc(symbol, cfg, now)
    if len(rows) < 15:
        return None
    closes = [float(r[2]) for r in rows] + [px]
    built = macd_series(closes, cfg)
    if built is None:
        return None
    macd_line, signal_line, hist = built

    cur_line = macd_line[-1]
    cur_sig = signal_line[-1]
    cur_gap = hist[-1]

    # Rolling standard deviation of histogram for scale-free gap measurement
    try:
        w = int(cfg.get("macd_sep_window", 50) or 50)
    except (TypeError, ValueError):
        w = 50
    hist_win = hist[-w:] if len(hist) >= w else hist
    if len(hist_win) >= 5:
        mean_h = sum(hist_win) / len(hist_win)
        var_h = sum((x - mean_h) ** 2 for x in hist_win) / (len(hist_win) - 1)
        std_h = math.sqrt(var_h) if var_h > 0 else 0.0
    else:
        std_h = 0.0

    sep_ratio = round(cur_gap / std_h, 2) if std_h > 0 else None

    try:
        cw = int(cfg.get("confirm_window", 8) or 8)
    except (TypeError, ValueError):
        cw = 8
    lo = max(0, len(macd_line) - cw - 1)
    bull_cross = False
    for k in range(lo, len(macd_line) - 1):
        if macd_line[k] <= signal_line[k] and macd_line[k + 1] > signal_line[k + 1]:
            bull_cross = True
            break

    is_bull = cur_line > cur_sig

    try:
        sep_mult = float(cfg.get("macd_sep_mult", 0.8) or 0.8)
        min_gap = float(cfg.get("macd_min_gap", 0.005) or 0.005)
    except (TypeError, ValueError):
        sep_mult, min_gap = 0.8, 0.005

    macd_ok = is_bull and cur_gap >= min_gap and (std_h <= 0 or cur_gap >= sep_mult * std_h)

    return {
        "macd_fast": round(cur_line, 4),
        "macd_slow": round(cur_sig, 4),
        "macd_gap": round(cur_gap, 4),
        "macd_hist": round(cur_gap, 4),
        "macd_hist_std": round(std_h, 4) if std_h > 0 else None,
        "macd_sep_ratio": sep_ratio,
        "macd_bull": is_bull,
        "macd_cross": bull_cross,
        "macd_ok": macd_ok,
        "macd_src": "realtime",
    }


def live_exhaustion_pair(
    symbol: str,
    price: float,
    cfg: dict,
    now: float,
) -> dict | None:
    """Fast + slow live %R. None when the fast line cannot form at all."""
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None
    if px <= 0:
        return None
    try:
        min_range = int(cfg.get("ai_watch_exhaustion_min_range_bars", 6) or 6)
    except (TypeError, ValueError):
        min_range = 6
    min_range = max(2, min_range)
    try:
        eps = float(cfg.get("rte_direction_eps", 0.05) or 0.0)
    except (TypeError, ValueError):
        eps = 0.05
    try:
        fast_span = float(cfg.get("rte_fast_ewm_span", 7) or 7)
    except (TypeError, ValueError):
        fast_span = 7.0
    try:
        slow_span = float(cfg.get("rte_slow_ewm_span", 3) or 3)
    except (TypeError, ValueError):
        slow_span = 3.0

    fast_len = _rte_fast_length(cfg)
    slow_len = _rte_slow_length(cfg)
    fast_rows, fast_span_sec = clock_window_rows(
        symbol, cfg, now, length=fast_len)
    fast = _live_percent_r_line(
        fast_rows, px, fast_len, fast_span, eps, min_range=min_range)
    if fast is None:
        return None
    slow_rows, slow_span_sec = clock_window_rows(
        symbol, cfg, now, length=slow_len)
    slow = _live_percent_r_line(
        slow_rows, px, slow_len, slow_span, eps, min_range=slow_len)
    out = {
        "fast": fast[0],
        "fast_rising": fast[1],
        "fast_falling": fast[2],
        "fast_src": fast[3],
        "fast_bars": len(fast_rows),
        "fast_window_sec": fast_span_sec,
        "slow": None if slow is None else slow[0],
        "slow_rising": None if slow is None else slow[1],
        "slow_falling": None if slow is None else slow[2],
        "slow_src": None if slow is None else slow[3],
        "slow_bars": len(slow_rows),
        "slow_window_sec": slow_span_sec,
    }
    try:
        thr = float(cfg.get("rte_threshold", 20) or 20)
    except (TypeError, ValueError):
        thr = 20.0
    try:
        tight_max = float(cfg.get("rte_confluence_max", 15) or 15)
    except (TypeError, ValueError):
        tight_max = 15.0
    if out["slow"] is not None:
        out["ob"] = out["fast"] >= -thr and out["slow"] >= -thr
        out["gap"] = abs(out["fast"] - out["slow"])
        out["tight"] = bool(out["ob"] and out["gap"] <= tight_max)
    else:
        out["ob"] = False
        out["gap"] = None
        out["tight"] = False
    return out


def live_exhaustion(
    symbol: str,
    price: float,
    cfg: dict,
    now: float,
) -> tuple[float, float, bool, bool] | None:
    """(%R, exhaustion_pct, rising, falling) recomputed against the LIVE price.

    Returns None when there is not enough bar history to form the window.

    The engine's reading is 60-120s behind the tape: a 1-minute bar has to
    close, then scan_interval_sec has to come round, and signal_proximity
    carries no timestamp so a consumer cannot even tell a fresh reading from a
    repeat. Against 6-minute holds that is not a lag, it is a different market
    — the price that triggers the zone is live while the exhaustion read
    describes two minutes ago.

    No new data is needed to fix it. Williams %R is
    ``-100 * (hh - close) / (hh - ll)`` and only ``close`` moves tick to tick:
    the window's high and low come from bars this module already caches for
    zone sizing. So the closed bars supply the window, the live price supplies
    the close, and the value updates as fast as quotes arrive.

    Smoothing is carried forward incrementally rather than recomputed. The
    engine applies EWM(span=7) to the raw series; one EWM step is
    ``a*x + (1-a)*prev`` with ``a = 2/(span+1)``, so advancing the last closed
    bar's smoothed value by the live raw reading reproduces what the engine
    would publish at the next bar close, without waiting for it.
    """
    got = live_exhaustion_pair(symbol, price, cfg, now)
    if got is None or got.get("fast") is None:
        return None
    pctr = float(got["fast"])
    ex = max(0.0, min(100.0, 100.0 + pctr))
    return pctr, ex, bool(got["fast_rising"]), bool(got["fast_falling"])


def _clear_stale_pctr(rec: dict, *, reason: str, now: float) -> None:
    """Drop a published %R that we can no longer stand behind.

    Leaving the last good print up after the clock window goes sparse is how
    a 98% OB reading outlives the 21-minute chart it was supposed to describe.
    """
    ind = rec.get("indicator")
    if not isinstance(ind, dict):
        return
    if ind.get("pctr") is None and ind.get("pctr_src") == reason:
        return
    ind["pctr"] = None
    ind["pctr_slow"] = None
    ind["pctr_ok"] = False
    ind["pctr_ob"] = False
    ind["pctr_tight"] = False
    ind["pctr_rising"] = False
    ind["pctr_falling"] = False
    ind["pctr_slow_falling"] = False
    ind["pctr_src"] = reason
    ind["pctr_ts"] = float(now)
    for k in ("pctr_raw", "pctr_hh", "pctr_ll", "pctr_bars", "pctr_window_sec",
              "pctr_gap", "pctr_px_src"):
        ind.pop(k, None)


def apply_live_exhaustion(rec: dict, price: float, cfg: dict, now: float) -> bool:
    """Overwrite a record's indicator %R with a live-price reading.

    True when a live value was written. Leaves the engine's other fields
    (cm_rsi, macd, sell_signal) untouched — this replaces the stale part, it
    does not invent the rest.
    """
    if not isinstance(rec, dict):
        return False
    if not bool(cfg.get("ai_watch_exhaustion_live", True)):
        return False
    sym = rec.get("symbol") or ""
    pair = live_exhaustion_pair(sym, price, cfg, now)
    if pair is None:
        rows, span = clock_window_rows(sym, cfg, now)
        # Only blank when we *saw* bars and they failed the clock window.
        # A cold cache (no rows yet) keeps whatever the engine last published.
        if rows or span is not None:
            _clear_stale_pctr(rec, reason="sparse_window", now=now)
        return False
    pctr = pair["fast"]
    rising = bool(pair["fast_rising"])
    falling = bool(pair["fast_falling"])
    ind = rec.get("indicator")
    if not isinstance(ind, dict):
        ind = {}
        rec["indicator"] = ind
    ind["pctr"] = round(float(pctr), 2)
    ind["pctr_rising"] = rising
    ind["pctr_falling"] = falling
    if pair.get("slow") is not None:
        ind["pctr_slow"] = round(float(pair["slow"]), 2)
        ind["pctr_slow_falling"] = bool(pair.get("slow_falling"))
        ind["pctr_slow_rising"] = bool(pair.get("slow_rising"))
    else:
        ind["pctr_slow"] = None
        ind["pctr_slow_falling"] = False
        ind["pctr_slow_rising"] = False
    ind["pctr_ob"] = bool(pair.get("ob"))
    ind["pctr_tight"] = bool(pair.get("tight"))
    if pair.get("gap") is not None:
        ind["pctr_gap"] = round(float(pair["gap"]), 2)
    rows, span = clock_window_rows(sym, cfg, now)
    length = _rte_fast_length(cfg)
    # EXH is the fast line. Slow is 112 bars and hover-only — requiring it
    # to also be live greys a real rolling %R(21) for most of the book.
    ind["pctr_src"] = pair.get("fast_src") or (
        "live" if len(rows) >= length else "clock_range"
    )
    ind["pctr_ts"] = float(now)
    # The RSI LEVEL and the RSI DIRECTION have to come off the same series.
    #
    # This used to overwrite cm_rsi with a local recompute over Alpaca IEX
    # REST bars while leaving cm_rsi_rising / cm_ok exactly as the engine
    # published them — those are computed on the engine's frame, which with
    # REALTIME_BARS on is the Finnhub trade stream. So "RSI is 20 and rising"
    # was a level from one series paired with a turn from another, and the
    # two disagreed hard: 2026-08-20 10:2x had BMNR at 5.5 / low=True on the
    # wire and 20.1 / low=False in the book at the same second.
    #
    # A rule shaped like "in the 0-50 band and trending up" cannot be built on
    # that pairing at all, so the local recompute is off by default and the
    # engine's reading stands whole. live_cm_rsi also has no clock window —
    # unlike live_exhaustion it reads raw symbol_ohlc rows, so its closes can
    # be stitched across the overnight gap — which is the second reason not to
    # prefer it. True restores the local recompute — and when it is on, the
    # DIRECTION moves with the level. It used to not: this block overwrote
    # cm_rsi and left cm_rsi_rising alone, which is the same two-frame pairing
    # the paragraph above rejects, only inverted. Behind an engine it merely
    # mixed frames; in a replay there is no engine, so cm_rsi_rising was never
    # written by anything, stayed falsy on every bar, and cm_rsi_allows_buy
    # answered rsi_not_rising forever. Every sweep that ever ran placed zero
    # trades and reported "no candidate, keep live config" off that.
    #
    # cm_ok is deliberately still the engine's and is NOT synthesised here —
    # it is a windowed composite, not a restatement of this reading. A replay
    # has none, so a sim that turns on ai_watch_arm_require_indicators (which
    # names cm_ok) will refuse everything; the sims run with it False.
    #
    # Stream-live RSI uses the SAME OHLC overlay as EXH (IEX seed + sampled
    # tape), so level and direction stay one series. The old local_iex path
    # is still available; it is the split-frame bug if left on without
    # rewriting rising.
    rsi_local = bool(cfg.get("ai_watch_cm_rsi_local", False))
    rsi_stream = _stream_bars_live(cfg)
    if rsi_local or rsi_stream:
        rsi_got = live_cm_rsi(sym, price, cfg, now)
        if rsi_got is not None:
            ind["cm_rsi"] = round(float(rsi_got[0]), 1)
            ind["cm_rsi_green"] = bool(rsi_got[1])
            ind["cm_rsi_rising"] = bool(rsi_got[2])
            ind["cm_rsi_src"] = "realtime" if rsi_stream else "local_iex"
            ind["cm_rsi_age_sec"] = 0.0 if rsi_stream else None
            try:
                buy_max = float(cfg.get("cm_rsi_buy_max", 10) or 10)
            except (TypeError, ValueError):
                buy_max = 10.0
            ind["cm_rsi_low"] = float(rsi_got[0]) <= buy_max
    macd_got = live_macd(sym, price, cfg, now)
    if macd_got is not None:
        for mk, mv in macd_got.items():
            ind[mk] = mv
    try:
        px = float(price)
    except (TypeError, ValueError):
        px = None
    win = (
        rows if len(rows) < length
        else (rows[-(length - 1):] if rows and length > 1 else [])
    )
    if win and px is not None and px > 0:
        ind["pctr_hh"] = round(max([r[0] for r in win] + [px]), 4)
        ind["pctr_ll"] = round(min([r[1] for r in win] + [px]), 4)
        raw_span = ind["pctr_hh"] - ind["pctr_ll"]
        if raw_span > 0:
            ind["pctr_raw"] = round(-100.0 * (ind["pctr_hh"] - px) / raw_span, 2)
    if rows:
        ind["pctr_bars"] = len(rows)
    if span is not None:
        ind["pctr_window_sec"] = round(float(span), 1)
    return True


def ensure_symbol_ohlc(
    symbol: str, cfg: dict, now: float,
) -> list[tuple[float, float, float]]:
    """Return OHLC for *symbol*, fetching bars when the cache is cold.

    ``symbol_ohlc`` deliberately does not fetch — the double-bottom / band
    rebuild used to be the only warmer. After a process restart the watch file
    still holds usable zones, so that path never runs, ``live_exhaustion`` sees
    an empty window forever, and every name refuses with
    ``no_exhaustion_data`` under ``ai_watch_require_exhaustion_data``.
    """
    rows = symbol_ohlc(symbol, cfg, now)
    fast_need = _rte_fast_length(cfg) + 2
    # A warm cache that can form the fast line is enough — don't refetch
    # every poll just because the slow 112-bar window is still short.
    if len(rows) >= fast_need:
        return rows
    need = max(fast_need, _rte_slow_length(cfg) + 2)
    cfg2 = dict(cfg or {})
    try:
        look = int(cfg2.get("ai_watch_db_lookback_bars", 220) or 220)
    except (TypeError, ValueError):
        look = 220
    cfg2["ai_watch_db_lookback_bars"] = max(look, need)
    _fetch_symbol_lows(symbol, cfg2, now)
    return symbol_ohlc(symbol, cfg, now)


def indicator_price(rec: dict, cfg: dict, now: float) -> tuple[float | None, str]:
    """The traded price %R should close on — never a quote.

    Williams %R is position-in-range of *traded* price, and the live close is
    folded into the window high (``max(bar_highs + [px])``), so handing it an
    ask that sits above the range makes the ask the high: %R comes back as
    exactly -0.0, EXH 100, "overbought", no matter where the stock is. The
    ask can only raise the high, so the failure only ever invents overbought.

    Order: the live tape, then the record's last tape print, then the newest
    closed bar. ``last_trade`` is safe to reach for because
    ``apply_decision_price`` only writes it for stream / stale_tape — a REST
    ask never lands there. The bar close is one bar behind but is a real
    trade, which is what the chart draws.

    ``(None, "none")`` when the desk holds no trade for the name at all; the
    caller blanks the column rather than drawing the line on an offer.
    """
    sym = str(rec.get("symbol") or "").upper().strip()
    if sym:
        tape = live_print(sym)
        if tape is not None and tape[0] and float(tape[0]) > 0:
            age = tape[1]
            if age is not None and age <= decision_max_age_sec(cfg):
                return float(tape[0]), "stream"
    last = _positive_price(rec.get("last_trade"))
    if last is not None:
        return last, "last_trade"
    rows = symbol_ohlc(sym, cfg, now) if sym else []
    if rows:
        close = _positive_price(rows[-1][2])
        if close is not None:
            return close, "bar_close"
    return None, "none"


def ensure_live_exhaustion(
    rec: dict, price: float, cfg: dict, now: float,
    sig: dict | None = None,
) -> bool:
    """Warm bar cache if needed and stamp live %R onto the watch record.

    Prefer the engine wire when it is Finnhub realtime and fresh — that is
    every trade, not a 2s sample of last. Local stream_bars is the fallback
    when the engine is not covering the name.

    Call this on every poll that has a usable price — not only when arming.
    The buy gate and the AI Watch exhaustion column both read
    ``rec['indicator']['pctr']``; without a warmer the column stays blank and
    the gate always sees ``unknown``.

    *price* says a usable print exists; it does not decide what the line
    closes on. Callers pass the decision price, which is the ask whenever the
    tape is quiet — see ``indicator_price`` for why an ask cannot be allowed
    to close a %R.
    """
    if not isinstance(rec, dict):
        return False
    if not bool(cfg.get("ai_watch_exhaustion_rules", True)):
        return False
    if not bool(cfg.get("ai_watch_exhaustion_live", True)):
        return False
    if refresh_engine_exh(rec, sig, cfg, now):
        return True
    # ENGINE AUTHORITATIVE. The local fallback below is not the engine's %R
    # with older data — it is a DIFFERENT indicator: a rolling window of
    # ai_watch_exh_bars against the engine's wr_length, recomputed off the
    # live print. Measured on AREN 2026-08-28 at the same instant, the two
    # disagreed by 48 points: engine %R -64.6 (EXH 35.4%) against local
    # -16.67 (EXH 83.3%), and opposite directions — engine rising, local
    # flat. The desk displayed and GATED on the local one, and the MACD
    # beside it came from the engine, so the confluence rule was combining
    # two indicators computed on different bars over different windows.
    #
    # With this on, a name the engine cannot cover simply has no %R, and
    # ai_watch_require_exhaustion_data decides what that means. Better a
    # missing reading than a confident wrong one.
    if bool(cfg.get("ai_watch_exhaustion_engine_only", False)):
        return False
    try:
        px = float(price)
    except (TypeError, ValueError):
        return False
    if px <= 0:
        return False
    sym = str(rec.get("symbol") or "").upper().strip()
    if not sym:
        return False
    ensure_symbol_ohlc(sym, cfg, now)
    if _stream_bars_live(cfg):
        try:
            import stream_bars
            # Fold the Finnhub last print when we have one — that is the
            # denser tape. Decision/ask is only the fallback so a quiet
            # name still fills the current minute.
            tape = live_print(sym)
            obs_px, obs_ts = px, now
            if tape is not None and tape[0] and float(tape[0]) > 0:
                obs_px = float(tape[0])
                age = tape[1]
                if age is not None and age >= 0:
                    obs_ts = now - float(age)
            stream_bars.observe(sym, obs_px, obs_ts)
            _overlay_stream_ohlc(sym, cfg, now)
        except Exception:
            pass
    px_src = "decision"
    if bool(cfg.get("ai_watch_exhaustion_trade_price_only", True)):
        got, px_src = indicator_price(rec, cfg, now)
        if got is None:
            _clear_stale_pctr(rec, reason="no_trade_price", now=now)
            return False
        px = got
    if not apply_live_exhaustion(rec, px, cfg, now):
        return False
    ind = rec.get("indicator")
    if isinstance(ind, dict):
        ind["pctr_px_src"] = px_src
    return True


def _price_in_or_below_zone(rec: dict, price: float, *, pad_pct: float = 0.0) -> bool:
    """True when *price* is inside the entry band or has fallen through it.

    These are the only geometries that can arm (in-zone) or still be a
    pullback-overshoot entry (below). Above-zone names do not need a fresh
    %R every 2s rebuild — the poller still stamps them on its cycle.
    """
    structure = rec.get("structure") if isinstance(rec, dict) else None
    levels = _structure_levels(structure) if isinstance(structure, dict) else None
    if levels is None:
        return False
    lo, hi = float(levels[0]), float(levels[1])
    if lo > hi:
        lo, hi = hi, lo
    try:
        px = float(price)
    except (TypeError, ValueError):
        return False
    if px <= 0 or lo <= 0 or hi <= 0:
        return False
    try:
        frac = max(0.0, float(pad_pct or 0.0)) / 100.0
    except (TypeError, ValueError):
        frac = 0.0
    high_bound = hi * (1.0 + frac)
    return px <= high_bound


def _rsi_wire_fields(rec: dict) -> dict:
    """CM RSI-2 for the book's RSI column.

    Third instance of the same omission. The MACD redesign shipped a column,
    a renderer, CSS and an arm gate with nothing putting the numbers on the
    wire; EXH did it before that. RSI had gone further still — feeds.js has
    carried _bookRsiText, _rsiArms, _rsiStale and _fmtRsiTitle the whole
    time, fully written, reading fields public_snapshot never sent.

    Provenance is not decoration here. The engine draws its bars from the
    Finnhub trade stream when the tape covers a name and falls back to Alpaca
    REST when it does not, and it flips per ticker mid-session — so a level
    without a source cannot be told apart from a level that is minutes old.
    The book dims the fallback rather than hiding it, because absent and
    stale want different reactions from the operator.

    Direction rides with the level for the same reason it does on MACD: the
    entry condition was a band AND a turn, and a bare number answers half of
    it.
    """
    ind = rec.get("indicator") if isinstance(rec, dict) else None
    if not isinstance(ind, dict):
        return {"cm_rsi": None, "cm_rsi_rising": None, "cm_rsi_green": False,
                "cm_rsi_low": False, "cm_rsi_src": None,
                "cm_rsi_age_sec": None}
    return {
        "cm_rsi": _f_or_none(ind.get("cm_rsi")),
        # None, not False — "no reading yet" and "not rising" are different
        # answers, and only the first should stop a gate from deciding.
        "cm_rsi_rising": (
            None if ind.get("cm_rsi_rising") is None
            else bool(ind.get("cm_rsi_rising"))),
        "cm_rsi_green": bool(ind.get("cm_rsi_green")),
        "cm_rsi_low": bool(ind.get("cm_rsi_low")),
        "cm_rsi_src": str(ind.get("cm_rsi_src") or "") or None,
        "cm_rsi_age_sec": _f_or_none(ind.get("cm_rsi_age_sec")),
    }


def _macd_wire_fields(rec: dict) -> dict:
    """MACD momentum for the book's MACD column (exit/display).

    The 8/26 redesign made MACD an entry lever and added the column. MACD no
    longer gates entry (only the optional bearish veto). Still on the wire
    for exits + desk display.
    Sibling of _exhaustion_wire_fields for exactly the same reason.

    Direction travels with size. Every other field here says how far apart
    the lines are; `macd_gap_rising` / `macd_gap_falling` say which way they
    are going, and a wide gap that is closing is momentum already over.
    `macd_gap_prev` rides along so the column can show the actual change
    rather than a bare boolean.
    """
    ind = rec.get("indicator") if isinstance(rec, dict) else None
    keys = ("macd_fast", "macd_slow", "macd_gap", "macd_sep_ratio",
            "macd_gap_prev")
    if not isinstance(ind, dict):
        out = {k: None for k in keys}
        out.update({"macd_bull": False, "macd_cross": False, "macd_ok": False,
                    "macd_gap_rising": None, "macd_gap_falling": None,
                    "macd_src": None, "macd_age_sec": None})
        return out
    gap = _f_or_none(
        ind.get("macd_gap") if ind.get("macd_gap") is not None
        else ind.get("macd_hist"))
    return {
        "macd_fast": _f_or_none(
            ind.get("macd_fast") if ind.get("macd_fast") is not None
            else ind.get("macd_line")),
        "macd_slow": _f_or_none(
            ind.get("macd_slow") if ind.get("macd_slow") is not None
            else ind.get("macd_signal")),
        "macd_gap": gap,
        "macd_src": str(ind.get("macd_src") or "") or None,
        "macd_age_sec": _f_or_none(ind.get("macd_age_sec")),
        "macd_sep_ratio": _f_or_none(ind.get("macd_sep_ratio")),
        "macd_bull": bool(ind.get("macd_bull")),
        "macd_cross": bool(ind.get("macd_cross")),
        "macd_ok": bool(ind.get("macd_ok")),
        # None, not False: "too few bars to say" and "not widening" are
        # different answers, and the arm gate refuses the first rather than
        # treating it as a held gap.
        "macd_gap_rising": (
            None if ind.get("macd_gap_rising") is None
            else bool(ind.get("macd_gap_rising"))),
        "macd_gap_falling": (
            None if ind.get("macd_gap_falling") is None
            else bool(ind.get("macd_gap_falling"))),
        "macd_gap_prev": _f_or_none(ind.get("macd_gap_prev")),
        # Provenance on the same wire as the number. Without these the book
        # showed a live-looking gap while State said "MACD src?" — or worse,
        # armed on a reading the operator could not audit.
        "macd_src": (str(ind.get("macd_src") or "").strip().lower() or None),
        "macd_age_sec": _f_or_none(ind.get("macd_age_sec")),
    }


def _exhaustion_wire_fields(rec: dict) -> dict:
    """Williams %R diagnostics for the EXH column tooltip."""
    ind = rec.get("indicator") if isinstance(rec, dict) else None
    if not isinstance(ind, dict):
        return {
            "pctr": None, "pctr_slow": None, "pctr_raw": None, "pctr_src": None,
            "pctr_rising": False, "pctr_falling": False,
            "pctr_ob": False, "pctr_tight": False, "pctr_gap": None,
            "cm_rsi": None, "cm_rsi_green": False, "cm_rsi_low": False,
            "cm_rsi_rising": False, "cm_rsi_src": None, "cm_rsi_age_sec": None,
            "exh_bars": None, "exh_window_min": None,
            "exh_hh": None, "exh_ll": None,
        }
    span = _f_or_none(ind.get("pctr_window_sec"))
    return {
        "pctr": _f_or_none(ind.get("pctr")),
        "pctr_slow": _f_or_none(ind.get("pctr_slow")),
        "pctr_raw": _f_or_none(ind.get("pctr_raw")),
        "pctr_src": str(ind.get("pctr_src") or "") or None,
        "pctr_rising": bool(ind.get("pctr_rising")),
        "pctr_falling": bool(ind.get("pctr_falling")),
        "pctr_ob": bool(ind.get("pctr_ob")),
        "pctr_tight": bool(ind.get("pctr_tight")),
        "pctr_gap": _f_or_none(ind.get("pctr_gap")),
        "cm_rsi": _f_or_none(ind.get("cm_rsi")),
        "cm_rsi_green": bool(ind.get("cm_rsi_green")),
        "cm_rsi_low": bool(ind.get("cm_rsi_low")),
        # Direction and provenance travel with the level, so the column can
        # show "22 and turning up, off the live tape" rather than a bare 22
        # that might be either series or either feed.
        "cm_rsi_rising": bool(ind.get("cm_rsi_rising")),
        "cm_rsi_src": str(ind.get("cm_rsi_src") or "") or None,
        "cm_rsi_age_sec": _f_or_none(ind.get("cm_rsi_age_sec")),
        "exh_bars": (
            int(ind["pctr_bars"])
            if isinstance(ind.get("pctr_bars"), (int, float))
            else None
        ),
        "exh_window_min": None if span is None else round(span / 60.0, 1),
        "exh_hh": _f_or_none(ind.get("pctr_hh")),
        "exh_ll": _f_or_none(ind.get("pctr_ll")),
        "macd_fast": _f_or_none(ind.get("macd_fast") if ind.get("macd_fast") is not None else ind.get("macd_line")),
        "macd_slow": _f_or_none(ind.get("macd_slow") if ind.get("macd_slow") is not None else ind.get("macd_signal")),
        "macd_gap": _f_or_none(ind.get("macd_gap") if ind.get("macd_gap") is not None else ind.get("macd_hist")),
        "macd_hist": _f_or_none(ind.get("macd_hist") if ind.get("macd_hist") is not None else ind.get("macd_gap")),
        "macd_hist_std": _f_or_none(ind.get("macd_hist_std")),
        "macd_sep_ratio": _f_or_none(ind.get("macd_sep_ratio")),
        "macd_bull": bool(ind.get("macd_bull") if ind.get("macd_bull") is not None else (_f_or_none(ind.get("macd_fast")) is not None and _f_or_none(ind.get("macd_slow")) is not None and float(ind.get("macd_fast")) > float(ind.get("macd_slow")))),
        "macd_cross": bool(ind.get("macd_cross")),
        "macd_ok": bool(ind.get("macd_ok")),
    }


def exhaustion_pct(record: dict) -> float | None:
    """0-100: how far this name has run toward overbought. None when unknown.

    Williams %R runs 0 at the top of its range to -100 at the bottom, so
    ``100 + %R`` reads as a plain percentage where 100 is pinned at the highs.
    The fast line is used: it is the desk's trigger scale, and the operator's
    question here ("is it heading into overbought right now") is a trigger
    question, not a setup one.

    None is a real answer and must not be coerced to 0: a missing reading
    scored as 0 would read as "deeply oversold", the exact opposite of "we do
    not know". (The ~18% no-indicator rate once cited here was inflated by the
    ascending-sort bar bug fixed 2026-08-11; the true blind rate is nearer 4%.
    The argument does not depend on the number.)
    """
    ind = record.get("indicator") if isinstance(record, dict) else None
    if not isinstance(ind, dict):
        return None
    raw = ind.get("pctr")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not (-100.5 <= v <= 0.5):
        return None
    return max(0.0, min(100.0, 100.0 + v))


def exh_square_arm_enabled(cfg: dict | None) -> bool:
    """Dual-%R OB+tight square arm (TV red ■). Default on once shipped."""
    cfg = cfg if isinstance(cfg, dict) else {}
    return bool(cfg.get("ai_watch_exh_square_arm", True))


def exh_heating_with_square(cfg: dict | None) -> bool:
    """Square ■ first, then last_heating on the same book.

    Off (default): a square miss is a hard refuse — no heating fall-through.
    On: square still arms when it matches; otherwise the legacy heat band
    (rising + dual-tight) may arm. Wed/Thu 2026-09-16/17 occupancy was
    last_heating; this keeps that lane without turning the square off.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    return bool(cfg.get("ai_watch_exh_heating_with_square", False))


def exh_mid_rise_arm_enabled(cfg: dict | None) -> bool:
    """ONE arm: fast %R crosses up through -50 with the slow line rising.

    When on it is the only exhaustion lane — square, oversold triangle and
    heating are not consulted. tools/entry_screen.py, 2026-09-14..22, events
    after admission: random minute 51.5% +1%-before--1%, mid_rise 51.0%,
    square 45.1% (z -2.6), live heating 45.0% (z -4.1). Default off.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    return bool(cfg.get("ai_watch_exh_mid_rise_arm", False))


# {symbol: (last fast %R seen, ts of the last upward -50 cross)}. Process
# local on purpose: the watch record is rebuilt by sync, and a crossing is a
# fact about two consecutive readings in this process, not about the book.
_MID_RISE_STATE: dict[str, tuple[float, float | None]] = {}
# Set on threads that only DISPLAY the arm (the book paint). They may read the
# latch but must not advance it: the paint rebuilds its record from the book
# row every publish (2-3 s), and advancing "last reading" from there consumed
# crosses the poll never saw — prev moved above -50 without a latch, so the
# poll read wait_mid_rise on SMCI 2026-09-25 12:00 while %R went -56.6 ->
# -49.6 -> -45.1 with the slow line rising.
_MID_RISE_PEEK = threading.local()


def _mid_rise_allows_buy(
    record: dict,
    cfg: dict,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """Arm for ai_watch_mid_rise_max_age_sec after fast %R crosses up through
    ai_watch_mid_rise_level (-50) while the slow %R is rising.

    The cross is detected between two consecutive readings of this symbol.
    Falling back under the level, or fast %R turning down, cancels it.
    """
    ind = record.get("indicator") if isinstance(record.get("indicator"), dict) else {}
    sym = str(record.get("symbol") or "").upper()
    fast = _f_or_none(ind.get("pctr"))
    if fast is None or not sym:
        return False, "no_exhaustion_data"
    try:
        level = float(cfg.get("ai_watch_mid_rise_level", -50.0))
    except (TypeError, ValueError):
        level = -50.0
    try:
        max_age = float(cfg.get("ai_watch_mid_rise_max_age_sec", 60.0) or 60.0)
    except (TypeError, ValueError):
        max_age = 60.0
    t = float(now if now is not None else time.time())
    prev, since = _MID_RISE_STATE.get(sym, (None, None))
    if (prev is not None and prev <= level < float(fast)
            and ind.get("pctr_slow_rising") is True):
        since = t
    if not getattr(_MID_RISE_PEEK, "on", False):
        _MID_RISE_STATE[sym] = (float(fast), since)
    if since is None:
        return False, "wait_mid_rise"
    if t - since > max_age:
        return False, "mid_rise_stale"
    if float(fast) <= level:
        return False, "mid_rise_lost"
    if ind.get("pctr_falling"):
        return False, "exh_falling"
    return True, "mid_rise"


def exh_oversold_triangle_arm_enabled(cfg: dict | None) -> bool:
    """TV %R Trend Exhaustion oversold triangle arm. Default True."""
    cfg = cfg if isinstance(cfg, dict) else {}
    return bool(cfg.get("ai_watch_exh_oversold_triangle_arm", True))


def _rte_threshold(cfg: dict | None) -> float:
    try:
        return float((cfg or {}).get("rte_threshold", 20) or 20)
    except (TypeError, ValueError):
        return 20.0


def _rte_confluence_max(cfg: dict | None) -> float:
    try:
        return float((cfg or {}).get("rte_confluence_max", 15) or 15)
    except (TypeError, ValueError):
        return 15.0


def dual_r_ob_tight(
    record: dict,
    cfg: dict | None = None,
    *,
    sticky: bool = False,
) -> tuple[bool | None, bool | None, str | None]:
    """Dual-%R square read: ``(both_ob, tight, refuse_reason)``.

    ``both_ob`` / ``tight`` are None when lines are missing.
    ``refuse_reason`` is set when the square cannot be evaluated or fails
    a hard presence check (``no_exhaustion_data``).

    ``sticky`` restores the pre-d7d05b5 OR-latch (cached ``pctr_ob`` /
    ``pctr_tight`` OR live math) and never writes the cache. Entry no longer
    uses it — ``_square_exh_allows_buy`` is live dual only (PSKY 2026-09-21
    false-■). Keep the flag for explicit tests / legacy callers.

    By default ``both_ob`` is live math only (fast ≥ −thr AND slow ≥ −thr).
    A sticky ``pctr_ob`` cache must not keep hold (or arm) after the lines
    have left OB — that OR-latch was the MARA 2026-09-18 exit lag and the
    PSKY 2026-09-21 entry false-■.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    ind = record.get("indicator") if isinstance(record.get("indicator"), dict) else {}
    fast = _f_or_none(ind.get("pctr"))
    slow = _f_or_none(ind.get("pctr_slow"))
    if fast is None or slow is None:
        return None, None, "no_exhaustion_data"
    thr = _rte_threshold(cfg)
    tight_max = _rte_confluence_max(cfg)
    gap = abs(float(fast) - float(slow))
    live_ob = float(fast) >= -thr and float(slow) >= -thr
    live_tight = gap <= tight_max + 1e-9
    if sticky:
        # Friday (pre-d7d05b5) entry semantics: a cached hold still counts.
        # Never writes, so a live-math caller earlier in the same poll
        # cannot silently defeat the latch.
        both_ob = bool(ind.get("pctr_ob")) or live_ob
        tight = bool(ind.get("pctr_tight")) or live_tight
        ind["pctr_gap"] = round(gap, 2)
        return bool(both_ob), bool(tight), None
    both_ob = live_ob
    tight = live_tight
    # Keep cache honest when callers pass a mutable indicator dict.
    ind["pctr_ob"] = bool(both_ob)
    ind["pctr_tight"] = bool(tight and both_ob)
    ind["pctr_gap"] = round(gap, 2)
    return bool(both_ob), bool(tight), None


def dual_r_os_tight(
    record: dict,
    cfg: dict | None = None,
) -> tuple[bool | None, bool | None, str | None]:
    """Dual-%R oversold read: ``(both_os, tight, refuse_reason)``.

    ``both_os`` / ``tight`` are None when lines are missing.
    ``refuse_reason`` is set when %R lines are missing (``no_exhaustion_data``).

    Live math: fast <= -100 + thr and slow <= -100 + thr (e.g. <= -80).
    tight: gap <= rte_confluence_max (e.g. <= 15).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    ind = record.get("indicator") if isinstance(record.get("indicator"), dict) else {}
    fast = _f_or_none(ind.get("pctr"))
    slow = _f_or_none(ind.get("pctr_slow"))
    if fast is None or slow is None:
        return None, None, "no_exhaustion_data"
    thr = _rte_threshold(cfg)
    os_level = -100.0 + thr
    tight_max = _rte_confluence_max(cfg)
    gap = abs(float(fast) - float(slow))
    live_os = float(fast) <= os_level and float(slow) <= os_level
    live_tight = gap <= tight_max + 1e-9
    ind["pctr_os"] = bool(live_os)
    ind["pctr_os_tight"] = bool(live_os and live_tight)
    ind["pctr_gap"] = round(gap, 2)
    return bool(live_os), bool(live_tight), None


def exh_pre_thr(cfg: dict | None = None) -> float:
    """Approach band for pre-square (both lines ≥ −pre_thr). Default 35."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return float(cfg.get("ai_watch_exh_pre_thr", 35.0) or 35.0)
    except (TypeError, ValueError):
        return 35.0


def admit_prefer_square(cfg: dict | None = None) -> bool:
    """Prefer square/pre-square seats on soft-seed / keep (default ON)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    return bool(cfg.get("ai_watch_admit_prefer_square", True))


def max_far_exh_seats(cfg: dict | None = None) -> int:
    """Cap on far dual-%R keep seats. <0 disables. Default 0 (starve far)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return int(cfg.get("ai_watch_max_far_exh_seats", 0))
    except (TypeError, ValueError):
        return 0


def far_exh_evict_sec(cfg: dict | None = None) -> float:
    """Seconds a far seat may stick before eviction. 0 disables."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(cfg.get("ai_watch_far_exh_evict_sec", 45.0) or 0.0))
    except (TypeError, ValueError):
        return 45.0


def exh_seat_class_counts(state: dict | None) -> dict[str, int]:
    """Count ``square`` / ``pre_square`` / ``os_square`` / ``os_triangle`` / ``far`` / ``unknown`` on the watch book."""
    out = {
        "n_square": 0,
        "n_pre_square": 0,
        "n_os_square": 0,
        "n_os_triangle": 0,
        "n_far": 0,
        "n_unknown": 0,
        "n_seats": 0,
    }
    if not isinstance(state, dict):
        return out
    for key, rec in state.items():
        if not isinstance(rec, dict):
            continue
        sym = str(rec.get("symbol") or key or "").upper().strip()
        if not sym or sym.startswith("_"):
            continue
        status = str(rec.get("status") or "").lower().strip()
        if status in ("closed", "expired", "cancelled", "dropped"):
            continue
        out["n_seats"] += 1
        cls = str(rec.get("exh_seat_class") or "").strip().lower()
        if not cls:
            cls, _ = classify_exh_seat(rec, None)
        if cls == "square":
            out["n_square"] += 1
        elif cls == "pre_square":
            out["n_pre_square"] += 1
        elif cls == "os_square":
            out["n_os_square"] += 1
        elif cls == "os_triangle":
            out["n_os_triangle"] += 1
        elif cls == "far":
            out["n_far"] += 1
        else:
            out["n_unknown"] += 1
    return out


def classify_exh_seat(
    row: dict | None,
    cfg: dict | None = None,
    *,
    ind: dict | None = None,
) -> tuple[str, float | None]:
    """Classify dual-%R seat quality for admit/seed/eviction.

    Returns ``(class, gap)`` where class is ``square`` | ``pre_square`` |
    ``far`` | ``unknown``.

    - square: both ≥ −rte_threshold and gap ≤ confluence
    - pre_square: both ≥ −pre_thr, gap ≤ confluence, and rising (fast or slow)
    - far: otherwise when both lines present
    - unknown: missing slow (or fast) — not pre-square
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    src = ind if isinstance(ind, dict) else None
    if src is None and isinstance(row, dict):
        nested = row.get("indicator")
        src = nested if isinstance(nested, dict) else row
    if not isinstance(src, dict):
        return "unknown", None
    fast = _f_or_none(src.get("pctr"))
    slow = _f_or_none(src.get("pctr_slow"))
    if fast is None or slow is None:
        return "unknown", None
    gap = abs(float(fast) - float(slow))
    thr = _rte_threshold(cfg)
    pre = exh_pre_thr(cfg)
    tight_max = _rte_confluence_max(cfg)
    tight = gap <= tight_max + 1e-9
    # Live math only — do not trust a sticky pctr_ob cache (same rule as
    # dual_r_ob_tight / left_overbought exit).
    both_ob = float(fast) >= -thr and float(slow) >= -thr
    if both_ob and tight:
        return "square", gap
    both_pre = float(fast) >= -pre and float(slow) >= -pre
    rising = src.get("pctr_rising")
    if rising is None and isinstance(row, dict):
        rising = _exh_rising_hint(row, src)
    slow_rising = src.get("pctr_slow_rising")
    rising_ok = (rising is True) or (slow_rising is True)
    if both_pre and tight and rising_ok:
        return "pre_square", gap
    if exh_oversold_triangle_arm_enabled(cfg):
        os_level = -100.0 + thr
        both_os = float(fast) <= os_level and float(slow) <= os_level
        if both_os and tight:
            if isinstance(row, dict):
                row["exh_was_oversold"] = True
            return "os_square", gap
        was_os = bool(
            (row and row.get("exh_was_oversold"))
            or (isinstance(src, dict) and src.get("pctr_os"))
        )
        if was_os and float(fast) > os_level and float(fast) < -thr and tight and rising_ok:
            return "os_triangle", gap
    return "far", gap


_KNOWN_EXH_SEAT_CLASSES = frozenset({"square", "pre_square", "far", "os_square", "os_triangle"})


def maybe_freeze_exh_seat_class_admit(rec: dict | None) -> str | None:
    """Freeze the first *known* dual-%R seat class for farm scorecards.

    ``unknown`` means dual lines were not ready yet — do **not** freeze it.
    Freezing unknown left every research/flood seat that later armed as
    square journaling ``exh_seat_class_admit=unknown`` (APLD 2026-09-21).
    """
    if not isinstance(rec, dict):
        return None
    cls = str(rec.get("exh_seat_class") or "").strip().lower()
    admit = str(rec.get("exh_seat_class_admit") or "").strip().lower()
    if admit in _KNOWN_EXH_SEAT_CLASSES:
        return admit
    if admit in ("unknown", "none"):
        rec.pop("exh_seat_class_admit", None)
        admit = ""
    if cls in _KNOWN_EXH_SEAT_CLASSES:
        rec["exh_seat_class_admit"] = cls
        return cls
    return None


def stamp_exh_seat_fields(
    row: dict,
    cfg: dict | None = None,
    *,
    ind: dict | None = None,
) -> str:
    """Stamp ``exh_seat_class`` + ``pctr_gap`` on *row*. Returns class."""
    cls, gap = classify_exh_seat(row, cfg, ind=ind)
    if isinstance(row, dict):
        row["exh_seat_class"] = cls
        if gap is not None:
            row["pctr_gap"] = round(float(gap), 2)
        elif "pctr_gap" not in row:
            row["pctr_gap"] = None
        maybe_freeze_exh_seat_class_admit(row)
    return cls


def is_overbought(record: dict, cfg: dict) -> bool | None:
    """True when %R has reached the overbought band. None when unknown.

    Square / TV desk mode (both lines): red boxes = fast AND slow >= -threshold.
    Legacy: fast line only (100 + %R >= 100 - threshold).
    """
    if tv_exh_rsi_enabled(cfg) or exh_square_arm_enabled(cfg):
        ind = record.get("indicator") if isinstance(record, dict) else None
        if not isinstance(ind, dict):
            return None
        # Square arm: live dual only — sticky pctr_ob must not label ■ after
        # TV has left OB+tight (PSKY 2026-09-21). TV-exh-rsi keeps the cache
        # short-circuit for its own path.
        if ind.get("pctr_ob") is True and not exh_square_arm_enabled(cfg):
            return True
        both_ob, _tight, err = dual_r_ob_tight(record, cfg)
        if err == "no_exhaustion_data":
            # Square mode needs both lines; unknown ≠ overbought.
            if exh_square_arm_enabled(cfg) and not tv_exh_rsi_enabled(cfg):
                fast = _f_or_none(ind.get("pctr"))
                if fast is None:
                    return None
                return False
            return None if _f_or_none(ind.get("pctr")) is None else False
        return bool(both_ob)
    ex = exhaustion_pct(record)
    if ex is None:
        return None
    thr = _rte_threshold(cfg)
    return ex >= (100.0 - thr)


def exhaustion_state(record: dict, cfg: dict) -> str:
    """'overbought' | 'heating' | 'cooling' | 'flat' | 'unknown'.

    The operator's three states plus the two honest extras: 'flat' when the
    line is neither rising nor falling, and 'unknown' when there is no reading.
    """
    ex = exhaustion_pct(record)
    if ex is None:
        return "unknown"
    ind = record.get("indicator") or {}
    if is_overbought(record, cfg):
        return "overbought"
    if ind.get("pctr_rising"):
        return "heating"
    if ind.get("pctr_falling"):
        return "cooling"
    return "flat"


def has_exhaustion(record: dict) -> bool:
    """True when this name has a usable %R reading right now."""
    return exhaustion_pct(record) is not None


def edge_mode(cfg: dict) -> str:
    """``continuation`` (default) or ``exhaustion_scalp``.

    continuation — Option A (2026-08-11 postmortem): arm earlier on heating,
    hold through overbought, bank via stop/T1/trail/dead-trade — not
    left_overbought. exhaustion_scalp — prior overbought-only arm + sell when
    %R leaves the band.
    """
    raw = str(cfg.get("ai_edge_mode") or "continuation").strip().lower()
    if raw in ("exhaustion", "exhaustion_scalp", "scalp", "ob", "overbought"):
        return "exhaustion_scalp"
    return "continuation"


def left_overbought_exit_enabled(cfg: dict) -> bool:
    """Whether software left_overbought may flatten a long.

    Default follows edge mode: off in continuation, on in exhaustion_scalp.
    Explicit ``ai_exit_left_overbought`` overrides when set.
    """
    if "ai_exit_left_overbought" in (cfg or {}):
        return bool(cfg.get("ai_exit_left_overbought"))
    return edge_mode(cfg) == "exhaustion_scalp"


def _hot_ob_source(record: dict) -> bool:
    """True when this name is desk-hot (trending / momentum), not a research sit.

    Already-overbought arms are allowed on these sources so a running name
    can still fill in or below the zone. Research / unknown stay on the
    90-cap. Cheap pullback/offset bands are refused separately
    (``cheap_ob_band``) even on this path — HCTI/BYSI were momentum.
    """
    src = str((record or {}).get("source") or "").strip().lower()
    if src in (
        "trending", "stocktwits", "st",
        "momentum", "mom", "mom_open",
        "bb_live", "bro",
    ):
        return True
    look = str(
        (record or {}).get("admit_look_reason")
        or (record or {}).get("look_reason")
        or ""
    ).strip().upper()
    if look == "EXT":
        return True
    crit = (record or {}).get("criteria") or (record or {}).get("admit_criteria") or []
    if isinstance(crit, (list, tuple)) and any(
        str(c).lower() in ("ext", "trending", "momentum") for c in crit
    ):
        return True
    return False


def _tv_exh_rsi_allows_buy(record: dict, cfg: dict) -> tuple[bool, str]:
    """%R red-box hold (both lines, optionally tight) then CM RSI-2 low."""
    ind = record.get("indicator") if isinstance(record.get("indicator"), dict) else {}
    fast = _f_or_none(ind.get("pctr"))
    slow = _f_or_none(ind.get("pctr_slow"))
    if fast is None or slow is None:
        if bool(cfg.get("ai_watch_require_exhaustion_data", True)):
            return False, "no_exhaustion_data"
        if bool(cfg.get("ai_watch_exhaustion_fallback", True)):
            return True, "no_exhaustion_fallback"
        return False, "no_exhaustion_data"
    try:
        thr = float(cfg.get("rte_threshold", 20) or 20)
    except (TypeError, ValueError):
        thr = 20.0
    both_ob = bool(ind.get("pctr_ob")) or (fast >= -thr and slow >= -thr)
    if not both_ob:
        return False, "wait_exh"
    try:
        tight_max = float(cfg.get("rte_confluence_max", 15) or 15)
    except (TypeError, ValueError):
        tight_max = 15.0
    gap = abs(fast - slow)
    tight = bool(ind.get("pctr_tight")) or gap <= tight_max
    if bool(cfg.get("rte_require_tight", True)) and not tight:
        return False, "exh_not_tight"
    rsi = _f_or_none(ind.get("cm_rsi"))
    if rsi is None:
        return False, "wait_rsi"
    try:
        buy_max = float(cfg.get("cm_rsi_buy_max", 10) or 10)
    except (TypeError, ValueError):
        buy_max = 10.0
    if rsi > buy_max:
        return False, "wait_rsi"
    return True, "exh_rsi"


def macd_reading_is_live(ind: dict | None, cfg: dict | None = None) -> tuple[bool, str]:
    """Is this MACD reading from the live tape, and young enough to trade?

    ``macd_src`` / ``bars_src`` must be ``realtime``. An age without a source
    is not provenance — that hole let a timestamped Alpaca fallback through
    as if it were the Finnhub tape.

    ``ai_watch_macd_max_age_sec`` 0 means source-only (any age). A positive
    ceiling also refuses a missing age (cannot prove freshness) and bars
    older than the ceiling.
    """
    ind = ind if isinstance(ind, dict) else {}
    cfg = cfg if isinstance(cfg, dict) else {}
    src = str(ind.get("macd_src") or ind.get("bars_src") or "").strip().lower()
    if src != "realtime":
        if not src:
            return False, "macd_src_unknown"
        return False, f"macd_not_realtime_{src}"[:40]
    age = _f_or_none(
        ind.get("macd_age_sec") if ind.get("macd_age_sec") is not None
        else ind.get("bars_age_sec"))
    try:
        max_age = float(cfg.get("ai_watch_macd_max_age_sec", 0) or 0)
    except (TypeError, ValueError):
        max_age = 0.0
    if max_age > 0:
        if age is None:
            return False, "macd_src_unknown"
        if age > max_age:
            return False, "macd_stale_bars"
    return True, ""


def macd_bearish_blocks_buy(
    record: dict,
    cfg: dict,
    *,
    fail_open_unknown: bool = True,
) -> str | None:
    """Refuse when the MACD lines are crossed down. Reason or ``None``.

    A veto, not a positive gate.

    Why it is its own knob. The retired ``require_macd`` gate bundled three different
    questions — DIRECTION (crossed down, gap closing), SIZE (macd_min_gap,
    macd_sep_mult) and AVAILABILITY (no_macd_data, macd_src_unknown,
    macd_stale_bars). Measured over 2026-08-31..09-04 the bundle refused
    84-94% of every arm decision the desk made, with macd_bearish the single
    largest reason each session (2,175-4,846/day). Turning the bundle off to
    stop the size and availability tests starving opens also drops the
    direction test, which is the half that says the trade is wrong rather
    than merely small. This keeps that half alone: a name whose fast line
    sits below its slow line is not an open, whatever the gap measures.

    Fail-open on missing MACD by default — failing closed here would reintroduce the availability refusals that the EXH+RSI
    arm path exists to avoid.
    """
    if not bool(cfg.get("ai_watch_macd_block_bearish", False)):
        return None
    ind = record.get("indicator") if isinstance(record, dict) else None
    ind = ind if isinstance(ind, dict) else {}
    fast = _f_or_none(
        ind.get("macd_fast") if ind.get("macd_fast") is not None
        else ind.get("macd_line"))
    slow = _f_or_none(
        ind.get("macd_slow") if ind.get("macd_slow") is not None
        else ind.get("macd_signal"))
    gap = _f_or_none(
        ind.get("macd_gap") if ind.get("macd_gap") is not None
        else ind.get("macd_hist"))
    if fast is None or slow is None or gap is None:
        if fail_open_unknown:
            return None
        if isinstance(record, dict):
            record["block_detail"] = "no realtime MACD (needs 1-min bars)"
        return "no_macd_data"
    # A negative histogram and a fast line at or under the signal are both
    # "crossed down".
    if fast > slow and gap > 0:
        return None
    if isinstance(record, dict):
        record["block_detail"] = (
            f"fast {fast:.4f} <= slow {slow:.4f} (gap {gap:+.4f})")
    return "macd_bearish"


def cm_rsi_allows_buy(record: dict, cfg: dict) -> tuple[bool, str]:
    """CM RSI-2 entry filter: inside the band AND turning up.

    The operator's rule, in their words: anything trending up from 0 to 50 is
    a good entry, never trending down. So this is a LEVEL test and a
    DIRECTION test, and both readings must come off the same series — see the
    note in apply_live_exhaustion about why that was not true before.

    Direction is the engine's ``cm_rsi_rising``, which is RSI-2 now against
    RSI-2 ``trend_lookback`` bars back (2 by default, strategy_three_indicator
    ``_rising``). Flat is not rising: on a 2-period RSI a flat print is
    usually a name that is not trading, not one that is turning.

    Exception (``ai_watch_arm_cm_rsi_allow_falling_below``): when RSI is still
    falling but deeply washed out (below that threshold) AND fast %R is already
    rising toward overbought (``pctr_rising``), allow the arm. EXH is the
    timing confirm; RSI only says "not chasing". 0 disables the exception.

    ``ai_watch_require_realtime_rsi`` additionally refuses a reading the
    engine drew on the REST fallback rather than the Finnhub tape — but only
    when this gate is on. When ``ai_watch_arm_require_cm_rsi`` is false the
    function short-circuits before level / rising / realtime checks, so RSI
    cannot veto arms (display + provenance elsewhere stay intact).
    """
    if not bool(cfg.get("ai_watch_arm_require_cm_rsi", False)):
        return True, "cm_rsi_off"
    ind = record.get("indicator") if isinstance(record, dict) else None
    ind = ind if isinstance(ind, dict) else {}

    rsi = _f_or_none(ind.get("cm_rsi"))
    if rsi is None:
        return False, "no_rsi_data"

    if bool(cfg.get("ai_watch_require_realtime_rsi", False)):
        src = str(ind.get("cm_rsi_src") or "").strip().lower()
        if src != "realtime":
            return False, f"rsi_not_realtime_{src or 'missing'}"

    try:
        band_max = float(cfg.get("ai_watch_arm_cm_rsi_max", 50.0))
    except (TypeError, ValueError):
        band_max = 50.0
    try:
        band_min = float(cfg.get("ai_watch_arm_cm_rsi_min", 0.0))
    except (TypeError, ValueError):
        band_min = 0.0
    if rsi > band_max:
        return False, "rsi_extended"
    if rsi < band_min:
        return False, "rsi_below_band"
    # Direction is optional because it is the weaker half. Replayed over 4,585
    # arms at a 15m horizon (tools/rsi_counterfactual.py):
    #   0-50 AND rising   7% of arms   +0.305%   win 54.8%
    #   0-50 only        37% of arms   +0.233%   win 49.3%
    #   rising only      54% of arms   +0.019%   win 49.8%
    # The band carries the edge. Requiring the turn as well buys a little more
    # per trade and a better win rate, at a fifth of the opportunities; the
    # turn on its own is indistinguishable from taking every arm.
    if bool(cfg.get("ai_watch_arm_cm_rsi_require_rising", True)):
        if not bool(ind.get("cm_rsi_rising")):
            try:
                fall_max = float(
                    cfg.get("ai_watch_arm_cm_rsi_allow_falling_below", 0.0) or 0.0)
            except (TypeError, ValueError):
                fall_max = 0.0
            # Deep OS + EXH already heating: waive the RSI turn.
            if (
                fall_max > 0
                and rsi < fall_max
                and bool(ind.get("pctr_rising"))
            ):
                return True, "rsi_deep_os_exh_heating"
            return False, "rsi_not_rising"
        return True, "rsi_turning_up"
    return True, "rsi_in_band"


def late_heat_blocks_buy(record: dict, cfg: dict) -> str | None:
    """Refuse a new long that is already overbought AND RSI-near-cap.

    Conjunction with a soft RSI floor (historically 55, below a hard max)
    separated HPE-class OB+high-RSI chases from BULL-class early OB heats.
    Off when ``ai_watch_soft_ob_enabled`` is false, the RSI floor is 0, or
    ``ai_watch_arm_require_cm_rsi`` is false (Plan A 2026-09-17: RSI is not
    an arm gate — do not reintroduce level vetoes via soft OB). Missing RSI
    abstains. Does not change MACD gap or the EXH override.
    """
    if not bool(cfg.get("ai_watch_soft_ob_enabled", False)):
        return None
    # RSI arm gate off → soft-OB RSI floor must not veto either.
    if not bool(cfg.get("ai_watch_arm_require_cm_rsi", False)):
        return None
    try:
        rsi_floor = float(cfg.get("ai_watch_soft_ob_rsi_min", 0.0) or 0.0)
    except (TypeError, ValueError):
        rsi_floor = 0.0
    if rsi_floor <= 0:
        return None
    if exhaustion_state(record, cfg) != "overbought":
        return None
    ind = record.get("indicator") if isinstance(record, dict) else None
    ind = ind if isinstance(ind, dict) else {}
    rsi = _f_or_none(ind.get("cm_rsi"))
    if rsi is None:
        return None
    if rsi + 1e-9 >= rsi_floor:
        return "late_heat"
    return None


def _note_confirm_rsi(record: dict) -> float | None:
    """Track peak cm_rsi across the current arm-confirm streak.

    GTLB 2026-09-04: confirm ticks printed ~59.3 then the pass tick 53.3.
    Soft OB never saw overbought, and a pass-only floor of 55 would have
    missed the chase. Peak across the streak catches that. Cleared when
    ``_arm_streak`` resets.
    """
    if not isinstance(record, dict):
        return None
    ind = record.get("indicator") if isinstance(record.get("indicator"), dict) else {}
    rsi = _f_or_none(ind.get("cm_rsi"))
    if rsi is None:
        return _f_or_none(record.get("arm_confirm_rsi_max"))
    prev = _f_or_none(record.get("arm_confirm_rsi_max"))
    if prev is None or rsi > prev:
        record["arm_confirm_rsi_max"] = float(rsi)
        return float(rsi)
    return float(prev)


def mistimed_heat_detail(record: dict, cfg: dict | None = None) -> str:
    """Operator/log detail for a mistimed_heat refuse."""
    ind = record.get("indicator") if isinstance(record, dict) else None
    ind = ind if isinstance(ind, dict) else {}
    rsi = _f_or_none(ind.get("cm_rsi"))
    peak = _f_or_none(record.get("arm_confirm_rsi_max")) if isinstance(record, dict) else None
    if peak is None:
        peak = rsi
    elif rsi is not None:
        peak = max(peak, rsi)
    exh = exhaustion_pct(record) if isinstance(record, dict) else None
    state = exhaustion_state(record, cfg or {}) if isinstance(record, dict) else "?"
    parts = [f"why=heating state={state}"]
    if rsi is not None:
        parts.append(f"rsi={rsi:.1f}")
    if peak is not None:
        parts.append(f"peak={peak:.1f}")
    if exh is not None:
        parts.append(f"exh={exh:.1f}")
    return " ".join(parts)


def mistimed_heat_blocks_buy(
    record: dict,
    cfg: dict,
    *,
    exh_why: str | None = None,
) -> str | None:
    """Refuse a heating-band arm that is already RSI-extended (GTLB chase).

    Soft OB (``late_heat``) only fires on **overbought** + RSI≥55. GTLB on
    2026-09-04 armed ``last_heating`` with confirm RSI ~59.3 / pass 53.3,
    EXH still in the heat band — soft OB never ran. MFE ~0.01R then local
    trail −0.13R.

    Rule (heating path only — BULL-class ``last_overbought`` + RSI 46 stays
    on soft OB and is untouched here):

      refuse when exh_why is heating AND (
          pass cm_rsi ≥ ai_watch_mistimed_heat_rsi_min   (default 52)
          OR confirm-window peak ≥ ai_watch_mistimed_heat_rsi_peak_min
             (default 55; 0 disables the peak leg)
      )

    Pass floor 52 blocks GTLB's 53.3 without needing the peak; the peak
    leg is the backup when RSI dips under the pass floor after printing
    hot on earlier confirm ticks. Early healthy heats (RSI ~46) clear
    both. Does not change RSI hard max 60, soft OB, macd_min_gap, or
    the EXH override.
    """
    if not bool(cfg.get("ai_watch_mistimed_heat_enabled", True)):
        return None
    # Aligned with soft OB: when CM RSI is not an arm gate, do not refuse
    # on RSI floors here either.
    if not bool(cfg.get("ai_watch_arm_require_cm_rsi", False)):
        return None
    why = str(exh_why or "").strip().lower()
    # Accept raw exhaustion why or the last_/zone_ wrapped form.
    if why.startswith("last_"):
        why = why[5:]
    elif why.startswith("zone_"):
        why = why[5:]
    if why != "heating":
        return None
    try:
        rsi_floor = float(cfg.get("ai_watch_mistimed_heat_rsi_min", 52.0) or 0.0)
    except (TypeError, ValueError):
        rsi_floor = 52.0
    try:
        peak_floor = float(
            cfg.get("ai_watch_mistimed_heat_rsi_peak_min", 55.0) or 0.0)
    except (TypeError, ValueError):
        peak_floor = 55.0
    if rsi_floor <= 0 and peak_floor <= 0:
        return None
    ind = record.get("indicator") if isinstance(record, dict) else None
    ind = ind if isinstance(ind, dict) else {}
    rsi = _f_or_none(ind.get("cm_rsi"))
    if rsi is None:
        return None
    peak = _f_or_none(record.get("arm_confirm_rsi_max")) if isinstance(record, dict) else None
    if peak is None:
        peak = rsi
    else:
        peak = max(float(peak), float(rsi))
    if rsi_floor > 0 and rsi + 1e-9 >= rsi_floor:
        return "mistimed_heat"
    if peak_floor > 0 and peak + 1e-9 >= peak_floor:
        return "mistimed_heat"
    return None


def exhaustion_allows_buy(
    record: dict, cfg: dict, *, now: float | None = None
) -> tuple[bool, str]:
    """Buy side of the exhaustion / momentum gate.

    TV desk mode: both %R lines in the overbought band (red boxes),
    optionally close together, then CM RSI-2 at/under buy_max.

    Legacy / continuation: buy when fast %R is **rising** (gaining heat),
    or the name is already **overbought and not falling**. Heat min/max
    still apply. With ``ai_watch_require_exh_rising`` (default on for
    paper scalp_legacy), falling EXH is an explicit refuse, flat EXH is
    refused except the pinned-ceiling + MACD-armed exemption, and a
    missing reading cannot fall through ``no_exhaustion_fallback``.

    Intent (2026-09-04): more early opens (low RSI + rising EXH + open
    MACD), fewer late chases (falling EXH / high RSI heat).
    """
    if not bool(cfg.get("ai_watch_exhaustion_rules", True)):
        return True, "exhaustion_off"
    require_rising = bool(cfg.get("ai_watch_require_exh_rising", True))
    # The reading has to BE the indicator before it is allowed to decide.
    #
    # pctr_src says how the number was produced. "live" is a rolling %R(length)
    # over a clock window, recomputed against the live print — the thing the
    # operator reads off a chart. "clock_range" means the window did not hold
    # length bars, so it reported position-in-range over whatever it had, and
    # "sparse_window" means it barely had anything. Those print in the same
    # column and mean something else: 2026-08-19 ran 57.9% live, 33.1%
    # clock_range, 8.5% sparse, with the window spanning 23 minutes at the
    # median and over sixteen hours at p90.
    #
    # Bars come from Alpaca IEX, a few percent of the consolidated tape, so a
    # thin name simply does not have a 1-minute bar every minute. That is a
    # data problem and the honest response is to decline the trade, not to
    # average the gap away.
    if bool(cfg.get("ai_watch_require_live_pctr", False)):
        ind = record.get("indicator") if isinstance(record, dict) else None
        ind = ind if isinstance(ind, dict) else {}
        src = str(ind.get("pctr_src") or "").strip().lower()
        if src != "live":
            return False, f"pctr_not_live_{src or 'missing'}"
    if tv_exh_rsi_enabled(cfg):
        return _tv_exh_rsi_allows_buy(record, cfg)
    # One arm (2026-09-23): the fast -50 cross replaces square, triangle and
    # heating. Exclusive — the lanes below are not consulted when it is on.
    # See legacy_arms.py for the settings those lanes own; live mid_rise
    # never imports that module.
    if exh_mid_rise_arm_enabled(cfg):
        return _mid_rise_allows_buy(record, cfg, now=now)
    # Square mode (TV red ■): enter on dual-OB + tight.
    # Oversold triangle mode: enter on oversold squares -> oversold triangle + RSI rising.
    sq_ok, sq_why = False, ""
    if exh_square_arm_enabled(cfg):
        sq_ok, sq_why = _square_exh_allows_buy(record, cfg, require_rising=require_rising, now=now)
        if sq_ok:
            return True, sq_why
    if exh_oversold_triangle_arm_enabled(cfg):
        os_ok, os_why = _oversold_triangle_allows_buy(record, cfg, now=now)
        if os_ok:
            return True, os_why
    if exh_square_arm_enabled(cfg) and not exh_heating_with_square(cfg):
        if exh_oversold_triangle_arm_enabled(cfg) and (
            bool(record.get("exh_was_oversold"))
            or os_why in ("oversold_squares", "stale_oversold_triangle", "still_oversold", "rsi_not_rising")
        ):
            return False, os_why
        return False, sq_why
    state = exhaustion_state(record, cfg)
    if state == "unknown":
        # Gaining-EXH rule needs a reading. Fallback used to arm blind when
        # require_exhaustion_data was false — that is a late/unknown chase,
        # not an early heat.
        if require_rising:
            return False, "exh_rising_required"
        if bool(cfg.get("ai_watch_require_exhaustion_data", True)):
            return False, "no_exhaustion_data"
        if bool(cfg.get("ai_watch_exhaustion_fallback", True)):
            return True, "no_exhaustion_fallback"
        return False, "no_exhaustion_data"
    ind = record.get("indicator") if isinstance(record.get("indicator"), dict) else {}
    # Falling EXH is never an open — explicit reason for the operator column.
    if ind.get("pctr_falling") or state == "cooling":
        if require_rising:
            return False, "exh_falling"
        if state == "overbought":
            return False, "not_rising_overbought"
        return False, f"not_rising_{state}"
    if state == "overbought":
        if bool(cfg.get("ai_watch_ob_allow_hot", True)) and _hot_ob_source(record):
            # Hot-OB free pass still needs rising when the gaining-EXH rule
            # is on — otherwise it re-opens the late-chase door.
            if require_rising and not ind.get("pctr_rising"):
                return False, "exh_not_rising"
            return True, "overbought_hot"
        # A name pinned at the top of its range cannot be "rising".
        #
        # Williams %R is position-in-range, so at 100% it is at the ceiling by
        # construction: pctr_rising and pctr_falling are BOTH False and the
        # test below refuses it as not_rising_overbought forever. Measured
        # 2026-08-27: CRMG 100.0%, CSIQ 100.0%, FIG 98.9% — all flat, all
        # refused, on a day the desk was hunting momentum. The strongest names
        # were the only ones structurally unreachable.
        #
        # So the level is allowed to stand in for the turn, but ONLY while
        # MACD is armed — bullish with an opening gap. That is the operator's
        # rule ("allow this when the MACD is armed"): the second indicator
        # supplies the direction %R has run out of room to express. A falling
        # %R is still refused above, so a rolling-over top cannot get in here.
        if bool(cfg.get("ai_watch_ob_allow_flat_when_macd_armed", False)):
            # ONLY where %R is genuinely out of room. The exemption exists
            # because a reading pinned at the ceiling cannot rise — 100% is
            # the top of the range by construction, so demanding a turn there
            # refuses the strongest names forever. That argument does not
            # extend to merely-overbought: GAP armed on this branch at 80.7%
            # on 2026-08-28 with nineteen points of headroom, where "not
            # rising" is a real refusal and not an artifact. It was flat by
            # choice of the tape, and the trade closed 79 seconds later on
            # macd_negative.
            try:
                pinned_at = float(cfg.get(
                    "ai_watch_ob_flat_min_pct", 99.0) or 99.0)
            except (TypeError, ValueError):
                pinned_at = 99.0
            ex_now = exhaustion_pct(record)
            if (ex_now is not None and ex_now + 1e-9 >= pinned_at
                    and _macd_is_armed(record)):
                return True, "overbought_macd_armed"
    ex = exhaustion_pct(record)
    raw_min = cfg.get("ai_watch_exhaustion_heat_min_pct", 50.0)
    try:
        heat_min = 50.0 if raw_min is None else float(raw_min)
    except (TypeError, ValueError):
        heat_min = 50.0
    raw_max = cfg.get("ai_watch_exhaustion_heat_max_pct", 0.0)
    try:
        heat_max = 0.0 if raw_max is None else float(raw_max)
    except (TypeError, ValueError):
        heat_max = 90.0
    if ex is None or ex + 1e-9 < heat_min:
        return False, "heating_too_low"
    if heat_max > 0 and ex + 1e-9 >= heat_max:
        return False, "already_extended"
    ind = record.get("indicator") if isinstance(record.get("indicator"), dict) else {}
    if not ind.get("pctr_rising"):
        if require_rising:
            return False, "exh_not_rising"
        return False, f"not_rising_{state}"
    if state == "overbought":
        return True, "overbought"
    # Legacy heating path (square arm off): still require dual-%R tight so
    # RKLB-class wide-gap heaters cannot last_heating.
    tight_ok, tight_why = _heating_dual_r_allows(record, cfg)
    if not tight_ok:
        return False, tight_why
    # Both %R lines rising together is the heating tell — bigger than the
    # heat level alone. Fast-only rising with a flat/falling slow is refuse.
    if not ind.get("pctr_slow_rising"):
        return False, "slow_not_rising"
    if not (
        ind.get("pctr_both_rising")
        or (ind.get("pctr_rising") and ind.get("pctr_slow_rising"))
    ):
        return False, "not_both_rising"
    # %R can rise inside a downtrend (GLND/IONQ 2026-09-23 heating fills).
    # Heating may arm only when the recent prints themselves are rising.
    px_ok, px_why = heating_price_rising(record, cfg, now=now)
    if not px_ok:
        return False, px_why
    return True, "heating"


def _square_exh_allows_buy(
    record: dict,
    cfg: dict,
    *,
    require_rising: bool,
    now: float | None = None,
) -> tuple[bool, str]:
    """Enter only on TV red-square: both %R OB and tight.

    No ``last_heating`` / fast-only heat. Falling still refuses. Already in
    the square (dual OB+tight) may arm even if flat (pinned at highs).
    """
    ind = record.get("indicator") if isinstance(record.get("indicator"), dict) else {}
    # Live dual only — sticky OR-latch armed false ■ after TV left (PSKY).
    both_ob, tight, err = dual_r_ob_tight(record, cfg, sticky=False)
    if err:
        if require_rising and (
            ind.get("pctr_falling") or exhaustion_state(record, cfg) == "cooling"
        ):
            return False, "exh_falling"
        if bool(cfg.get("ai_watch_require_exhaustion_data", True)):
            return False, err
        return False, "exh_not_tight"
    if ind.get("pctr_falling") or exhaustion_state(record, cfg) == "cooling":
        if require_rising or both_ob:
            return False, "exh_falling"
        return False, "exh_falling"
    if not both_ob:
        # Not in the square. Wide gap → exh_not_tight (RKLB); else wait_exh.
        if tight is False:
            fast = _f_or_none(ind.get("pctr"))
            slow = _f_or_none(ind.get("pctr_slow"))
            if fast is not None and slow is not None:
                gap = abs(float(fast) - float(slow))
                record["block_detail"] = (
                    f"exh gap {gap:.1f}>{_rte_confluence_max(cfg):g}")
            return False, "exh_not_tight"
        return False, "wait_exh"
    if not tight:
        fast = _f_or_none(ind.get("pctr"))
        slow = _f_or_none(ind.get("pctr_slow"))
        if fast is not None and slow is not None:
            gap = abs(float(fast) - float(slow))
            record["block_detail"] = (
                f"exh gap {gap:.1f}>{_rte_confluence_max(cfg):g}")
        return False, "exh_not_tight"
    # Staleness gate: refuse entries if the square has already been running too long.
    # Entering late into a square buys the climax rather than the breakout.
    max_sq_age = _f_or_none(cfg.get("ai_watch_square_max_age_sec", 60.0))
    if max_sq_age is not None and max_sq_age > 0:
        sq_since = _f_or_none(record.get("square_since"))
        if sq_since is not None:
            now_ts = float(now if now is not None else time.time())
            sq_age = max(0.0, now_ts - sq_since)
            if sq_age >= max_sq_age:
                record["block_detail"] = f"square age {sq_age:.0f}s >= {max_sq_age:.0f}s"
                return False, "stale_square"
    # In the square. Rising preferred; flat while both OB is still a square.
    if require_rising and not ind.get("pctr_rising") and not both_ob:
        return False, "exh_not_rising"
    if bool(cfg.get("ai_watch_ob_allow_hot", True)) and _hot_ob_source(record):
        return True, "overbought_hot"
    return True, "overbought"


def _oversold_triangle_allows_buy(
    record: dict,
    cfg: dict,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """Enter on TV oversold triangle: oversold squares confirmed, then leave-OS triangle.

    Inverse of the overbought square/triangle thesis:
      - Oversold squares: both %R in oversold band (<= -100 + rte_threshold) and tight.
      - Oversold triangle: leaves oversold band (fast > -100 + rte_threshold) while rising.
      - RSI directional indicator: cm_rsi_rising is True.
    """
    ind = record.get("indicator") if isinstance(record.get("indicator"), dict) else {}
    both_os, tight, err = dual_r_os_tight(record, cfg)
    if err:
        return False, err

    t_now = float(now if now is not None else time.time())
    thr = _rte_threshold(cfg)
    os_level = -100.0 + thr

    # In oversold squares: record latch and wait for triangle (do not enter in squares)
    if both_os and tight:
        if isinstance(record, dict):
            record["exh_was_oversold"] = True
            if _f_or_none(record.get("os_square_since")) is None:
                record["os_square_since"] = t_now
            record["left_os_since"] = None
        return False, "oversold_squares"

    # Must have had oversold squares (thesis requirement)
    was_os = bool(record.get("exh_was_oversold")) if isinstance(record, dict) else False
    if not was_os and bool(ind.get("pctr_os")):
        was_os = True
        if isinstance(record, dict):
            record["exh_was_oversold"] = True
    if not was_os:
        return False, "never_oversold"

    fast = _f_or_none(ind.get("pctr"))
    if fast is None:
        return False, "no_exhaustion_data"

    # Fast %R must have left oversold band (> os_level)
    if float(fast) <= os_level:
        return False, "still_oversold"

    # Must not be already extended into overbought
    if float(fast) >= -thr:
        return False, "already_extended"

    # Confluence check: fast and slow should remain reasonably tight
    if tight is False and bool(cfg.get("rte_require_tight", True)):
        fast = _f_or_none(ind.get("pctr"))
        slow = _f_or_none(ind.get("pctr_slow"))
        if fast is not None and slow is not None and isinstance(record, dict):
            gap = abs(float(fast) - float(slow))
            record["block_detail"] = f"exh gap {gap:.1f}>{_rte_confluence_max(cfg):g}"
        return False, "exh_not_tight"

    # Fast line must be rising out of oversold, not falling
    falling = ind.get("pctr_falling")
    if falling is None and isinstance(record, dict):
        falling = record.get("pctr_falling")
    if falling or exhaustion_state(record, cfg) == "cooling":
        return False, "exh_falling"

    pctr_rising = ind.get("pctr_rising")
    if pctr_rising is None and isinstance(record, dict):
        pctr_rising = record.get("pctr_rising")
    if not bool(pctr_rising):
        return False, "exh_not_rising"

    # RSI directional indicator: cm_rsi_rising must be True
    rsi_rising = ind.get("cm_rsi_rising")
    if rsi_rising is None and isinstance(record, dict):
        rsi_rising = record.get("cm_rsi_rising")
    if not bool(rsi_rising):
        return False, "rsi_not_rising"

    # Staleness gate: refuse entries if the triangle has been active too long
    max_age = _f_or_none(cfg.get("ai_watch_os_triangle_max_age_sec", 60.0))
    if max_age is not None and max_age > 0 and isinstance(record, dict):
        since = _f_or_none(record.get("left_os_since"))
        if since is None:
            record["left_os_since"] = t_now
        else:
            age = max(0.0, t_now - since)
            if age >= max_age:
                record["block_detail"] = f"os triangle age {age:.0f}s >= {max_age:.0f}s"
                return False, "stale_oversold_triangle"

    return True, "oversold_triangle"


def note_px_ring(rec: dict, px: float | None, now: float | None) -> None:
    """Keep a short (ts, price) ring so heating can see the tape slope."""
    if not isinstance(rec, dict):
        return
    try:
        p = float(px) if px is not None else 0.0
        t = float(now) if now is not None else 0.0
    except (TypeError, ValueError):
        return
    if p <= 0 or t <= 0:
        return
    ring = rec.get("px_ring")
    if not isinstance(ring, list):
        ring = []
    clean: list[list[float]] = []
    for item in ring:
        try:
            clean.append([float(item[0]), float(item[1])])
        except (TypeError, ValueError, IndexError):
            continue
    if clean and t - clean[-1][0] < 1.0:
        clean[-1] = [t, p]
    else:
        clean.append([t, p])
    rec["px_ring"] = clean[-12:]


def rising_heat_quality(record: dict | None, cfg: dict | None = None) -> bool:
    """True when both %R lines are rising, tight, and heat is in band.

    Book seating and RVOL relief use this so quality heaters can sit even
    when the broad min-RVOL floor would wipe them.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not isinstance(record, dict):
        return False
    ind = record.get("indicator") if isinstance(record.get("indicator"), dict) else {}
    src = ind or record
    if not (src.get("pctr_rising") and src.get("pctr_slow_rising")):
        if not src.get("pctr_both_rising"):
            return False
    try:
        heat_min = float(cfg.get("ai_watch_exhaustion_heat_min_pct", 40.0) or 40.0)
    except (TypeError, ValueError):
        heat_min = 40.0
    ex = exhaustion_pct(record)
    if ex is None:
        # Fall back to fast %R → heat.
        fast = _f_or_none(src.get("pctr"))
        if fast is None:
            return False
        ex = 100.0 + float(fast)
    if float(ex) + 1e-9 < heat_min:
        return False
    tight_ok, _why = _heating_dual_r_allows(record, cfg)
    return bool(tight_ok)


def heating_price_rising(
    record: dict,
    cfg: dict | None,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """Heating lane: recent prints must be up, not merely %R.

    ``ai_watch_heating_price_rise_sec`` 0 disables. Otherwise the newest
    print in the window must be above the oldest, and the window must
    actually span the lookback. Missing tape refuses (do not buy blind).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        need = float(cfg.get("ai_watch_heating_price_rise_sec", 0) or 0)
    except (TypeError, ValueError):
        need = 0.0
    if need <= 0:
        return True, ""
    ring = record.get("px_ring") if isinstance(record, dict) else None
    if not isinstance(ring, list) or len(ring) < 2:
        return False, "price_trend_unknown"
    try:
        t_now = float(now) if now is not None else float(ring[-1][0])
    except (TypeError, ValueError, IndexError):
        return False, "price_trend_unknown"
    window: list[tuple[float, float]] = []
    for item in ring:
        try:
            ts, px = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if t_now - ts <= need + 1.0 and px > 0:
            window.append((ts, px))
    if len(window) < 2:
        return False, "price_trend_unknown"
    span = window[-1][0] - window[0][0]
    if span + 1e-9 < min(8.0, need):
        return False, "price_trend_unknown"
    if window[-1][1] + 1e-9 <= window[0][1]:
        return False, "price_falling"
    return True, "price_rising"


def _heating_dual_r_allows(record: dict, cfg: dict) -> tuple[bool, str]:
    """Dual-%R gate for the heating (non-OB) arm path.

    Locked 2026-09-18 from TV: SMCI (both OB, gap ~9) = buy; RKLB (not OB,
    gap ~25) = don't. Does not enable ``ai_watch_tv_exh_rsi`` (that path
    also demands CM RSI ≤ buy_max and fights scalp_legacy RSI rising under 75).

    When ``rte_require_tight`` is on (default): require ``pctr`` +
    ``pctr_slow`` and ``abs(fast−slow) ≤ rte_confluence_max``. Missing slow
    → ``no_exhaustion_data``; wide gap → ``exh_not_tight``.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not bool(cfg.get("rte_require_tight", True)):
        return True, "tight_off"
    ind = record.get("indicator") if isinstance(record.get("indicator"), dict) else {}
    fast = _f_or_none(ind.get("pctr"))
    slow = _f_or_none(ind.get("pctr_slow"))
    if fast is None or slow is None:
        if bool(cfg.get("ai_watch_require_exhaustion_data", True)):
            return False, "no_exhaustion_data"
        # Heat path with unknown slow: refuse rather than arm blind on fast.
        return False, "exh_not_tight"
    try:
        tight_max = float(cfg.get("rte_confluence_max", 15) or 15)
    except (TypeError, ValueError):
        tight_max = 15.0
    gap = abs(float(fast) - float(slow))
    tight = bool(ind.get("pctr_tight")) or gap <= tight_max + 1e-9
    if not tight:
        if isinstance(record, dict):
            record["block_detail"] = f"exh gap {gap:.1f}>{tight_max:g}"
        return False, "exh_not_tight"
    # Optional both-OB confirmation when already in the red-box band: if both
    # lines are OB and tight, heating is fine (same confluence picture).
    # Not required — heat_min + rising + tight is enough for early confluence.
    return True, "exh_tight"


def _macd_is_armed(record: dict) -> bool:
    """Bullish AND opening — the direction %R cannot express at 100%.

    No min-gap, no separation test. This answers one question — are the
    lines apart and still separating — because it is standing in for a %R
    turn, not re-deciding the entry.

    Provenance is required for the same reason it is everywhere else: an
    opening gap drawn on the REST fallback is an opening gap in older bars,
    and absence is not a pass.
    """
    ind = record.get("indicator") if isinstance(record, dict) else None
    ind = ind if isinstance(ind, dict) else {}
    live, _why = macd_reading_is_live(ind, _push_cfg())
    if not live:
        return False
    gap = _f_or_none(
        ind.get("macd_gap") if ind.get("macd_gap") is not None
        else ind.get("macd_hist"))
    if gap is None or gap <= 0:
        return False
    if ind.get("macd_bull") is False:
        return False
    # Opening, not merely positive. A wide gap that is closing is a move
    # already over, which is exactly what a pinned %R must not be paired with.
    if ind.get("macd_gap_falling"):
        return False
    return bool(ind.get("macd_gap_rising"))


def _left_ob_confirm_sec(cfg: dict) -> float:
    """Seconds dual leave-OB must persist before flatten (APLD flicker guard).

    Default 3s. Capped well under 30s so MARA-class multi-minute lag cannot
    return via an oversized confirm.
    """
    try:
        sec = float(cfg.get("ai_exit_left_overbought_confirm_sec", 3.0) or 0.0)
    except (TypeError, ValueError):
        sec = 3.0
    return max(0.0, min(15.0, sec))


def _dual_slow_max_age_sec(cfg: dict) -> float:
    """Max age of a usable slow %R for dual hold. Past this + fast left → exit."""
    try:
        sec = float(cfg.get("ai_exit_dual_slow_max_age_sec", 45.0) or 0.0)
    except (TypeError, ValueError):
        sec = 45.0
    return max(0.0, sec)


def merge_triangle_indicator(
    engine: dict | None,
    live: dict | None,
    thr: float,
) -> dict:
    """Indicator the triangle exit should judge.

    The chart ▼ is the engine fast line. A live recompute that is still
    inside the square, or a blank read, must not hide a leave the engine
    has already printed. CLSK 2026-09-22: the book showed fast −26 while
    the position stayed ``hold``.
    """
    eng = engine if isinstance(engine, dict) else {}
    out = dict(live) if isinstance(live, dict) else {}
    if not out:
        out = dict(eng)

    def _f(src: dict, key: str):
        try:
            v = src.get(key)
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    eng_fast = _f(eng, "pctr")
    live_fast = _f(out, "pctr")
    band = -float(thr)
    engine_left = eng_fast is not None and eng_fast < band
    live_left = live_fast is not None and live_fast < band
    if engine_left and not live_left:
        out["pctr"] = eng_fast
        if out.get("pctr_slow") is None and eng.get("pctr_slow") is not None:
            out["pctr_slow"] = eng.get("pctr_slow")
    elif out.get("pctr") is None and eng_fast is not None:
        out["pctr"] = eng_fast
        if out.get("pctr_slow") is None and eng.get("pctr_slow") is not None:
            out["pctr_slow"] = eng.get("pctr_slow")
    return out


def exhaustion_exit_now(
    record: dict,
    cfg: dict,
    now: float | None = None,
) -> tuple[bool, str]:
    """Sell when %R leaves the overbought band (triangle ▼ / left_overbought).

    Square mode / dual-%R: latch and exit on **both-line** OB edge
    (``ob_reversal``: was dual-OB, now not) — not fast-only heat drop.

    Leave must persist ``ai_exit_left_overbought_confirm_sec`` (default 3s)
    before flatten; dual OB returning inside the window cancels the pending
    exit (APLD flicker). If slow is missing/stale past
    ``ai_exit_dual_slow_max_age_sec`` while fast has left OB and we already
    latched dual OB, treat as leave (triangle-first, not trail-first).

    Legacy (square off): fast-line band only.

    Disabled when ``left_overbought_exit_enabled`` is false.

    Returns (exit_now, reason). Mutates ``record`` for latch / confirm state
    (``exh_was_overbought``, ``left_ob_since``) — caller should persist those
    onto the open position.
    """
    if not left_overbought_exit_enabled(cfg):
        return False, "left_overbought_off"
    if not bool(cfg.get("ai_watch_exhaustion_rules", True)):
        return False, "exhaustion_off"

    t = float(now if now is not None else time.time())
    use_dual = bool(
        exh_square_arm_enabled(cfg) or tv_exh_rsi_enabled(cfg)
    )
    if use_dual:
        ind = (
            record.get("indicator")
            if isinstance(record.get("indicator"), dict) else {}
        )
        fast = _f_or_none(ind.get("pctr"))
        slow = _f_or_none(ind.get("pctr_slow"))
        thr = _rte_threshold(cfg)
        both_ob, _tight, err = dual_r_ob_tight(record, cfg)

        # Stale/missing slow after a dual-OB latch: if fast has left OB,
        # prefer triangle exit over waiting on trail (MARA-class).
        if err == "no_exhaustion_data" or both_ob is None:
            slow_ts = record.get("pctr_slow_live_ts")
            if slow_ts is None:
                slow_ts = ind.get("pctr_ts")
            max_age = _dual_slow_max_age_sec(cfg)
            try:
                age = (
                    t - float(slow_ts)
                    if slow_ts is not None else float("inf")
                )
            except (TypeError, ValueError):
                age = float("inf")
            slow_stale = slow is None or (max_age > 0 and age > max_age)
            fast_left = fast is not None and float(fast) < -thr
            if (
                record.get("exh_was_overbought")
                and slow_stale
                and fast_left
            ):
                both_ob = False
            else:
                record["left_ob_since"] = None
                return False, "no_exhaustion_data"

        if both_ob:
            record["exh_was_overbought"] = True
            record["left_ob_since"] = None  # squares back on → cancel pending
            return False, "overbought_hold"
        if not record.get("exh_was_overbought"):
            record["left_ob_since"] = None
            return False, "never_overbought"

        confirm = _left_ob_confirm_sec(cfg)
        if confirm <= 0:
            record["left_ob_since"] = None
            return True, "left_overbought"
        since = record.get("left_ob_since")
        if not isinstance(since, (int, float)) or float(since) <= 0:
            record["left_ob_since"] = t
            return False, "left_overbought_pending"
        if (t - float(since)) < confirm:
            return False, "left_overbought_pending"
        return True, "left_overbought"

    record["left_ob_since"] = None
    ex = exhaustion_pct(record)
    if ex is None:
        return False, "no_exhaustion_data"
    thr = _rte_threshold(cfg)
    band = 100.0 - thr
    if ex >= band:
        record["exh_was_overbought"] = True
        return False, "overbought_hold"
    if not record.get("exh_was_overbought"):
        return False, "never_overbought"
    # Small give-back so a single print one tick under the band does not exit
    # a position that is still pinned at the highs.
    try:
        give = float(cfg.get("ai_watch_exhaustion_exit_give_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        give = 0.0
    if ex >= band - give:
        return False, "overbought_hold"
    return True, "left_overbought"


def exhaustion_says_exit(record: dict, cfg: dict, now: float | None = None) -> bool:
    """True once the fast line has been falling continuously for N seconds.

    Measured in TIME, not in polls. Counting polls was wrong in a way that
    silently inverted the setting: the position loop runs every 5s against an
    engine reading that refreshes every 60s, so "2 consecutive falling reads"
    resolved to 10 seconds off a single computation — the same stale number
    counted twice — and fired roughly 12x sooner than intended. Seconds are
    poll-rate independent, so retuning ai_positions_poll_sec cannot quietly
    change how long a fade must persist before it sells.

    The clock resets on any non-falling reading, so the window means "falling
    for this long without interruption".
    """
    if not bool(cfg.get("ai_watch_exhaustion_rules", True)):
        return False
    t = float(now if now is not None else time.time())
    ind = record.get("indicator") if isinstance(record, dict) else None
    if not isinstance(ind, dict) or ind.get("pctr") is None:
        # No reading: do not manufacture an exit. Stop and target still apply.
        record["pctr_fall_since"] = None
        return False
    try:
        need_sec = float(cfg.get("ai_watch_exhaustion_exit_sec", 120.0) or 0.0)
    except (TypeError, ValueError):
        need_sec = 120.0
    need_sec = max(0.0, need_sec)
    if not ind.get("pctr_falling"):
        record["pctr_fall_since"] = None
        return False
    since = record.get("pctr_fall_since")
    if not isinstance(since, (int, float)) or since <= 0:
        record["pctr_fall_since"] = t
        return need_sec <= 0.0
    return (t - float(since)) >= need_sec


def ask_in_zone(
    ask: float,
    entry_low: float,
    entry_high: float,
    pad_pct: float,
) -> bool:
    """True if *ask* is inside ``[entry_low, entry_high]`` expanded by *pad_pct*.

    *pad_pct* is a percent (e.g. ``0.15`` = 0.15%): low is reduced and high
    is raised by that fraction of each bound.
    """
    try:
        a = float(ask)
        lo = float(entry_low)
        hi = float(entry_high)
        pad = max(0.0, float(pad_pct or 0.0))
    except (TypeError, ValueError):
        return False
    if a <= 0 or lo <= 0 or hi <= 0:
        return False
    if hi < lo:
        lo, hi = hi, lo
    frac = pad / 100.0
    low_bound = lo * (1.0 - frac)
    high_bound = hi * (1.0 + frac)
    return low_bound <= a <= high_bound


def armable_below_floor(
    entry_low: float,
    entry_high: float,
    stop: float | None,
    *,
    pad_pct: float = 0.0,
    max_r: float = 0.5,
) -> float | None:
    """Lowest ask that is still an armable pullback overshoot (not a breakdown).

    R is ``zone_floor − stop``. The floor is ``zone_floor − max_r · R``.
    Returns None when there is no valid dip window (missing/invalid stop,
    stop at or above the zone, or max_r <= 0) — caller should treat any
    print under the band as a hard below_zone.
    """
    try:
        lo = float(entry_low)
        hi = float(entry_high)
        pad = max(0.0, float(pad_pct or 0.0))
        mr = float(max_r or 0.0)
    except (TypeError, ValueError):
        return None
    if lo <= 0 or hi <= 0 or mr <= 0:
        return None
    if hi < lo:
        lo, hi = hi, lo
    try:
        sp = float(stop) if stop is not None else 0.0
    except (TypeError, ValueError):
        return None
    if sp <= 0:
        return None
    frac = pad / 100.0
    low_bound = lo * (1.0 - frac)
    r_unit = low_bound - sp
    if r_unit <= 0:
        return None
    return low_bound - mr * r_unit


def ask_triggers_zone(
    ask: float,
    entry_low: float,
    entry_high: float,
    *,
    pad_pct: float = 0.0,
    stop: float | None = None,
    max_below_r: float = DEFAULT_ARM_BELOW_MAX_R,
    arm_below: bool = True,
) -> bool:
    """True when *ask* is inside the band or anywhere below it.

    Above the band is never a trigger. The planned stop is not a veto —
    it only binds after the fill. A dip through the old stop (IPWR $5.10
    vs stop $5.22) is still a below-zone buy.
    """
    if ask_in_zone(ask, entry_low, entry_high, pad_pct):
        return True
    if not arm_below:
        return False
    try:
        a = float(ask)
        lo = float(entry_low)
        hi = float(entry_high)
        pad = max(0.0, float(pad_pct or 0.0))
    except (TypeError, ValueError):
        return False
    if a <= 0 or lo <= 0 or hi <= 0:
        return False
    if hi < lo:
        lo, hi = hi, lo
    high_bound = hi * (1.0 + pad / 100.0)
    return a <= high_bound


def spread_ok(
    bid: float | None,
    ask: float,
    max_spread_pct: float,
) -> bool:
    """True if bid/ask spread as % of mid is within *max_spread_pct*.

    When *max_spread_pct* <= 0, spread is not enforced (always OK).
    Missing/invalid bid → OK (IEX often omits one side; do not block zone fills).
    """
    try:
        a = float(ask)
        msp = float(max_spread_pct or 0.0)
    except (TypeError, ValueError):
        return False
    if a <= 0:
        return False
    if msp <= 0:
        return True
    if bid is None:
        return True
    try:
        b = float(bid)
    except (TypeError, ValueError):
        return True
    if b <= 0 or a < b:
        return True
    mid = (a + b) / 2.0
    if mid <= 0:
        return False
    spr = 100.0 * (a - b) / mid
    return spr <= msp + 1e-12


def _stop_of(rec: dict) -> float | None:
    """The stop this watch record would enter with, or None.

    Needed by the R-denominated spread gate: a spread only means something
    against the distance to the stop.
    """
    if not isinstance(rec, dict):
        return None
    structure = rec.get("structure")
    if not isinstance(structure, dict):
        return None
    try:
        stop = float(structure.get("stop_price") or 0)
    except (TypeError, ValueError):
        return None
    return stop if stop > 0 else None


def _structure_levels(structure: dict) -> tuple[float, float, float, float, float] | None:
    """Parse entry/stop/target/rr from structure; None if incomplete for zone arm."""
    try:
        entry_low = float(structure.get("entry_low") or 0)
        entry_high = float(structure.get("entry_high") or 0)
        stop = float(structure.get("stop_price") or 0)
        target = float(structure.get("target_1") or 0)
        rr = float(structure.get("reward_risk") or 0)
    except (TypeError, ValueError):
        return None
    if entry_low <= 0 or entry_high <= 0 or stop <= 0 or target <= 0:
        return None
    return entry_low, entry_high, stop, target, rr


# ── Double-bottom structure zones ───────────────────────────────────────────
# Two candle lows at the same support shelf. Buy band: from S (tiny pad under)
# up ~1–1.5%. Stop under the lower low. Bars are throttled; failure falls back
# to the legacy % offset zone.

_bar_cache: dict[str, tuple[float, Any]] = {}  # symbol -> (ts, lows list or df)
_bar_cache_lock = threading.Lock()
# symbol -> (ts, [(high, low, close), ...]) filled by the same fetch as above.
_ohlc_cache: dict[str, tuple[float, list[tuple[float, float, float]]]] = {}
_ohlc_cache_lock = threading.Lock()
# symbol -> (ts, [bar epoch seconds, ...]) parallel to _ohlc_cache.
#
# Bar COUNT is not bar COVERAGE. On the free IEX feed a thin name still returns
# 23 rows, but they are the 23 minutes it happened to print across five days —
# so %R computes cleanly over a "21-minute" window that actually spans a week.
# Measured 2026-08-11 across the 96-name book: 9% of the names that pass the
# 23-bar gate have a window spanning more than a day (NEGG's spanned 7,120
# minutes). Those readings are not missing, they are wrong, which is worse.
# Timestamps are the only way to tell the two apart.
_ohlc_ts_cache: dict[str, tuple[float, list[float]]] = {}


def find_double_bottom_support(
    lows: list[float],
    *,
    swing: int = 2,
    match_pct: float = 0.40,
    min_sep_bars: int = 3,
) -> dict[str, Any] | None:
    """Find two matching swing lows that define the same support shelf.

    A swing low is a bar whose low is <= lows within ``swing`` bars on each
    side. Two swings "match" when |L1−L2| / mid <= match_pct/100 and they are
    at least ``min_sep_bars`` apart. Support S = min of the two lows (floor of
    the shelf). Returns None when no pair qualifies.
    """
    if not lows or len(lows) < max(5, 2 * swing + min_sep_bars + 1):
        return None
    try:
        xs = [float(x) for x in lows]
    except (TypeError, ValueError):
        return None
    n = len(xs)
    swing = max(1, int(swing))
    min_sep = max(1, int(min_sep_bars))
    match = max(0.0, float(match_pct)) / 100.0

    pivots: list[tuple[int, float]] = []
    for i in range(swing, n - swing):
        lo = xs[i]
        if lo <= 0:
            continue
        window = xs[i - swing: i + swing + 1]
        if lo <= min(window) + 1e-12:
            # strict-ish local min: at least as low as neighbors
            pivots.append((i, lo))
    if len(pivots) < 2:
        return None

    # Prefer the most recent pair that matches (walk newest-first).
    for j in range(len(pivots) - 1, 0, -1):
        i2, l2 = pivots[j]
        for k in range(j - 1, -1, -1):
            i1, l1 = pivots[k]
            if i2 - i1 < min_sep:
                continue
            mid = (l1 + l2) / 2.0
            if mid <= 0:
                continue
            if abs(l1 - l2) / mid > match:
                continue
            s = min(l1, l2)
            return {
                "support": s,
                "low_a": l1,
                "low_b": l2,
                "index_a": i1,
                "index_b": i2,
                "match_pct": round(100.0 * abs(l1 - l2) / mid, 3),
            }
    return None


def build_double_bottom_zone_structure(
    support: float,
    cfg: dict | None = None,
    *,
    reason: str = "",
    low_a: float | None = None,
    low_b: float | None = None,
    last_price: float | None = None,
) -> dict[str, Any] | None:
    """Zone from double-bottom support S: tiny pad under → ~1.25% above.

    entry_low  ≈ S × (1 − below_pct)
    entry_high ≈ S × (1 + above_pct)   # preferred entry near top
    stop       ≈ min(lows) × (1 − stop_below_pct)
    target     from day-scalp R multiple off mid-zone risk
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        s = float(support)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None

    def _pct(key: str, default: float) -> float:
        return max(0.0, _opt_float(cfg.get(key, default), default))

    above = _pct("ai_watch_db_above_pct", 1.25) / 100.0
    below = _pct("ai_watch_db_below_pct", 0.25) / 100.0
    stop_below = _pct("ai_watch_db_stop_below_pct", 0.50) / 100.0
    rr = max(0.25, _opt_float(cfg.get("ai_watch_synth_rr"), 0.6))
    scale_out = max(1.0, min(99.0, _opt_float(
        cfg.get("ai_watch_synth_scale_out_pct"), 50.0)))
    trail_pct = max(0.0, _opt_float(cfg.get("ai_watch_synth_trail_pct"), 2.5))

    floor = s
    for x in (low_a, low_b):
        try:
            if x is not None and float(x) > 0:
                floor = min(floor, float(x))
        except (TypeError, ValueError):
            pass

    entry_low = s * (1.0 - below)
    entry_high = s * (1.0 + above)
    if entry_low <= 0 or entry_high <= entry_low:
        return None
    stop = floor * (1.0 - stop_below)
    if stop <= 0 or stop >= entry_low:
        stop = entry_low * 0.995
    mid = (entry_low + entry_high) / 2.0
    risk = mid - stop
    if risk <= 0:
        return None
    target = mid + rr * risk

    if bool(cfg.get("ai_watch_db_require_price_above", True)):
        try:
            px = float(last_price) if last_price is not None else 0.0
        except (TypeError, ValueError):
            px = 0.0
        if px > 0 and px < s:
            # Price already through the shelf — do not publish a long zone.
            return None

    def _r(x: float) -> float:
        if x >= 100:
            return round(x, 2)
        if x >= 1:
            return round(x, 3)
        return round(x, 4)

    return {
        "decision": "WAIT",
        "wait_kind": "wait_for_zone",
        "entry_low": _r(entry_low),
        "entry_high": _r(entry_high),
        "stop_price": _r(stop),
        "target_1": _r(target),
        "reward_risk": round(rr, 2),
        "scale_out_pct": scale_out,
        "trail_pct": trail_pct,
        "trail_method": "pct",
        "synthetic": True,
        "zone_kind": "double_bottom",
        "strategy": "day_scalp_v0",
        "support": _r(s),
        "db_low_a": _r(float(low_a)) if low_a else None,
        "db_low_b": _r(float(low_b)) if low_b else None,
        "anchor_price": _r(float(last_price)) if last_price else _r(s),
        "summary": (
            f"double-bottom zone: support {_r(s)} "
            f"band {_r(entry_low)}-{_r(entry_high)} (+{above * 100:.2f}%/-{below * 100:.2f}%)"
            + (f" · {reason}" if reason else "")
        ),
    }


def _extract_ohlc_from_bars(bars: Any, lookback: int) -> list[tuple[float, float, float]]:
    """(high, low, close) rows from a DataFrame or sequence. Empty on failure.

    Same request the double-bottom scan already pays for — the zone sizing
    below needs highs and closes as well as lows, and re-fetching for that
    would put a second bar call per symbol on the rate limit this desk is
    already fighting.
    """
    if bars is None:
        return []
    try:
        import pandas as pd
        if isinstance(bars, pd.DataFrame):
            cols = {"high", "low", "close"}
            if not cols.issubset(set(bars.columns)):
                return []
            tail = bars[["high", "low", "close"]].tail(int(lookback))
            out: list[tuple[float, float, float]] = []
            for h, lo, c in tail.itertuples(index=False, name=None):
                try:
                    hf, lf, cf = float(h), float(lo), float(c)
                except (TypeError, ValueError):
                    continue
                if hf > 0 and lf > 0 and hf >= lf:
                    out.append((hf, lf, cf))
            return out
    except Exception:
        pass
    if isinstance(bars, (list, tuple)):
        out = []
        for row in list(bars)[-int(lookback):]:
            h = getattr(row, "high", None)
            lo = getattr(row, "low", None)
            c = getattr(row, "close", None)
            if h is None and isinstance(row, dict):
                h, lo, c = row.get("high"), row.get("low"), row.get("close")
            try:
                hf, lf, cf = float(h), float(lo), float(c)
            except (TypeError, ValueError):
                continue
            if hf > 0 and lf > 0 and hf >= lf:
                out.append((hf, lf, cf))
        return out
    return []


def _extract_ohlc_ts_from_bars(bars: Any, lookback: int) -> list[float]:
    """Bar timestamps as epoch seconds, aligned 1:1 with _extract_ohlc_from_bars.

    Kept in step with that function's row filter on purpose — a timestamp list
    that drifts out of alignment would mis-date the window it is meant to
    police, which is a worse failure than not checking at all. Returns [] when
    alignment cannot be guaranteed, and the span check then skips.
    """
    if bars is None:
        return []
    try:
        import pandas as pd
        if not isinstance(bars, pd.DataFrame):
            return []
        cols = {"high", "low", "close"}
        if not cols.issubset(set(bars.columns)):
            return []
        tail = bars[["high", "low", "close"]].tail(int(lookback))
        out: list[float] = []
        for idx, (h, lo, c) in zip(tail.index, tail.itertuples(index=False, name=None)):
            try:
                hf, lf, _cf = float(h), float(lo), float(c)
            except (TypeError, ValueError):
                continue
            if not (hf > 0 and lf > 0 and hf >= lf):
                continue
            stamp = idx[-1] if isinstance(idx, tuple) else idx
            try:
                out.append(float(pd.Timestamp(stamp).timestamp()))
            except Exception:
                return []
        return out
    except Exception:
        return []


def window_span_sec(symbol: str, length: int, cfg: dict, now: float) -> float | None:
    """Wall-clock seconds covered by the newest *length* cached bars.

    None when unknown. For 1-minute bars over a 21-bar window the honest
    answer is ~21 minutes; anything far above that means the feed skipped
    minutes and the "window" is stitched from whenever the name last printed.
    """
    sym = str(symbol or "").upper().strip()
    if not sym or length < 2:
        return None
    try:
        refresh = float(cfg.get("ai_watch_db_bar_refresh_sec", 120.0) or 120.0)
    except (TypeError, ValueError):
        refresh = 120.0
    max_age = max(60.0, refresh * 3.0)
    with _ohlc_cache_lock:
        hit = _ohlc_ts_cache.get(sym)
        if not hit or (now - hit[0]) >= max_age:
            return None
        stamps = list(hit[1])
    if len(stamps) < length:
        return None
    win = stamps[-length:]
    return float(win[-1] - win[0])


def pullback_depths(
    rows: list[tuple[float, float, float]],
    window: int = 15,
) -> list[float]:
    """Deepest drawdown below a running high, over each rolling window (%).

    This is the statistic the entry zone is built on, and it is a measurement
    rather than a parameter: "how far below a recent high does THIS name
    typically trade within `window` bars".

    The obvious alternative — recording each *completed* pullback, i.e. a dip
    that resolved into a new high — was tried first and is unusable on exactly
    the names this desk trades. A strong trender prints one running high and
    then fades, so it yields a single sample; measured on 2026-08-10, six of
    thirteen book names produced fewer than three completed pullbacks over 90
    minutes of 1-minute bars, and their one sample was the whole afternoon's
    decline. A rolling window always has ~N samples regardless of trend shape,
    and on that same day its median tracked the next 30 minutes' actual dip
    closely (FSLY 1.28 vs 1.24, SMCI 1.12 vs 1.40, ACHR 2.02 vs 3.47).

    Expressed in percent rather than as an ATR multiple: the same multiplier
    means a different depth on 1Min than on 5Min bars, which is how a "2% zone"
    silently becomes a 10% zone when a bar timeframe is retuned.
    """
    w = max(2, int(window or 15))
    if len(rows) < w:
        return []
    depths: list[float] = []
    for i in range(len(rows) - w + 1):
        run = 0.0
        worst = 0.0
        for high, low, _close in rows[i:i + w]:
            if high > run:
                run = high
            if run > 0 and low < run:
                worst = max(worst, 100.0 * (run - low) / run)
        if worst > 0:
            depths.append(worst)
    return depths


def _percentile(vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile. 0.0 on an empty list."""
    xs = sorted(v for v in vals if v is not None)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return float(xs[0])
    k = max(0.0, min(1.0, pct / 100.0)) * (len(xs) - 1)
    lo_i = int(k)
    hi_i = min(lo_i + 1, len(xs) - 1)
    frac = k - lo_i
    return float(xs[lo_i] * (1.0 - frac) + xs[hi_i] * frac)


def _session_decay(cfg: dict, now: float) -> float:
    """Shrink factor for zone depth as the session runs out (1.0 → floor).

    A 4% pullback is an ordinary morning event and a fantasy at 15:30: there
    are not enough minutes left to make one. Without this the book spends the
    afternoon waiting on depths the remaining session cannot produce, which is
    the same "never hits it" failure as an over-deep zone, just arriving later
    in the day.
    """
    if not bool(cfg.get("ai_watch_zone_time_decay", True)):
        return 1.0
    try:
        floor = float(cfg.get("ai_watch_zone_decay_floor", 0.5) or 0.5)
    except (TypeError, ValueError):
        floor = 0.5
    floor = max(0.1, min(1.0, floor))
    try:
        sh, sm = _parse_hhmm(str(cfg.get("ai_watch_start_time", "04:00")), (4, 0))
        eh, em = _parse_hhmm(str(cfg.get("ai_eod_liquidate_time", "15:50")), (15, 50))
        start, end = sh * 60 + sm, eh * 60 + em
        et = _et_now(now)
    except Exception:
        return 1.0
    if end <= start:
        return 1.0
    mins = et.hour * 60 + et.minute
    if mins <= start:
        return 1.0
    if mins >= end:
        return floor
    frac = (mins - start) / float(end - start)
    return 1.0 - (1.0 - floor) * frac


def variable_zone_band(
    price: float,
    rows: list[tuple[float, float, float]],
    cfg: dict,
    now: float,
) -> tuple[float, float, dict] | None:
    """Entry band scaled to how deep this name actually pulls back.

    Returns (entry_low, entry_high, meta) or None when there is not enough
    history to measure. The band is bounded on both sides for different
    reasons: the TOP must sit far enough below the print that this is a
    pullback and not a market order at the ask, and the BOTTOM must stay
    within a depth the name reaches often enough to be worth waiting for.

    Calibration on 2026-08-10's book (13 names): median 5-minute ATR was 0.65%
    of price and the median deepest intraday retrace was 5.9% — every single
    name pulled back at least 3x its own ATR. The zones in force that day sat
    20-30% below price because they were pinned to 90-bar double-bottom
    structure, so not one of eleven rows ever came within reach and the book
    took zero entries. Depth has to come from the name's own behaviour.
    """
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None
    if px <= 0 or not rows:
        return None

    try:
        window = int(cfg.get("ai_watch_zone_dip_window_bars", 15) or 15)
    except (TypeError, ValueError):
        window = 15
    depths = pullback_depths(rows, window=window)
    try:
        min_obs = int(cfg.get("ai_watch_zone_min_samples", 10) or 10)
    except (TypeError, ValueError):
        min_obs = 10
    if len(depths) < max(2, min_obs):
        return None

    def _f(key: str, default: float) -> float:
        try:
            return float(cfg.get(key, default) if cfg.get(key) is not None else default)
        except (TypeError, ValueError):
            return default

    top_pctl = _f("ai_watch_zone_top_pctl", 25.0)
    bot_pctl = _f("ai_watch_zone_bottom_pctl", 65.0)
    top_raw = _percentile(depths, top_pctl)
    bot_raw = _percentile(depths, bot_pctl)

    decay = _session_decay(cfg, now)
    top = top_raw * decay
    bot = bot_raw * decay

    top = max(_f("ai_watch_zone_top_min_pct", 0.4),
              min(_f("ai_watch_zone_top_max_pct", 3.0), top))
    bot = max(_f("ai_watch_zone_bottom_min_pct", 1.2),
              min(_f("ai_watch_zone_bottom_max_pct", 9.0), bot))
    if bot <= top:
        # Percentiles collapsed (a name that only ever dips one depth). Keep a
        # usable band rather than an inverted or zero-width one.
        bot = top + max(0.3, _f("ai_watch_zone_min_width_pct", 0.6))

    entry_high = px * (1.0 - top / 100.0)
    entry_low = px * (1.0 - bot / 100.0)
    if entry_low <= 0 or entry_high <= entry_low:
        return None
    meta = {
        "zone_src": "pullback_band",
        "depth_top_pct": round(top, 3),
        "depth_bottom_pct": round(bot, 3),
        "depth_p25": round(top_raw, 3),
        "depth_p65": round(bot_raw, 3),
        "decay": round(decay, 3),
        "samples": len(depths),
    }
    return entry_low, entry_high, meta


def _extract_lows_from_bars(bars: Any, lookback: int) -> list[float]:
    """Pull recent low prices from a DataFrame or sequence."""
    if bars is None:
        return []
    try:
        import pandas as pd
        if isinstance(bars, pd.DataFrame):
            if "low" not in bars.columns:
                return []
            series = bars["low"].tail(int(lookback))
            out: list[float] = []
            for v in series.tolist():
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if fv > 0:
                    out.append(fv)
            return out
    except Exception:
        pass
    if isinstance(bars, (list, tuple)):
        out = []
        for v in list(bars)[-int(lookback):]:
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv > 0:
                out.append(fv)
        return out
    return []


def _fetch_symbol_lows(symbol: str, cfg: dict, now: float) -> list[float]:
    """Throttled bar lows for double-bottom detection. Empty on failure."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return []
    try:
        refresh = float(cfg.get("ai_watch_db_bar_refresh_sec", 120.0) or 120.0)
    except (TypeError, ValueError):
        refresh = 120.0
    refresh = max(30.0, refresh)
    try:
        lookback = int(cfg.get("ai_watch_db_lookback_bars", 90) or 90)
    except (TypeError, ValueError):
        lookback = 90
    lookback = max(20, min(300, lookback))

    with _bar_cache_lock:
        hit = _bar_cache.get(sym)
        if hit and (now - hit[0]) < refresh and hit[1]:
            return list(hit[1])

    lows: list[float] = []
    try:
        from config import load_config
        full = load_config() or {}
        full = {**full, **(cfg or {})}
        # Prefer short history for structure (not full bar_count 300 every time).
        bar_cfg = dict(full)
        bar_cfg["bar_timeframe"] = str(
            full.get("ai_watch_db_bar_timeframe") or "1Min")
        bar_cfg["bar_count"] = lookback
        api_key = full.get("api_key") or full.get("alpaca_key")
        secret = full.get("secret_key") or full.get("alpaca_secret")
        if not api_key or not secret:
            try:
                import json
                sec_path = ROOT / "config" / "secrets.json"
                if sec_path.exists():
                    sec = json.loads(sec_path.read_text(encoding="utf-8"))
                    api_key = api_key or sec.get("api_key") or sec.get("ALPACA_API_KEY")
                    secret = secret or sec.get("secret_key") or sec.get("ALPACA_SECRET_KEY")
            except Exception:
                pass
        if api_key and secret:
            import alpaca_api as aa
            client = aa.connect_data_client({
                "api_key": api_key, "secret_key": secret,
            })
            df = aa.fetch_bars(client, sym, bar_cfg)
            lows = _extract_lows_from_bars(df, lookback)
            ohlc = _extract_ohlc_from_bars(df, lookback)
            if ohlc:
                stamps = _extract_ohlc_ts_from_bars(df, lookback)
                with _ohlc_cache_lock:
                    _ohlc_cache[sym] = (now, ohlc)
                    # Only stored when it lines up row-for-row; a short list
                    # would silently shift the window it is checking.
                    if len(stamps) == len(ohlc):
                        _ohlc_ts_cache[sym] = (now, stamps)
                        _seed_stream_from_iex(sym, ohlc, stamps)
                    else:
                        _ohlc_ts_cache.pop(sym, None)
    except Exception:
        lows = []

    with _bar_cache_lock:
        if lows:
            _bar_cache[sym] = (now, lows)
        elif hit:
            return list(hit[1])
    return lows


def _stream_bars_live(cfg: dict) -> bool:
    return bool(cfg.get("ai_watch_stream_bars_live", True))


def _overlay_stream_ohlc(symbol: str, cfg: dict, now: float) -> None:
    """Merge sampled-tape minutes over IEX history in the OHLC cache.

    IEX is a few percent of the tape, so a 21-bar %R window on REST bars
    often spans an hour. The watch loop already sees the Finnhub print every
    ~2s; folding that into 1-minute bars and splicing it on top of the IEX
    seed is what makes EXH/RSI a 1-minute reading on names that actually
    trade.
    """
    if not _stream_bars_live(cfg):
        return
    sym = str(symbol or "").upper().strip()
    if not sym:
        return
    try:
        import stream_bars
    except Exception:
        return
    srows, sstamps = stream_bars.ohlc_with_stamps(sym)
    if len(srows) < 1 or len(srows) != len(sstamps):
        return
    t0 = float(sstamps[0])
    with _ohlc_cache_lock:
        hit = _ohlc_cache.get(sym)
        ts_hit = _ohlc_ts_cache.get(sym)
        iex_rows = list(hit[1]) if hit else []
        iex_ts = list(ts_hit[1]) if ts_hit and len(ts_hit[1]) == len(iex_rows) else []
        kept_rows: list = []
        kept_ts: list = []
        if iex_rows and iex_ts:
            for row, ts in zip(iex_rows, iex_ts):
                try:
                    if float(ts) < t0 - 1.0:
                        kept_rows.append(row)
                        kept_ts.append(float(ts))
                except (TypeError, ValueError):
                    continue
        merged_rows = kept_rows + list(srows)
        merged_ts = kept_ts + [float(t) for t in sstamps]
        if not merged_rows or len(merged_rows) != len(merged_ts):
            return
        _ohlc_cache[sym] = (now, merged_rows)
        _ohlc_ts_cache[sym] = (now, merged_ts)


def _seed_stream_from_iex(symbol: str, ohlc: list, stamps: list) -> None:
    try:
        import stream_bars
        stream_bars.seed(symbol, ohlc, stamps)
    except Exception:
        return


def symbol_ohlc(symbol: str, cfg: dict, now: float) -> list[tuple[float, float, float]]:
    """Cached (high, low, close) rows, populated by the double-bottom fetch.

    Deliberately does NOT fetch on its own. The structure scan already pulls
    these bars on its own throttle; adding a second trigger here would double
    the bar requests for every watched name. Returns [] until that scan has
    run for the symbol, and callers fall back to a fixed zone.

    For exhaustion (and any path that needs bars without rebuilding a zone)
    call ``ensure_symbol_ohlc`` instead — that warms the cache once when cold.

    When ``ai_watch_stream_bars_live`` is on, sampled Finnhub tape minutes
    overwrite the recent IEX window so %R/RSI see a 1-minute clock.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return []
    try:
        refresh = float(cfg.get("ai_watch_db_bar_refresh_sec", 120.0) or 120.0)
    except (TypeError, ValueError):
        refresh = 120.0
    # Bars age out slower than the structure refresh: a pullback distribution
    # measured four minutes ago is still a fair description of the name, while
    # a support level that old may already be broken.
    max_age = max(60.0, refresh * 3.0)
    _overlay_stream_ohlc(sym, cfg, now)
    with _ohlc_cache_lock:
        hit = _ohlc_cache.get(sym)
        if hit and (now - hit[0]) < max_age:
            return list(hit[1])
    if _stream_bars_live(cfg):
        try:
            import stream_bars
            srows, sstamps = stream_bars.ohlc_with_stamps(sym)
            if srows and len(srows) == len(sstamps):
                return list(srows)
        except Exception:
            pass
    return []


def build_double_bottom_zone_for_symbol(
    symbol: str,
    last_price: float,
    cfg: dict | None = None,
    *,
    now: float | None = None,
    reason: str = "",
    lows: list[float] | None = None,
) -> dict[str, Any] | None:
    """Full path: bars → double bottom → zone structure. None if unavailable."""
    cfg = cfg if isinstance(cfg, dict) else {}
    t0 = time.time() if now is None else float(now)
    if lows is None:
        lows = _fetch_symbol_lows(symbol, cfg, t0)
    if not lows:
        return None
    try:
        swing = int(cfg.get("ai_watch_db_swing_bars", 2) or 2)
    except (TypeError, ValueError):
        swing = 2
    try:
        match_pct = float(cfg.get("ai_watch_db_match_pct", 0.40) or 0.40)
    except (TypeError, ValueError):
        match_pct = 0.40
    try:
        min_sep = int(cfg.get("ai_watch_db_min_sep_bars", 3) or 3)
    except (TypeError, ValueError):
        min_sep = 3
    found = find_double_bottom_support(
        lows, swing=swing, match_pct=match_pct, min_sep_bars=min_sep)
    if not found:
        return None
    return build_double_bottom_zone_structure(
        found["support"],
        cfg,
        reason=reason or "double_bottom",
        low_a=found.get("low_a"),
        low_b=found.get("low_b"),
        last_price=last_price,
    )


def build_last_zone_structure(
    price: float,
    cfg: dict | None = None,
    *,
    reason: str = "",
) -> dict[str, Any]:
    """Entry at the tape. Stop/target from fill so RSTOP has a real R.

    Not a pullback. ``entry_low``/``entry_high`` sit a tiny pad around last
    so the book still has a band to paint; the arm gate does not use it.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        px = float(price)
    except (TypeError, ValueError):
        px = 0.0
    if px <= 0:
        return {
            "decision": "WAIT",
            "wait_kind": "hard_no",
            "entry_low": 0.0,
            "entry_high": 0.0,
            "stop_price": 0.0,
            "target_1": 0.0,
            "reward_risk": 0.0,
            "synthetic": True,
            "zone_kind": "at_last",
            "summary": "at last: invalid price",
        }

    stop_pct = max(0.0, _opt_float(cfg.get("ai_watch_synth_stop_pct", 5.0), 5.0)) / 100.0
    rr = max(0.25, _opt_float(cfg.get("ai_watch_synth_rr"), 0.6))
    scale_out = max(1.0, min(99.0, _opt_float(
        cfg.get("ai_watch_synth_scale_out_pct"), 50.0)))
    trail_pct = max(0.0, _opt_float(cfg.get("ai_watch_synth_trail_pct"), 2.5))
    pad = max(0.0, _opt_float(cfg.get("ai_entry_limit_pad_pct"), 0.15)) / 100.0

    entry_high = px * (1.0 + pad)
    entry_low = px * (1.0 - pad)
    if entry_low <= 0 or entry_high <= entry_low:
        entry_low = px
        entry_high = px
    stop = px * (1.0 - stop_pct)
    if stop <= 0 or stop >= px:
        stop = px * 0.95
    risk = px - stop
    target = px + rr * risk

    def _r(x: float) -> float:
        if x >= 100:
            return round(x, 2)
        if x >= 1:
            return round(x, 3)
        return round(x, 4)

    return {
        "decision": "WAIT",
        "wait_kind": "wait_for_zone",
        "entry_low": _r(entry_low),
        "entry_high": _r(entry_high),
        "stop_price": _r(stop),
        "target_1": _r(target),
        "reward_risk": round(rr, 2),
        "scale_out_pct": scale_out,
        "trail_pct": trail_pct,
        "trail_method": "pct",
        "synthetic": True,
        "strategy": "day_scalp_v0",
        "anchor_price": _r(px),
        "zone_kind": "at_last",
        "summary": (
            f"at last {_r(px)} stop {_r(stop)} t1 {_r(target)}"
            + (f" · {reason}" if reason else "")
        ),
    }


def build_offset_zone_structure(
    price: float,
    cfg: dict | None = None,
    *,
    reason: str = "",
) -> dict[str, Any]:
    """Pullback entry zone from live price — no model required.

    Upper limit (``entry_high``) = price × (1 − offset%), i.e. a *negative*
    offset from the current print. Lower band and stop/target are derived so
    ``wait_for_zone`` + mechanical sizing still work.

    Defaults (overridable in bot_config):
      ai_watch_zone_offset_pct  — % below last to set buy-zone *upper* (5.0)
      ai_watch_zone_width_pct   — zone depth below that upper (2.0)
      ai_watch_synth_stop_pct   — stop distance below entry_low (2.0)
      ai_watch_synth_rr         — reward:risk to target_1 (3.0)
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        px = float(price)
    except (TypeError, ValueError):
        px = 0.0
    if px <= 0:
        return {
            "decision": "WAIT",
            "wait_kind": "hard_no",
            "entry_low": 0.0,
            "entry_high": 0.0,
            "stop_price": 0.0,
            "target_1": 0.0,
            "reward_risk": 0.0,
            "synthetic": True,
            "summary": "synth zone: invalid price",
        }

    def _pct(key: str, default: float) -> float:
        return max(0.0, _opt_float(cfg.get(key, default), default))

    offset = _pct("ai_watch_zone_offset_pct", 2.0) / 100.0
    width = _pct("ai_watch_zone_width_pct", 2.0) / 100.0
    stop_pct = _pct("ai_watch_synth_stop_pct", 5.0) / 100.0
    # Sub-1R first targets are intentional for day scalp (reachable T1).
    rr = max(0.25, _opt_float(cfg.get("ai_watch_synth_rr"), 0.6))
    scale_out = max(1.0, min(99.0, _opt_float(
        cfg.get("ai_watch_synth_scale_out_pct"), 50.0)))
    trail_pct = max(0.0, _opt_float(cfg.get("ai_watch_synth_trail_pct"), 2.5))

    # Upper buy limit sits *below* the print so we wait for a dip.
    entry_high = px * (1.0 - offset)
    entry_low = entry_high * (1.0 - width)
    if entry_low <= 0 or entry_high <= 0 or entry_low >= entry_high:
        entry_high = px * 0.99
        entry_low = px * 0.98
    # Stop is a percentage of the *entry price*, not a step below entry_low.
    # Derived-from-entry_low gave 2-4% of real risk depending on where in the
    # zone the fill landed, so position size swung ~1.9x between a fill at the
    # zone low and one at the zone top. Keying it to the price paid makes risk
    # per share — and therefore notional — constant.
    #
    # mid stands in for the fill here so reward_risk/target are coherent on the
    # UI before an order exists; _decision_for_place recomputes both off the
    # actual ask at placement.
    mid = (entry_low + entry_high) / 2.0
    stop = mid * (1.0 - stop_pct)
    if stop <= 0 or stop >= mid:
        stop = mid * 0.95
    risk = mid - stop
    target = mid + rr * risk

    def _r(x: float) -> float:
        # Tighter rounding for cheap names.
        if x >= 100:
            return round(x, 2)
        if x >= 1:
            return round(x, 3)
        return round(x, 4)

    off_pct = offset * 100.0
    return {
        "decision": "WAIT",
        "wait_kind": "wait_for_zone",
        "entry_low": _r(entry_low),
        "entry_high": _r(entry_high),
        "stop_price": _r(stop),
        "target_1": _r(target),
        "reward_risk": round(rr, 2),
        "scale_out_pct": scale_out,
        "trail_pct": trail_pct,
        "trail_method": "pct",
        "synthetic": True,
        "strategy": "day_scalp_v0",
        "anchor_price": _r(px),
        "summary": (
            f"synth pullback: upper {_r(entry_high)} "
            f"({off_pct:.1f}% under {_r(px)})"
            + (f" · {reason}" if reason else "")
        ),
    }


def build_band_zone_structure(
    price: float,
    entry_low: float,
    entry_high: float,
    cfg: dict | None = None,
    *,
    reason: str = "",
    meta: dict | None = None,
) -> dict[str, Any] | None:
    """Zone structure around a pre-measured pullback band.

    Same stop/target/rounding contract as build_offset_zone_structure — only
    the band comes from measurement instead of a fixed percentage, so sizing,
    the READY badge and _decision_for_place all keep working unchanged.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        px, lo, hi = float(price), float(entry_low), float(entry_high)
    except (TypeError, ValueError):
        return None
    if px <= 0 or lo <= 0 or hi <= lo:
        return None

    stop_pct = max(0.0, _opt_float(
        cfg.get("ai_watch_synth_stop_pct", 5.0), 5.0)) / 100.0
    rr = max(0.25, _opt_float(cfg.get("ai_watch_synth_rr"), 0.6))
    scale_out = max(1.0, min(99.0, _opt_float(
        cfg.get("ai_watch_synth_scale_out_pct"), 50.0)))
    trail_pct = max(0.0, _opt_float(cfg.get("ai_watch_synth_trail_pct"), 2.5))

    mid = (lo + hi) / 2.0
    stop = mid * (1.0 - stop_pct)
    if stop <= 0 or stop >= mid:
        stop = mid * 0.95
    risk = mid - stop
    target = mid + rr * risk

    def _r(x: float) -> float:
        if x >= 100:
            return round(x, 2)
        if x >= 1:
            return round(x, 3)
        return round(x, 4)

    m = meta or {}
    top_pct = float(m.get("depth_top_pct") or 0.0)
    bot_pct = float(m.get("depth_bottom_pct") or 0.0)
    out: dict[str, Any] = {
        "decision": "WAIT",
        "wait_kind": "wait_for_zone",
        "entry_low": _r(lo),
        "entry_high": _r(hi),
        "stop_price": _r(stop),
        "target_1": _r(target),
        "reward_risk": round(rr, 2),
        "scale_out_pct": scale_out,
        "trail_pct": trail_pct,
        "trail_method": "pct",
        "synthetic": True,
        "strategy": "day_scalp_v0",
        "anchor_price": _r(px),
        "zone_kind": "pullback_band",
        "summary": (
            f"pullback band {_r(hi)}–{_r(lo)} "
            f"({top_pct:.1f}–{bot_pct:.1f}% under {_r(px)}"
            f", {int(m.get('samples') or 0)} dips"
            + (f", decay {m.get('decay')}" if m.get("decay") not in (None, 1.0)
               else "")
            + ")"
            + (f" · {reason}" if reason else "")
        ),
    }
    for k in ("depth_top_pct", "depth_bottom_pct", "decay", "samples"):
        if k in m:
            out[k] = m[k]
    return out


def _structure_usable(structure: Any) -> bool:
    """True when structure has a real armable zone (not hard_no / empty)."""
    if not isinstance(structure, dict):
        return False
    wk = str(structure.get("wait_kind") or "").lower().strip()
    if wk == "hard_no":
        return False
    levels = _structure_levels(structure)
    return levels is not None


def _desk_source(rec: dict) -> bool:
    src = str(rec.get("source") or "").lower().strip()
    return src in ("momentum", "mom", "trending", "st", "stocktwits")


def ensure_offset_zone_if_needed(
    rec: dict,
    ask: float,
    cfg: dict,
    now: float,
) -> dict | None:
    """Attach a synthetic pullback zone for mom/ST when model zone is missing.

    Freezes the zone at first apply (anchor = live ask) so we wait for a dip
    rather than chasing. Returns an event dict when a zone is created/replaced.

    Arm-at-last rebuilds a tape-centered band every call so stop/target track
    the print the order will actually pay.
    """
    try:
        ask_f = float(ask)
    except (TypeError, ValueError):
        ask_f = 0.0
    if ask_f > 0 and arm_at_last(cfg):
        reason = str(rec.get("reason") or rec.get("source") or "")
        synth = build_last_zone_structure(ask_f, cfg, reason=reason)
        rec["structure"] = synth
        rec["structure_ts"] = float(now)
        if str(rec.get("status") or "").lower() in ("invalidated", "expired"):
            rec["status"] = "watching"
        return {
            "kind": "synth_zone",
            "symbol": str(rec.get("symbol") or "").upper(),
            "entry_low": synth.get("entry_low"),
            "entry_high": synth.get("entry_high"),
            "stop_price": synth.get("stop_price"),
            "target_1": synth.get("target_1"),
            "anchor": synth.get("anchor_price"),
            "support": synth.get("support"),
            "zone_kind": "at_last",
            "reason": "at_last",
        }
    # Applies to every source, not just momentum/trending. Research records
    # used to be excluded here, and the LLM refresh below only fires when a
    # structure is *unusable* — so a stale-but-parseable research zone was
    # never refreshed by either path. On 2026-08-04 the whole book was research
    # records and not one synth_zone event was logged all day; HPE sat on a
    # 47.75-48.85 zone against a 52.85 ask.
    if not isinstance(rec, dict):
        return None
    if not bool(cfg.get("ai_watch_synth_zone_enabled", True)):
        return None
    try:
        ask_f = float(ask)
    except (TypeError, ValueError):
        return None
    if ask_f <= 0:
        return None

    structure = rec.get("structure") if isinstance(rec.get("structure"), dict) else None
    sym_early = str(rec.get("symbol") or "").upper()

    # Is the zone we already hold still within reach of the print?
    #
    # Every other exit from this function keeps the existing zone, and the
    # re-anchor test below only fires when price runs ABOVE the anchor the zone
    # was drawn from. So a zone that was never reachable at the moment it was
    # created is never revisited: ABCL sat at 5.58-5.67 against a 9.21 print
    # (-38%) with anchor_price 9.32 — price was *below* its anchor, so no
    # re-anchor, and the row waited all session for a 40% crash. Eight of
    # thirteen rows were in that state on 2026-08-10.
    #
    # This is what makes the band a live constraint rather than a build-time
    # one. Bars come from the throttled cache (>=120s per symbol), so the
    # re-check costs nothing on most polls.
    force_rebuild = False
    if _structure_usable(structure):
        # Price fell through the armable overshoot window → the band is a
        # breakdown, not a dip. Rebuild under the new print so the book does
        # not sit on a permanent "below zone" that can never arm. Without this,
        # force_rebuild only fired when variable_zone_band had bars; a dark
        # OHLC cache left UPST-class names skipped as below_zone all session.
        lv_now = _structure_levels(structure)
        if lv_now is not None:
            z_lo, z_hi, z_stop = (
                min(lv_now[0], lv_now[1]),
                max(lv_now[0], lv_now[1]),
                lv_now[2],
            )
            max_r = arm_below_max_r(cfg)
            floor = armable_below_floor(
                z_lo, z_hi, z_stop, pad_pct=0.0, max_r=max_r)
            # No valid dip window (tight structural stop) → any print under
            # the floor is already a breakdown.
            if floor is None:
                if ask_f < z_lo:
                    force_rebuild = True
            elif ask_f < floor:
                force_rebuild = True
        if not force_rebuild and bool(cfg.get("ai_watch_zone_variable", True)):
            try:
                rows = symbol_ohlc(sym_early, cfg, float(now))
                if not rows:
                    _fetch_symbol_lows(sym_early, cfg, float(now))
                    rows = symbol_ohlc(sym_early, cfg, float(now))
                reach = variable_zone_band(ask_f, rows, cfg, float(now))
                if reach is not None and lv_now is not None:
                    z_lo, z_hi = min(lv_now[0], lv_now[1]), max(lv_now[0], lv_now[1])
                    if z_hi < reach[0] or z_lo > reach[1]:
                        force_rebuild = True
            except Exception:
                pass

    # Keep a good *model* zone, but only while it is fresh — a stale model zone
    # describes a price that has moved on, so let the synth path replace it.
    if (
        _structure_usable(structure)
        and not structure.get("synthetic")
        and not _structure_stale(rec, cfg, now)
        and not force_rebuild
    ):
        return None

    reanchor = False
    if (
        _structure_usable(structure)
        and structure.get("synthetic")
        and not _structure_stale(rec, cfg, now)
        and not force_rebuild
    ):
        # Re-anchor when price has run above the level the zone was drawn
        # FROM, so the band follows a name that got away (e.g. ZETA stuck at
        # $24 while printing $28) without chasing one that is coming back.
        #
        # This used to compare against entry_high. The zone sits
        # ai_watch_zone_offset_pct BELOW its anchor, so `ask > entry_high` was
        # true on essentially every poll — including while price fell — and the
        # band was redrawn under each new lower print. Price could then only
        # enter it by dropping more than the offset inside a single poll
        # interval, i.e. a crash rather than a pullback.
        #
        # 2026-08-06 measured the damage: 22 zones drawn, 4 ever touched, 0
        # armed, 0 trades. The three that touched fell 12%, 24% and 30% in
        # minutes; every gradual pullback (SOUN -2.4%, IOVA -0.7%) watched the
        # zone retreat ahead of it. See tools/pullback_study.py.
        try:
            anchor = float(structure.get("anchor_price")
                           or structure.get("anchor") or 0)
        except (TypeError, ValueError):
            anchor = 0.0
        if anchor <= 0:
            # Pre-existing zone with no recorded anchor — reconstruct it from
            # the band rather than falling back to the broken comparison.
            try:
                hi = float(structure.get("entry_high") or 0)
                off = float(cfg.get("ai_watch_zone_offset_pct", 2.0) or 0.0)
                anchor = hi / (1.0 - off / 100.0) if hi > 0 and off < 100 else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                anchor = 0.0
        try:
            re_pct = max(
                0.0,
                float(cfg.get("ai_watch_synth_reanchor_pct", 0.0) or 0.0),
            ) / 100.0
        except (TypeError, ValueError):
            re_pct = 0.0
        if anchor > 0 and ask_f > anchor * (1.0 + re_pct):
            reanchor = True
        else:
            return None

    reason = str(rec.get("reason") or rec.get("source") or "")
    if reanchor:
        reason = (reason + " · reanchor").strip(" ·")
    sym = str(rec.get("symbol") or "").upper()
    mode = str(cfg.get("ai_watch_zone_mode") or "pullback").lower().strip()
    synth: dict[str, Any] | None = None
    zone_reason = "offset_from_last"

    # Pullback is the default wait: a band under last, sized from this
    # name's own dips. Double-bottom is optional and rare on 1m heat.
    if mode in ("double_bottom", "db", "structure"):
        synth = build_double_bottom_zone_for_symbol(
            sym, ask_f, cfg, now=float(now), reason=reason)
        if synth is not None:
            zone_reason = (
                "reanchor_double_bottom" if reanchor else "double_bottom")

    # Reachable band for this name, measured from its own pullback history.
    # Deliberately computed AFTER the double-bottom attempt: that call is what
    # fetches and caches the bars (_fetch_symbol_lows fills _ohlc_cache), so
    # measuring first would see an empty cache on a symbol's first poll and
    # silently skip the reachability check below — exactly when a fresh name is
    # most likely to be handed an out-of-reach zone.
    band = None
    if bool(cfg.get("ai_watch_zone_variable", True)):
        try:
            band = variable_zone_band(
                ask_f, symbol_ohlc(sym, cfg, float(now)), cfg, float(now))
        except Exception:
            band = None

    # Structure wins only while it is reachable. A double bottom found over 90
    # bars can sit 25% under the print, and on 2026-08-10 that is exactly what
    # happened to all eleven rows: every one parked at above_zone for the whole
    # session and the book took no entries. Below the band floor the level is
    # real but not tradable today, so fall back rather than wait on a dip that
    # is not coming.
    if synth is not None and band is not None:
        db_levels = _structure_levels(synth)
        band_low, band_high, _bmeta = band
        if db_levels is not None:
            db_low = min(db_levels[0], db_levels[1])
            db_high = max(db_levels[0], db_levels[1])
            if db_high < band_low:
                synth = None
                zone_reason = "db_out_of_reach"
            elif db_low > band_high:
                # Structure sits above the entry band — price is under its own
                # support, which is a broken level, not a dip.
                synth = None
                zone_reason = "db_above_price"

    if synth is None and band is not None:
        band_low, band_high, bmeta = band
        synth = build_band_zone_structure(
            ask_f, band_low, band_high, cfg, reason=reason, meta=bmeta)
        if synth is not None:
            synth.setdefault("zone_kind", "pullback_band")
            if zone_reason not in ("db_out_of_reach", "db_above_price"):
                zone_reason = "pullback_band"
            if reanchor:
                zone_reason = f"reanchor_{zone_reason}"

    if synth is None:
        # Bars missing, no matching lows, or mode=offset → legacy % band.
        synth = build_offset_zone_structure(ask_f, cfg, reason=reason)
        synth.setdefault("zone_kind", "offset")
        zone_reason = "reanchor_from_last" if reanchor else "offset_from_last"
    rec["structure"] = synth
    rec["structure_ts"] = float(now)
    if str(rec.get("status") or "").lower() in ("invalidated", "expired"):
        rec["status"] = "watching"
    return {
        "kind": "synth_zone",
        "symbol": sym,
        "entry_low": synth.get("entry_low"),
        "entry_high": synth.get("entry_high"),
        "stop_price": synth.get("stop_price"),
        "target_1": synth.get("target_1"),
        "anchor": synth.get("anchor_price"),
        "support": synth.get("support"),
        "zone_kind": synth.get("zone_kind"),
        "reason": zone_reason,
    }


def _entry_features(rec: dict, *, ask: float | None = None,
                    bid: float | None = None,
                    stop: float | None = None) -> dict[str, Any]:
    """Decision-time feature vector, for retrospective A/B slicing.

    Snapshotted at the moment of entry and carried onto the position, so the
    outcome record lands denormalized — features and result on one row, no
    join needed. Deliberately records the values the gates SAW, not the
    thresholds they were compared against: thresholds live in config and
    change, so a stored threshold tells you nothing a config diff wouldn't.

    Live trading cannot A/B its own features at this desk's volume (a 0.1R
    edge needs ~780 trades per arm). This is instrumentation for observation
    and for slicing the replay harness — not an experiment.
    """
    rec = rec if isinstance(rec, dict) else {}
    sig = rec.get("indicator")
    sig = sig if isinstance(sig, dict) else {}
    admitted = _f_or_none(rec.get("admit_ts"))
    now = time.time()
    return {
        # Which pipeline claimed it, and what its numbers were at admission.
        "source": str(rec.get("source") or "") or None,
        "score": _f_or_none(rec.get("score")),
        "rvol": _f_or_none(rec.get("admit_rvol")),
        "pct_change": _f_or_none(rec.get("admit_pct_change")),
        "look_reason": rec.get("admit_look_reason"),
        "criteria": list(rec.get("admit_criteria") or []),
        # Indicator state at the moment of arming — the timing question,
        # separate from the selection question above.
        "cm_ok": bool(sig.get("cm_ok")),
        "pctr_ok": bool(sig.get("pctr_ok")),
        "cm_rsi_rising": bool(sig.get("cm_rsi_rising")),
        "macd_ok": bool(sig.get("macd_ok")),
        "cm_rsi": _f_or_none(sig.get("cm_rsi")),
        "pctr": _f_or_none(sig.get("pctr")),
        "pctr_slow": _f_or_none(sig.get("pctr_slow")),
        "pctr_gap": _f_or_none(sig.get("pctr_gap")),
        "pctr_ob": (
            bool(sig.get("pctr_ob")) if sig.get("pctr_ob") is not None else None
        ),
        "pctr_tight": (
            bool(sig.get("pctr_tight")) if sig.get("pctr_tight") is not None
            else None
        ),
        # Entry square is live dual only; pin false so a false-■ cannot hide.
        "sticky_used": False,
        # Which tape the levers were on at arm. Gate 1 (min-hold) is only
        # evidence about the realtime product when these are live/realtime.
        "pctr_src": str(sig.get("pctr_src") or "").strip() or None,
        "cm_rsi_src": (
            str(sig.get("cm_rsi_src") or sig.get("bars_src") or "").strip()
            or None
        ),
        "bars_age_sec": _f_or_none(
            sig.get("cm_rsi_age_sec")
            if sig.get("cm_rsi_age_sec") is not None
            else sig.get("bars_age_sec")
        ),
        "proximity_pct": _f_or_none(sig.get("proximity_pct")),
        # Time-of-day and dwell: an open-drive entry and a 15:00 entry facing
        # the 15:50 flatten are different trades with the same signal.
        "entry_hour_et": _et_hour_decimal(now),
        "dwell_sec": round(now - admitted, 1) if admitted else None,
        # Seat class / arm class for late-vs-dead + farm scoring (not seed text).
        "exh_seat_class": (
            str(rec.get("exh_seat_class") or "").strip().lower() or None
        ),
        "exh_seat_class_admit": (
            str(rec.get("exh_seat_class_admit") or rec.get("exh_seat_class")
                or "").strip().lower() or None
        ),
        "square_since": _f_or_none(rec.get("square_since")),
        # Volume pace vs this stock's own normal (SIP, 16-min delayed) at the
        # arm pass; None when the observe/enforce gate is off or not yet served.
        "rvol_pace_sip": _f_or_none(rec.get("rvol_pace_sip")),
        "ask": _f_or_none(ask),
        # What crossing cost on THIS fill. The shadow log prices candidates,
        # but until now nothing priced consequences: ai_max_spread_r sits at 0
        # "until it can be set from these rows rather than guessed", and the
        # outcome rows — the only place cost meets result — carried the ask
        # alone, so the round trip could not be reconstructed afterwards.
        #
        # It matters at this desk's scale. Across 2026-08-11..20 the median
        # candidate spread was 0.048R and the p90 was 3.89R, against a median
        # trade MFE of 0.046R: at the p90 the round trip is eighty times the
        # move it is trying to capture. Same arithmetic the gate enforces, so
        # a threshold read off these rows means what the gate will mean.
        "bid": _f_or_none(bid),
        "spread_r": _spread_r(ask, bid, stop),
        # 1m volatility at entry, for ai_local_trail_give_vol_k: the trail
        # cushion scaled to how far this name moves in a minute.
        "vol_1m_pct": _vol_1m_pct(str(rec.get("symbol") or ""), now),
    }


def _vol_1m_pct(sym: str, now: float, n: int = 15,
                max_age: float = 180.0, window: int = 20) -> float | None:
    """Stdev of one-minute close-to-close returns, %, from the cached IEX bars
    the arm pass already fetched.

    Only returns between adjacent minutes count. A thin IEX tape skips minutes
    (TOST/VIAV/BMNR lost 1-3 of 15 on 2026-09-25), and a return taken across
    a gap folds several minutes of drift into one "1m" step, which overstates
    the volatility. So the last *window* bars are searched for adjacent pairs
    and at least ``n - 5`` are required. The newest bar must end within
    *max_age* of now. None otherwise: better no reading than a wide wrong one.
    Close to tools/studies/vol_trail_book_study.py (15 SIP minutes).
    """
    sym = str(sym or "").upper()
    if not sym:
        return None
    with _ohlc_cache_lock:
        hit = _ohlc_cache.get(sym)
        ts_hit = _ohlc_ts_cache.get(sym)
    if not hit or not ts_hit:
        return None
    rows, stamps = hit[1], ts_hit[1]
    if len(rows) != len(stamps) or len(rows) < 2:
        return None
    rows, stamps = rows[-(window + 1):], stamps[-(window + 1):]
    if now - (float(stamps[-1]) + 60.0) > max_age:
        return None
    try:
        closes = [float(r[2]) for r in rows]
    except (TypeError, ValueError, IndexError):
        return None
    rets = [(closes[i] / closes[i - 1] - 1.0) * 100.0
            for i in range(1, len(closes))
            if abs(float(stamps[i]) - float(stamps[i - 1]) - 60.0) <= 1.0
            and closes[i - 1] > 0 and closes[i] > 0]
    rets = rets[-n:]
    if len(rets) < max(2, n - 5):
        return None
    mean = sum(rets) / len(rets)
    return round((sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5, 4)


# symbol -> last ts a reject sample was written. The book is rebuilt every 2s;
# logging every reject on every rebuild would write ~15k rows an hour and say
# nothing a 60s series does not.
_reject_last_logged: dict[str, float] = {}
_REJECT_LOG_EVERY_SEC = 60.0


def _log_rejects(
    rejected: list[dict],
    by_symbol: dict[str, dict],
    cfg: dict,
    now: float,
) -> None:
    """Sample candidates admission turned away, so the gate has a second arm.

    Without this a filter is only ever observed on what it passed, which
    cannot distinguish a gate that removes losers from one that removes
    winners. Prices are taken from the candidate row the screeners already
    refreshed — never a fresh quote.
    """
    if not bool(cfg.get("ai_reject_log_enabled", True)):
        return
    import ai_positions as cp  # module-scope name does not exist here
    for rej in rejected or []:
        if not isinstance(rej, dict):
            continue
        sym = str(rej.get("symbol") or "").upper().strip()
        if not sym:
            continue
        reason = str(rej.get("reason") or "")
        # Dwell is not a verdict — the name is mid-admission and will be
        # sampled properly once it is on the book. Logging it would pollute
        # the reject arm with names that were never actually turned away.
        if reason.startswith("dwell_"):
            continue
        last = _reject_last_logged.get(sym)
        if last is not None and (now - last) < _REJECT_LOG_EVERY_SEC:
            continue
        _reject_last_logged[sym] = now
        row = by_symbol.get(sym) or {}
        try:
            cp.log_reject_sample({
                "ts": round(float(now), 2),
                "symbol": sym,
                "reason": reason,
                "price": _f_or_none(row.get("price")),
                # Same feature names the shadow log and entry vector use, so
                # admitted and rejected arms are directly comparable.
                "source": str(row.get("source") or "") or None,
                "score": _f_or_none(row.get("score")),
                "rvol": _f_or_none(row.get("rvol")),
                "pct_change": _f_or_none(row.get("pct_change")),
                "look_reason": _look_reason_value(row),
                "criteria": list(rej.get("criteria") or row.get("criteria") or []),
                "entry_hour_et": _et_hour_decimal(now),
            })
        except Exception:
            pass


def _spread_r(ask: float | None, bid: float | None,
              stop: float | None) -> float | None:
    """Round-trip spread as a fraction of R, or None when unknowable.

    Crossing is paid twice: buy at the ask, sell at the bid. Mirrors the
    max_spread_r arithmetic in ai_positions.pre_entry_gate so a threshold set
    off these rows means the same thing the gate will enforce.
    """
    a, b, st = _f_or_none(ask), _f_or_none(bid), _f_or_none(stop)
    if a is None or b is None or st is None:
        return None
    if not (a > 0 and b > 0 and 0 < st < a):
        return None
    # ask == bid is not a free round trip, it is a missing bid. Every one of
    # the 856 zero-spread rows on 2026-08-11..20 had them identical to the
    # penny — including PFE and BMNR, where a genuinely locked book would be
    # remarkable rather than routine. Some quote paths hand back the last
    # price for both sides, and recording that as 0.000 puts the names whose
    # book the desk CANNOT see at the top of the cheapest bucket, which is
    # precisely where a spread-priority rule would go looking. Unknowable is
    # None. A truly locked market is untradeable anyway.
    if b >= a:
        return None
    risk = a - st
    if risk <= 0:
        return None
    return round(2.0 * (a - b) / risk, 5)


def _tape_age_for_shadow(rec: dict) -> float | None:
    """Age of the live tape print for this record, or None if unprovable."""
    try:
        got = live_print(str((rec or {}).get("symbol") or ""))
    except Exception:  # noqa: BLE001
        return None
    if not got:
        return None
    age = got[1]
    if age is None:
        return None
    try:
        return round(max(0.0, float(age)), 2)
    except (TypeError, ValueError):
        return None


def _shadow_row(
    rec: dict,
    *,
    price: float | None,
    price_src: str,
    arm_ok: bool | None,
    arm_why: str,
    now: float,
    bid: float | None = None,
) -> dict[str, Any]:
    """One counterfactual sample: the decision, and the price that tested it.

    Deliberately flat and self-describing rather than a reference into the
    book — the book is rebuilt every 2s and a symbol can be admitted twice in
    a session, so anything joined by symbol alone would silently mix them.

    Carries only what a slice needs. Forward return, first zone touch and the
    would-have-been outcome of a blocked entry are all reconstructed
    downstream from a series of these.
    """
    rec = rec if isinstance(rec, dict) else {}
    stru = rec.get("structure") if isinstance(rec.get("structure"), dict) else {}
    sig = rec.get("indicator") if isinstance(rec.get("indicator"), dict) else {}
    lo = _f_or_none(stru.get("entry_low"))
    hi = _f_or_none(stru.get("entry_high"))
    px = _f_or_none(price)
    in_zone = bool(lo is not None and hi is not None and px is not None
                   and lo <= px <= hi)
    sym_u = str(rec.get("symbol") or "").upper()
    news = _news_fields(sym_u, now)
    setup = _setup_fields(rec, sym_u, px, sig, news)
    stream = _stream_pctr_fields(sym_u, px, _push_cfg(), now)
    return {
        "ts": round(float(now), 2),
        "symbol": str(rec.get("symbol") or "").upper(),
        "price": px,
        "price_src": price_src,          # "quote" (ask) or "tape" (trade print)
        "status": str(rec.get("status") or ""),
        # Zone geometry, so reachability is answerable without re-deriving it.
        "entry_low": lo,
        "entry_high": hi,
        "stop_price": _f_or_none(stru.get("stop_price")),
        "target_1": _f_or_none(stru.get("target_1")),
        "in_zone": in_zone,
        # The gate verdict at this instant. arm_ok False WITH in_zone True is
        # the interesting row: price was there and the desk refused.
        "arm_ok": arm_ok,
        "arm_why": arm_why or "",
        # Selection provenance — same fields the entry feature vector uses, so
        # a shadow slice and a filled-trade slice are directly comparable.
        "source": str(rec.get("source") or "") or None,
        "score": _f_or_none(rec.get("score")),
        "rvol": _f_or_none(rec.get("admit_rvol")),
        # Is that RVOL even a number? 3.94% of readings on this log are above
        # 100 (max 81,820), which is not a relative volume — and because the
        # floor test is `rv < min_rvol`, a garbage-high reading PASSES the
        # thin-tape gate. Recorded rather than clamped: clamping would edit
        # the evidence, and the gate itself is frozen for GATE 1.
        "rvol_ok": _rvol_is_sane(rec.get("admit_rvol")),
        "dollar_volume": _f_or_none(rec.get("admit_dollar_volume")),
        # The two numbers RVOL is made of. Logged so a reading of 3,144 can
        # be attributed to a bad numerator or a near-zero denominator
        # instead of only being flagged and dropped.
        "vol_session": _f_or_none(rec.get("admit_vol_session")),
        "avg_vol": _f_or_none(rec.get("admit_avg_vol")),
        "rvol_raw": _f_or_none(rec.get("admit_rvol_raw")),
        # WHY the name is moving — the first genuinely new input on this row.
        # Read from a cache the watchdog keeps warm, never fetched here: the
        # poll has ~2s to decide and a news call belongs nowhere near it.
        # All-None means the cache had nothing for this symbol, which is "we
        # did not look", not "there was no catalyst". news_cache_age_sec is
        # what separates those two, and a dead refresher shows up there
        # rather than as a quiet week of uneventful names.
        "news_n_24h": news["n_news_24h"],
        "news_mins_since": news["mins_since"],
        "news_bearish": news["bearish"],
        "news_bullish": news["bullish"],
        "news_cache_age_sec": news["cache_age_sec"],
        # THE SUPPLY SIDE. Shares outstanding in millions — an upper bound
        # on float, so a low reading is trustworthy and a high one may
        # still hide a small float. This is the first *cause* on the row:
        # every other strength column (pct_change, rvol, score, extension)
        # measured anti-predictive in 2026-08 because 5x volume means
        # opposite things on a 3M-share company and a 500M-share one.
        "shares_out_m": setup["shares_out_m"],
        # The operator's stage-1 conjunction, evaluated live so tomorrow's
        # tape is sliceable without reconstructing five conditions from
        # four logs. It fires on ~5% of name-days, which is precisely why
        # every marginal gate this lab tested read as the null.
        "setup_ok": setup["ok"],
        "setup_legs": setup["legs"],
        "setup_n_legs": setup["n_legs"],
        # Stage 2 — the timing rule, which has never been recorded. Both
        # lines travelling to overbought together is the move; one turning
        # while the other has not is where the gain stops.
        "pctr_rising": setup["pctr_rising"],
        "pctr_slow_rising": setup["pctr_slow_rising"],
        "pctr_slow_falling": setup["pctr_slow_falling"],
        "pctr_both_rising": setup["pctr_both_rising"],
        "pctr_diverging": setup["pctr_diverging"],
        "rsi_at_bottom": setup.get("rsi_at_bottom"),
        "rsi_at_top": setup.get("rsi_at_top"),
        "setup_entry_ok": setup.get("setup_entry_ok"),
        "setup_exit_ok": setup.get("setup_exit_ok"),
        # SHADOW: the same %R over Finnhub stream bars instead of IEX bars.
        # Decides nothing. Logged beside `pctr` / `pctr_src` /
        # `window_span_min` so the question "would a denser feed fix the
        # 23-minute window" gets a measurement rather than my estimate.
        # stream_bar_count near zero early in a name's life is the
        # forward-only limitation showing, not a fault.
        "pctr_stream": stream["pctr_stream"],
        "pctr_stream_src": stream["pctr_stream_src"],
        "pctr_stream_bars": stream["pctr_stream_bars"],
        "pctr_stream_span_sec": stream["pctr_stream_span_sec"],
        "stream_bar_count": stream["stream_bar_count"],
        "stream_empty_min": stream["stream_empty_min"],
        # Same for RSI: live_cm_rsi's exact arithmetic (cm_rsi_series) over
        # stream-built bars. cm_rsi_src today is 65% realtime / 35% a REST
        # fallback, and both update on bar close rather than on tick.
        "cm_rsi_stream": stream["cm_rsi_stream"],
        "cm_rsi_stream_rising": stream["cm_rsi_stream_rising"],
        "cm_rsi_stream_bars": stream["cm_rsi_stream_bars"],
        # Day change was recorded on the reject arm but omitted here, so the
        # completeness report read 0% admitted / 91% rejected and the two arms
        # could not be compared on the gate (ai_watch_require_uptrend) that
        # does most of the actual filtering — 20 of 28 rejects on 2026-08-06.
        "pct_change": _f_or_none(rec.get("admit_pct_change")),
        "look_reason": rec.get("admit_look_reason"),
        "criteria": list(rec.get("admit_criteria") or []),
        "admit_ts": _f_or_none(rec.get("admit_ts")),
        # Exhaustion state — the rule that now decides entries, so it has to be
        # on the row that scores them. Without the LEVEL a slice can only ask
        # "did the gate pass", never "did buying at 85% beat buying at 55%",
        # which is the question the heat floor was guessed at.
        "exhaustion": _f_or_none(exhaustion_pct(rec)),
        "exhaustion_state": exhaustion_state(rec, _push_cfg()),
        "pctr": _f_or_none(sig.get("pctr")) if sig else None,
        # "live" = recomputed against the live price; "engine" = the 60-120s
        # copy. A row scored without knowing which is scoring two rules at once.
        "pctr_src": (sig.get("pctr_src") or "engine") if sig else None,
        "macd_src": (str(sig.get("macd_src") or "").strip().lower() or None) if sig else None,
        "macd_age_sec": _f_or_none(sig.get("macd_age_sec")) if sig else None,
        "last_ask_src": rec.get("last_ask_src"),
        "last_ask_age_sec": _f_or_none(rec.get("last_ask_age_sec")),
        # Minutes the %R window actually spans. Logged even when the reading
        # was refused for being too wide, because the threshold that refused it
        # is a guess (3x) and this column is the only way to sweep it: bucket
        # forward return by span and the cutoff stops being an opinion.
        "window_span_min": _window_span_min(rec),
        # The three inputs the live buy rule actually compares, as LEVELS.
        #
        # _tv_exh_rsi_allows_buy tests both %R lines against rte_threshold,
        # their gap against rte_confluence_max, then CM RSI-2 against
        # cm_rsi_buy_max. Only the fast line was on this row, so two of those
        # three thresholds had no recorded input at all and could not be swept
        # — the same hole that made the heat floor a guess (see the comment on
        # "exhaustion" above). Booleans are not enough: cm_ok says the gate
        # passed, never what it would have done at a different cutoff.
        # Crossing cost, in the unit that decides whether it matters. The
        # percent-of-mid spread answers "is this book wide for a $50 stock",
        # which is not the question — the question is what fraction of the
        # money at risk the round trip eats, and on these zones 1R is ~5% of
        # price so the two readings differ by an order of magnitude.
        # ai_max_spread_r is the one spread gate wired into the fill path and
        # it is off, because nothing on disk said what crossing actually costs.
        # This is that record; the threshold stays 0 until it can be set from
        # these rows rather than guessed.
        # How stale the Finnhub print was when this row was written. The stream
        # is the only free real-time source on the table — Alpaca's IEX bars are
        # one exchange with a small share of the tape, and paid SIP is out of
        # scope — so whether a dense 1-minute bar can be built from it is the
        # question that decides the whole data plan. Nothing was recording it:
        # live_print hands back an age and it went nowhere.
        #
        # This is age-at-observation, not a print arrival stamp, but the poll
        # samples every book name every couple of seconds, so its distribution
        # is the inter-print interval. Ages clustered under a second or two mean
        # a bar is constructible; a median in the tens of seconds means the
        # instrument does not trade often enough for any feed to fix.
        #
        # None when the desk has a number but cannot prove it is live — that is
        # not zero, and must not be read as fresh.
        "tape_age_sec": _tape_age_for_shadow(rec),
        "bid": _f_or_none(bid),
        # The other side of the book, stated rather than implied. When
        # price_src is "quote" the price IS the ask, so spread_r was
        # derivable — but only by someone who knew that, and outcomes.jsonl
        # already carries `ask` explicitly. Symmetry beats a footnote.
        "ask": px if price_src == "quote" else None,
        "spread_r": _spread_r(price, bid, stru.get("stop_price")),
        "pctr_slow": _f_or_none(sig.get("pctr_slow")) if sig else None,
        "pctr_gap": _f_or_none(sig.get("pctr_gap")) if sig else None,
        "pctr_ob": bool(sig.get("pctr_ob")) if sig else None,
        "pctr_tight": bool(sig.get("pctr_tight")) if sig else None,
        "cm_rsi": _f_or_none(sig.get("cm_rsi")) if sig else None,
        # Timing state.
        "cm_ok": bool(sig.get("cm_ok")) if sig else None,
        "pctr_ok": bool(sig.get("pctr_ok")) if sig else None,
        "cm_rsi_rising": bool(sig.get("cm_rsi_rising")) if sig else None,
        # Which pipe drew the bars behind the RSI. Without it the trust report
        # can only say "unknown" for every row, and the realtime-vs-fallback
        # split — the thing that decides whether this reading may gate an
        # entry — is unmeasurable after the fact. See tools/rsi_trust.py.
        "cm_rsi_src": (sig.get("cm_rsi_src") or None) if sig else None,
        # HOW OLD the levers were when this row was scored. Provenance without
        # age answers "which pipe" and never "how stale", and the desk gates
        # arming on the age of the PRINT (_row_tape_stale, 8s) while %R and
        # RSI come from bars it never times. Across 8/24-26 cm_rsi_age_sec was
        # absent from all 17,585 RTH rows and pctr_age_sec did not exist, so
        # "were EXH and RSI fresh at the arm?" could not be answered from the
        # record at all — it had to be read off a live process, which cannot
        # be done retroactively for a session already gone.
        #
        # Two numbers, not one, because they fail independently and add:
        # bars_age_sec is engine-side (the tape the reading was computed on),
        # ind_snapshot_age_sec is transport-side (the age of this process's
        # copy). Deliberately NOT summed here — a slice can add them, and a
        # single blended figure would hide which half broke.
        #
        # No pctr_age_sec: when pctr_src is "live" the %R came off these same
        # bars, so it is this number, and when it is sparse_window/clock_range
        # /engine the age is genuinely unknown. Duplicating the value under a
        # second name would invite the two to drift apart and imply a
        # measurement that was never taken.
        "bars_age_sec": (_f_or_none(
            sig.get("cm_rsi_age_sec")
            if sig.get("cm_rsi_age_sec") is not None
            else sig.get("bars_age_sec")
        ) if sig else None),
        "ind_snapshot_age_sec": dashboard_state_age_sec(),
        "sell_signal": bool(sig.get("sell_signal")) if sig else None,
        "proximity_pct": _f_or_none(sig.get("proximity_pct")) if sig else None,
        # MACD, which was absent from every one of the 176,081 rows written
        # before 2026-08-30. It is the PRIMARY lever — the override's second
        # leg and both halves of the standard path — and none of it was
        # recorded, so the entry gate could not be replayed from its own log
        # at all. Two questions died on that in one afternoon: "what would an
        # RSI condition cost" had to be answered against arm_ok as a proxy,
        # and "what does lowering macd_sep_mult admit" could not be answered
        # at any price.
        #
        # Every field the gate reads, under the names it reads them by, so a
        # replay is a lookup rather than a reconstruction:
        #   macd_gap        the size the 0.005 floor tests
        #   macd_sep_ratio  the multiple macd_sep_mult tests
        #   macd_gap_rising the override's MACD leg, and the "opening" term
        #   macd_gap_falling the standard path's refusal
        #   macd_bull/_ok   the engine's own verdicts
        #   macd_src/_age   provenance and staleness, since the gate refuses a
        #                   MACD not drawn on the live tape BEFORE any rule
        #
        # Direction stays tri-state. None is "too few bars to say", which the
        # gate treats differently from False, and flattening it here would
        # make a refusal indistinguishable from a held gap after the fact.
        "macd_gap": _f_or_none(
            sig.get("macd_gap") if sig.get("macd_gap") is not None
            else sig.get("macd_hist")) if sig else None,
        "macd_sep_ratio": _f_or_none(sig.get("macd_sep_ratio")) if sig else None,
        "macd_gap_prev": _f_or_none(sig.get("macd_gap_prev")) if sig else None,
        "macd_gap_rising": (
            None if not sig or sig.get("macd_gap_rising") is None
            else bool(sig.get("macd_gap_rising"))),
        "macd_gap_falling": (
            None if not sig or sig.get("macd_gap_falling") is None
            else bool(sig.get("macd_gap_falling"))),
        "macd_bull": bool(sig.get("macd_bull")) if sig else None,
        "macd_cross": bool(sig.get("macd_cross")) if sig else None,
        "macd_ok": bool(sig.get("macd_ok")) if sig else None,
        "macd_src": (sig.get("macd_src") or None) if sig else None,
        "macd_age_sec": _f_or_none(sig.get("macd_age_sec")) if sig else None,
        "entry_hour_et": _et_hour_decimal(now),
    }


def _arm_day_change(record: dict) -> float | None:
    """Session % change the arm gate sees (admit stamp, else features)."""
    rec = record if isinstance(record, dict) else {}
    for key in ("admit_pct_change", "pct_change"):
        got = _f_or_none(rec.get(key))
        if got is not None:
            return got
    feat = rec.get("features")
    if isinstance(feat, dict):
        return _f_or_none(feat.get("pct_change"))
    return None


def _arm_rvol(record: dict) -> float | None:
    """RVOL the arm gate sees. Live stamp on the record, else admit.

    Poll stamps a fresh desk rvol onto ``record['rvol']`` before this
    runs. Known-low refuses; missing everywhere → None (abstain).
    """
    rec = record if isinstance(record, dict) else {}
    for key in ("rvol", "admit_rvol"):
        got = _f_or_none(rec.get(key))
        if got is not None:
            return got
    feat = rec.get("features")
    if isinstance(feat, dict):
        return _f_or_none(feat.get("rvol"))
    return None


def _desk_pct_change(symbol: str) -> float | None:
    """Live percent change off the dashboard row, if the desk has one.

    Mirrors _desk_rvol and reads the same cached row, so ranking on the move
    costs no extra quote call.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None
    try:
        for r in _dashboard_tickers():
            if not isinstance(r, dict):
                continue
            key = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
            if key != sym:
                continue
            return _f_or_none(r.get("pct_change"))
    except Exception:
        return None
    return None


def _desk_rvol(symbol: str) -> float | None:
    """Live RVOL off the dashboard row, if the desk has one."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None
    try:
        for r in _dashboard_tickers():
            if not isinstance(r, dict):
                continue
            key = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
            if key != sym:
                continue
            return _f_or_none(r.get("rvol"))
    except Exception:
        return None
    return None


def arm_sources_allow(cfg: dict | None) -> frozenset[str] | None:
    """RTH buy allow-list. None means every source may arm.

    ``*`` / empty / ``all`` / ``any`` leave the book unchanged. A comma
    list (or a list) is the only restrictive form. Missing key is open,
    so partial test configs keep today's behavior.
    """
    if not isinstance(cfg, dict) or "ai_watch_arm_sources" not in cfg:
        return None
    raw = cfg.get("ai_watch_arm_sources")
    if raw is None:
        return None
    if isinstance(raw, (list, tuple, set, frozenset)):
        parts = [str(x).strip().lower() for x in raw if str(x).strip()]
    else:
        text = str(raw).strip().lower()
        if not text or text in ("*", "all", "any"):
            return None
        parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return None
    return frozenset(parts)


def arm_source_allows_buy(record: dict | None, cfg: dict | None) -> tuple[bool, str]:
    """True when this record's source may open a buy.

    Seats are untouched. A restrictive list blocks a missing source and
    any name that is not an allowed token (exact, or a ``_`` / ``-``
    piece such as ``soft_seed_momentum``).
    """
    allow = arm_sources_allow(cfg)
    if allow is None:
        return True, "sources_open"
    src = str((record or {}).get("source") or "").strip().lower()
    if not src:
        return False, "source_blocked"
    if src in allow:
        return True, "source_ok"
    pieces = {p for p in src.replace("-", "_").split("_") if p}
    if pieces & allow:
        return True, "source_ok"
    return False, "source_blocked"


def should_arm_buy(
    record: dict,
    *,
    ask: float,
    bid: float | None,
    cfg: dict,
    now: float | None = None,
) -> tuple[bool, str]:
    """Whether a watch record may auto-arm a paper buy at *ask*.

    Returns ``(True, "zone")`` when armable, else ``(False, reason)`` where
    reason is one of: ``not_watching``, ``no_structure``, ``hard_no``,
    ``wait_setup``, ``spread``, ``above_zone``, ``below_zone``, ``reward_risk``.
    """
    if not isinstance(record, dict):
        return False, "not_watching"
    if _is_wash_look(record):
        return False, "look_wash"
    status = str(record.get("status") or "").lower().strip()
    if status not in _ARMABLE_STATUSES:
        return False, "not_watching"
    # Never arm when the decision src is explicitly stale_tape / none.
    # Full _row_tape_stale (fail-closed on missing age) stays in the poller /
    # refresh path — unit tests often call should_arm_buy without a clock.
    # Promote false stale labels first so stream+young cannot return
    # stale_quote / tape_only (Class C CAPR/SDOT). Then paint-trust the
    # young field over a lagging last_ask_ts — same order as
    # apply_tape_blocker — so poller arm cannot disagree with overlay.
    promote_stream_src_if_print_fresh(record, cfg, now=now)
    _paint_trust_young_stream_field(record, cfg, now=now)
    _src_arm = str(
        record.get("last_ask_src") or record.get("price_src") or ""
    ).strip().lower()
    if _src_arm in ("stale_tape", "none"):
        return False, "stale_quote"
    # Engine heartbeat. The indicators every arm reads come from the signal
    # engine; if it stops writing, they freeze. 2026-09-23 13:51-14:15 the
    # engine sat on a lock and VKTX armed at 14:03 on 13:51 indicators.
    # Missing heartbeat refuses too — no engine, no indicator, no entry.
    try:
        _eng_max = float(cfg.get("ai_watch_engine_stale_max_sec", 0) or 0)
    except (TypeError, ValueError):
        _eng_max = 0.0
    if _eng_max > 0:
        _eng_age = engine_heartbeat_age(now=now)
        if _eng_age is None or _eng_age > _eng_max:
            return False, "engine_stale"
    _gate_sym = str(record.get("symbol") or "").upper()
    # Spread gate: the whole trading cost is the spread (exec_report,
    # 2026-09-23: fills at the touch, 0 excess). A <= 0.20% gate cut the
    # one-arm cost 0.112% -> 0.071% and net -0.062% -> -0.011%, both halves.
    _sp_max = _spread_gate_max(record.get("source"), cfg)
    if _sp_max > 0 and _gate_sym:
        _sp = sip_spread_pct(_gate_sym, now=now)
        if _sp is None:
            return False, "spread_unknown"
        if _sp > _sp_max:
            record["block_detail"] = f"SIP spread {_sp:.2f}% > {_sp_max:g}%"
            return False, "spread_wide"
    # Gap-down exclusion: names that opened > pct under yesterday's close.
    try:
        _gap_max = float(cfg.get("ai_watch_gap_down_block_pct", 0) or 0)
    except (TypeError, ValueError):
        _gap_max = 0.0
    if _gap_max > 0 and _gate_sym:
        _gap = open_gap_pct(_gate_sym, now=now)
        if _gap is None:
            return False, "gap_unknown"
        if _gap < -_gap_max:
            record["block_detail"] = f"gapped {_gap:+.1f}%"
            return False, "gapped_down"

    structure = record.get("structure")
    if not isinstance(structure, dict):
        return False, "no_structure"

    decision = str(structure.get("decision") or "").upper().strip()
    wait_kind = structure.get("wait_kind")
    wait_kind_s = (
        str(wait_kind).lower().strip() if wait_kind is not None else ""
    )

    if wait_kind_s == "hard_no":
        return False, "hard_no"
    if wait_kind_s == "wait_setup":
        return False, "wait_setup"

    # Arm only BUY (with levels) or WAIT + wait_for_zone
    is_buy = decision == "BUY"
    is_zone_wait = wait_kind_s == "wait_for_zone" or (
        decision == "WAIT" and wait_kind_s == "wait_for_zone"
    )
    if decision == "WAIT" and wait_kind_s and wait_kind_s != "wait_for_zone":
        return False, "wait_setup"
    if not is_buy and not is_zone_wait:
        # WAIT without explicit wait_for_zone (or other decisions) → no auto-buy
        if decision == "WAIT":
            return False, "wait_setup"
        return False, "no_structure"

    levels = _structure_levels(structure)
    if levels is None:
        return False, "no_structure"
    entry_low, entry_high, _stop, _target, rr = levels

    cfg = cfg if isinstance(cfg, dict) else {}

    # Product veto (docs/PROFIT_REDESIGN.md). Default observe blocks new
    # arms. Omitted desk_product on a partial cfg is scalp_legacy so unit
    # tests keep their old geometry.
    _desk_product_mod = None
    try:
        import desk_product as _desk_product_mod
        _prod_block = _desk_product_mod.arm_block_reason(cfg)
    except Exception:
        _prod_block = None
        _desk_product_mod = None
    if _prod_block:
        return False, _prod_block
    if (_desk_product_mod is not None
            and _desk_product_mod.product(cfg) == _desk_product_mod.H4_SWING
            and _desk_product_mod.h4_paper(cfg)):
        try:
            import desk_h4 as _desk_h4
            return _desk_h4.should_arm(record, ask=ask, bid=bid, cfg=cfg)
        except Exception:
            return False, "h4_arm_error"

    # Last-hour hold paper test: suppress daytime arms; in-window, skip
    # heat/RSI and buy last — matching gate 2's --arm-at-admit. Names
    # admitted before 14:00 stay on the book but do not get a slot.
    import desk_late_hold as _lh
    t_arm = float(now if now is not None else time.time())
    late_why = _lh.arm_why(cfg, t_arm, record.get("admit_ts"))
    if late_why:
        return False, late_why
    src_ok, src_why = arm_source_allows_buy(record, cfg)
    if not src_ok:
        return False, src_why
    if _lh.enabled(cfg) and arm_at_last(cfg):
        return True, "last_late_hold"

    # When the double-bottom detector finds no shelf, ensure_offset_zone_if_needed
    # silently substitutes a percentage band: a 5% stop, a target 2-6% below the
    # last print, no structural level anywhere in it. That is a different trade
    # from the one the zone mode asks for, and it is the regime the 1,220
    # symbol-day replay measured at -0.0027R with 77% of exits at the 15:50
    # clock. GLXY — the only live trade on record — was one of these.
    #
    # Refused rather than merely labelled: nothing downstream distinguishes
    # them, so an unlabelled offset fill lands in outcomes.jsonl next to a
    # double-bottom fill and the scorecard averages two different strategies.
    # Flip ai_watch_require_db_zone to False to allow them back.
    # ``pullback_band`` is armable, ``offset`` is not, and the difference is the
    # whole point. The offset band above is a fixed percentage off the last
    # print — the same 2%/5% for a name that moves 0.4% a day and one that moves
    # 20% — which is what the replay measured at -0.0027R. A pullback band is
    # sized from the symbol's own measured dip distribution, so its depth is a
    # statement about that name rather than a constant.
    #
    # It carries no replay of its own yet, so it is enabled on the condition
    # that made the original refusal necessary being removed: fills now record
    # zone_kind (see _entry_zone_kind), so band trades and double-bottom trades
    # can be scored apart instead of averaging into one meaningless number.
    # Drop "pullback_band" from ai_watch_armable_zone_kinds to go back.
    last_mode = arm_at_last(cfg)
    armable = cfg.get("ai_watch_armable_zone_kinds")
    if not isinstance(armable, (list, tuple)) or not armable:
        armable = ("double_bottom", "pullback_band", "at_last")
    armable = {str(k).lower().strip() for k in armable}
    # Offset (fixed % under last) is not a zone. It was −0.0027R over 1,220
    # symbol-days. Refuse it in every zone mode unless the operator puts
    # "offset" on the armable list or flips require_db_zone off.
    if not last_mode:
        zk = str(structure.get("zone_kind") or "").lower().strip()
        allowed = set(armable)
        if not bool(cfg.get("ai_watch_require_db_zone", True)):
            allowed.add("offset")
        # Missing zone_kind is a model / test structure, not the offset fallback.
        if zk and zk not in allowed:
            return False, "offset_zone"
    try:
        min_rr = float(cfg.get("ai_min_reward_risk", 0) or 0)
    except (TypeError, ValueError):
        min_rr = 0.0
    if min_rr > 0 and rr + 1e-12 < min_rr:
        return False, "reward_risk"

    # Exhaustion first so (a) missing %R is named correctly, and (b) the soft
    # sell_signal veto below can reference exh_why without UnboundLocalError.
    exh_ok, exh_why = exhaustion_allows_buy(record, cfg, now=t_arm)

    # CM RSI-2 band + turn (checked when ai_watch_arm_require_cm_rsi is active)
    rsi_ok, rsi_why = cm_rsi_allows_buy(record, cfg)
    if not rsi_ok:
        return False, rsi_why

    # Indicators: optional timing filter. Default off — book symbols often have
    # no engine indicator map, so requiring cm_ok/pctr_ok/cm_rsi_rising blocked
    # every in-zone arm. When present and enabled, still refuse sell_signal and
    # missing named flags. When disabled, in-zone price alone can arm.
    if bool(cfg.get("ai_watch_arm_require_indicators", False)):
        sig = record.get("indicator")
        if not isinstance(sig, dict):
            return False, "no_indicators"
        if sig.get("sell_signal"):
            return False, "sell_signal"
        # Gate on NAMED conditions, not a count. proximity_pct is just "how
        # many of the three hold", so requiring 100 silently demanded MACD as
        # well — and MACD is the laggard here by design: the strategy's own
        # buy_signal docstring notes that by the time it crosses, CM RSI-2 has
        # usually already left the <40 zone. CM RSI-2 (cm_ok) and %R exhaustion
        # (pctr_ok) are the operator's actual buy signals; MACD is ignored.
        required = cfg.get("ai_watch_arm_require")
        if not isinstance(required, (list, tuple)) or not required:
            required = ("cm_ok", "pctr_ok")
        missing = [k for k in required if not sig.get(k)]
        if missing:
            return False, "indicators_faded"

        # Optional count floor on top, off by default (0) since the named
        # flags above are the real test.
        try:
            arm_min = float(cfg.get("ai_watch_arm_min_proximity", 0) or 0)
        except (TypeError, ValueError):
            arm_min = 0.0
        if arm_min > 0:
            try:
                prox = float(sig.get("proximity_pct") or 0)
            except (TypeError, ValueError):
                prox = 0.0
            if prox < arm_min:
                return False, "indicators_faded"
    elif (
        not last_mode
        and (not bool(cfg.get("ai_watch_exhaustion_rules", True))
             or exh_why == "no_exhaustion_fallback")
    ):
        # Soft sell-signal veto when the engine has published one, even if the
        # full arm triple is not required.
        #
        # Skipped under the exhaustion rules, and that is the point of them.
        # sell_signal is NOT a %R signal: strategy_three_indicator composes it
        # from a MACD bearish cross OR CM RSI-2 rolling over OR %R rolling over
        # (exit_signals defaults to cm+rte). Leaving it in place meant MACD and
        # CM RSI-2 were still vetoing entries the operator had specified should
        # depend on exhaustion alone.
        # Arm-at-last Phase 0 also skips this — RSTOP is the exit.
        sig = record.get("indicator")
        if isinstance(sig, dict) and sig.get("sell_signal"):
            return False, "sell_signal"

    try:
        pad = float(cfg.get("ai_entry_zone_pad_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        pad = 0.0

    try:
        a = float(ask)
    except (TypeError, ValueError):
        return False, "below_zone"

    # Risk per share must be a real risk unit before anything sizes off it.
    #
    # A double-bottom zone spans [S*0.9975, S*1.0125] against a stop fixed at
    # S*0.995 — floor is always exactly S, because find_double_bottom_support
    # already returns min(low_a, low_b). So risk per share is 0.25% of price at
    # the bottom of the band and 1.73% at the top: a 6.9x swing decided purely
    # by where the fill lands. At the tight end size_by_risk asks for ~400% of
    # equity for a 1% risk, the notional cap chops it to 25%, and the trade's
    # real risk is ~1/16th of intended while still being booked as "1R".
    #
    # A 0.25% stop is also below the noise floor of these names (2-11% observed
    # intraday range on the 10 replayed): it is not a thesis level, it is a
    # rounding error that the spread alone can trip.
    #
    # build_offset_zone_structure solved its own version of this by keying the
    # stop to the price paid. That is wrong for a shelf — the whole point of a
    # structural stop is that it sits under real support — so the fix here is
    # to decline the fill instead of moving the stop.
    try:
        min_stop_pct = float(cfg.get("ai_watch_min_stop_pct", 0.5) or 0.0)
    except (TypeError, ValueError):
        min_stop_pct = 0.5
    if min_stop_pct > 0 and a > 0 and _stop > 0 and a > _stop:
        risk_pct_of_px = 100.0 * (a - _stop) / a
        if risk_pct_of_px < min_stop_pct:
            return False, "stop_too_tight"

    # Believe the reading before thresholding it. An RVOL the feed cannot
    # have produced (the 2026-09-03 audit found nineteen arms taken between
    # 26.8x and 1144.6x, clustered near 1000 — an arithmetic fault, not a
    # tape) is not a hot name, and sizing a trade off it is a calculation on
    # data known to be wrong. Refuse instead: this is a credibility bound,
    # and it deliberately takes no view on merely extreme readings.
    try:
        sane_rvol = float(cfg.get("ai_watch_arm_rvol_sane_max", 0.0) or 0.0)
    except (TypeError, ValueError):
        sane_rvol = 0.0
    if sane_rvol > 0:
        rv_sane = _arm_rvol(record)
        if rv_sane is not None and rv_sane > sane_rvol:
            return False, "rvol_implausible"

    # Arm RVOL is separate from admission. Default 0: the ratchet owns the
    # trade once price is in the zone. Set ai_watch_arm_min_rvol to restore
    # the 08-14 veto (WEN/RUM 0.4–0.6x).
    try:
        min_rvol = float(cfg.get("ai_watch_arm_min_rvol", 0.0) or 0.0)
    except (TypeError, ValueError):
        min_rvol = 0.0
    if min_rvol > 0:
        rv = _arm_rvol(record)
        if rv is not None and rv + 1e-12 < min_rvol:
            return False, "thin_rvol"

    try:
        zone_win = float(cfg.get("ai_watch_zone_exh_window_sec", 0.0) or 0.0)
    except (TypeError, ValueError):
        zone_win = 0.0
    in_zone = ask_triggers_zone(
        a, entry_low, entry_high,
        pad_pct=pad, stop=_stop,
        max_below_r=arm_below_max_r(cfg),
        arm_below=bool(cfg.get("ai_watch_arm_below_zone", True)),
    )
    if not exh_ok:
        # Optional: cooling EXH may still fill. Last-mode does not also
        # demand the leftover pullback band. Zone mode still does.
        # Never waive an explicit falling-EXH refuse when gaining-EXH is
        # required — that knob exists to stop late chases into a rollover.
        _exh_hard = str(exh_why or "") in (
            "exh_falling", "exh_not_rising", "exh_rising_required",
        )
        fade_ok = (
            (not _exh_hard)
            and bool(cfg.get("ai_watch_in_zone_ignore_fade", False))
            and (
                str(exh_why).startswith("not_rising")
                or str(exh_why) in ("exh_not_rising",)
            )
        )
        if last_mode and fade_ok:
            exh_ok, exh_why = True, "in_zone_fade_ok"
        elif fade_ok and in_zone:
            exh_ok, exh_why = True, "in_zone_fade_ok"
        elif zone_win <= 0:
            return False, exh_why
        # zone_win > 0: stay on the name; zone entry starts a wait below.

    # Soft overbought / late-heat: already in the OB band AND RSI is
    # already near the hard cap. Runs after the EXH allow so we do not
    # mask a real fade (not_rising_overbought) with late_heat, and after
    # the RSI hard-max so rsi_extended still wins above 60.
    if exh_ok:
        # Room below the day's high (off at 0). Runs after the cross check so
        # the mid-rise latch keeps seeing every reading while this refuses.
        _hod_why = room_below_hod_refusal(record, _gate_sym, ask, cfg, now=now)
        if _hod_why:
            return False, _hod_why
        late = late_heat_blocks_buy(record, cfg)
        if late:
            return False, late
        # Heating-band chase (GTLB): soft OB needs overbought, so a name
        # still in the heat band with mid/high RSI used to arm. Peak RSI
        # across confirm ticks is noted by the poller before this runs on
        # later ticks; current RSI is always considered too.
        mistimed = mistimed_heat_blocks_buy(record, cfg, exh_why=exh_why)
        if mistimed:
            return False, mistimed

    # MACD direction veto: refuse a crossed-down gap. Fail-open on missing
    # MACD so it cannot starve opens the way macd_src_unknown once did.
    if bool(cfg.get("ai_watch_macd_block_bearish", False)):
        bear_why = macd_bearish_blocks_buy(
            record, cfg, fail_open_unknown=True)
        if bear_why:
            return False, bear_why

    # Cheap pullback/offset + overbought is the HCTI/BYSI dump: $2 spike,
    # 20% of equity, then −1R in under a minute. Last-mode used to skip
    # this and buy the same blow-off at the tape.
    try:
        cheap_px = float(cfg.get("ai_watch_cheap_price", 5.0) or 0.0)
    except (TypeError, ValueError):
        cheap_px = 5.0
    zk = str(structure.get("zone_kind") or "").lower().strip()
    if (
        cheap_px > 0
        and a < cheap_px
        and zk in ("pullback_band", "offset", "at_last", "")
        and is_overbought(record, cfg) is True
    ):
        return False, "cheap_ob_band"
    day_chg = _arm_day_change(record)
    if (
        cheap_px > 0
        and a < cheap_px
        and day_chg is not None
        and day_chg >= _CHEAP_BLOWOFF_PCT
    ):
        return False, "extended_cheap"

    if last_mode:
        # Last is the entry. Structure only supplies stop/target for R.
        if exh_ok:
            pace_ok, pace_why = _rvol_pace_gate(record, cfg, now)
            if not pace_ok:
                return False, pace_why
            return True, f"last_{exh_why}"
        return False, exh_why

    t_now = float(now if now is not None else time.time())
    if not in_zone:
        if isinstance(record, dict):
            record.pop("zone_touch_ts", None)
        frac = max(0.0, pad) / 100.0
        high_bound = max(entry_low, entry_high) * (1.0 + frac)
        if a > high_bound:
            return False, "above_zone"
        return False, "below_zone"

    # In / below the band. Stamp first touch so EXH can arm on a later tick.
    if zone_win > 0:
        try:
            touch = float(record.get("zone_touch_ts") or 0.0)
        except (TypeError, ValueError):
            touch = 0.0
        if touch <= 0:
            touch = t_now
            record["zone_touch_ts"] = touch
        if exh_ok:
            return True, f"zone_{exh_why}"
        if (t_now - touch) <= zone_win + 1e-9:
            return False, "wait_exh"
        return False, exh_why

    if exh_ok:
        return True, f"zone_{exh_why}"
    return False, exh_why


def _prune_structure_budget(now: float) -> None:
    cutoff = float(now) - _STRUCTURE_BUDGET_WINDOW_SEC
    while _structure_call_ts and _structure_call_ts[0] < cutoff:
        _structure_call_ts.pop(0)


def structure_calls_remaining(cfg: dict, now: float | None = None) -> int:
    """How many structure LLM calls remain in the rolling 1h window."""
    t = float(now if now is not None else time.time())
    _prune_structure_budget(t)
    try:
        cap = int(cfg.get("ai_max_structure_calls_per_hour", 12) or 0)
    except (TypeError, ValueError):
        cap = 12
    if cap <= 0:
        return 0
    return max(0, cap - len(_structure_call_ts))


def _record_structure_call(now: float) -> None:
    _prune_structure_budget(now)
    _structure_call_ts.append(float(now))


def _structure_stale(record: dict, cfg: dict, now: float) -> bool:
    """True when structure is missing or older than TTL."""
    structure = record.get("structure")
    if not isinstance(structure, dict):
        return True
    try:
        ts = float(record.get("structure_ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts <= 0:
        return True
    try:
        ttl = float(cfg.get("ai_structure_ttl_sec", 5400) or 5400)
    except (TypeError, ValueError):
        ttl = 5400.0
    if ttl <= 0:
        return False
    return (float(now) - ts) > ttl


def _blocker_for_gate(why: str) -> str:
    """Map a pre_entry_gate rejection string to a UI blocker code.

    The gate returns detail-rich strings (``spread_pct_2.10>1``); the Blocker
    column wants a stable code so the operator can tell "risk cap" from
    "too wide" at a glance.
    """
    w = str(why or "").lower()
    if w.startswith("daily_loss_limit_r"):
        return "daily_loss_limit"
    if w.startswith("pdt_"):
        return "pdt"
    if w.startswith("open_risk_pct"):
        return "open_risk_cap"
    if w.startswith("spread_pct") or w in ("crossed_quote", "bad_mid"):
        return "spread"
    if w.startswith("dollar_vol"):
        return "dollar_volume"
    if w == "already_managed":
        return "already_managed"
    if w.startswith("above_max_price"):
        return "above_max_price"
    if w in ("no_ask", "no_equity", "invalid_symbol"):
        return w
    return "risk_gate"


# (mtime, size) -> last exit ts, last dead_trade ts, last outcome row.
# outcomes.jsonl only ever grows, so a stat is enough to know the parse is
# still valid.
_exit_cache: tuple[
    tuple[float, int] | None,
    dict[str, float],
    dict[str, float],
    dict[str, dict],
] = (
    None, {}, {}, {},
)


def _exit_maps() -> tuple[dict[str, float], dict[str, float]]:
    """symbol -> last exit ts, and symbol -> last dead_trade ts."""
    ts_map, dead_map, _rows = _exit_maps_full()
    return ts_map, dead_map


def _exit_maps_full() -> tuple[dict[str, float], dict[str, float], dict[str, dict]]:
    global _exit_cache
    try:
        import ai_positions as cp
        st = cp.OUTCOMES_PATH.stat()
        key = (st.st_mtime, st.st_size)
    except Exception:
        return {}, {}, {}
    cached_key, cached, cached_dead, cached_rows = _exit_cache
    if cached_key == key:
        return cached, cached_dead, cached_rows
    out: dict[str, float] = {}
    dead: dict[str, float] = {}
    rows: dict[str, dict] = {}
    try:
        text = cp.OUTCOMES_PATH.read_text(encoding="utf-8")
    except Exception:
        return {}, {}, {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        try:
            ts = float(row.get("exit_time") or row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts and ts > out.get(sym, 0.0):
            out[sym] = ts
            rec: dict = {"ts": ts}
            try:
                rec["realized_r"] = float(row.get("realized_r_multiple"))
            except (TypeError, ValueError):
                rec["realized_r"] = None
            try:
                rec["mfe_r"] = float(row.get("mfe_r"))
            except (TypeError, ValueError):
                rec["mfe_r"] = None
            rec["close_reason"] = str(row.get("close_reason") or "")
            rows[sym] = rec
        reason = str(row.get("close_reason") or "").strip().lower()
        if reason == "dead_trade" and ts and ts > dead.get(sym, 0.0):
            dead[sym] = ts
    _exit_cache = (key, out, dead, rows)
    return out, dead, rows


def _exit_ts_map() -> dict[str, float]:
    """symbol -> most recent exit timestamp, parsed at most once per write."""
    return _exit_maps()[0]


def _recent_exit_ts(symbol: str) -> float | None:
    """When this symbol last closed (None if never)."""
    return _exit_ts_map().get(str(symbol or "").upper().strip())


def _recent_dead_exit_ts(symbol: str) -> float | None:
    """When this symbol last closed as a dead trade (None if never)."""
    return _exit_maps()[1].get(str(symbol or "").upper().strip())


def _last_exit_row(symbol: str) -> dict | None:
    """Most recent outcome row for *symbol*, or None."""
    rows = _exit_maps_full()[2]
    return rows.get(str(symbol or "").upper().strip())


def _same_et_day(ts: float, now: float) -> bool:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        return (
            datetime.fromtimestamp(float(now), tz=et).date()
            == datetime.fromtimestamp(float(ts), tz=et).date()
        )
    except Exception:
        return False


def _dead_reentry_blocked(symbol: str, now: float, cfg: dict) -> bool:
    """True when today's last exit was a loser that never ran 0.5R.

    Off unless ``ai_dead_reentry_block`` is set. Same-day re-entry is
    allowed by default; ``ai_reentry_cooldown_sec`` still spaces fills.
    When the flag is on, green exits and names that printed MFE ≥
    ``ai_reentry_min_mfe_r`` may still re-arm (LFS).
    """
    if not bool(cfg.get("ai_dead_reentry_block", False)):
        return False
    row = _last_exit_row(symbol)
    if not row or not row.get("ts"):
        # Fall back to the old dead-only clock if outcomes are missing.
        ts = _recent_dead_exit_ts(symbol)
        if not ts:
            return False
        return _same_et_day(ts, now)
    if not _same_et_day(float(row["ts"]), now):
        return False
    try:
        realized = row.get("realized_r")
        if realized is not None and float(realized) > 0:
            return False
    except (TypeError, ValueError):
        pass
    try:
        need = float(cfg.get("ai_reentry_min_mfe_r", 0.5) or 0.5)
    except (TypeError, ValueError):
        need = 0.5
    try:
        mfe = row.get("mfe_r")
        if mfe is not None and float(mfe) + 1e-12 >= need:
            return False
    except (TypeError, ValueError):
        pass
    return True


def _decision_for_place(
    structure: dict,
    *,
    ask: float | None = None,
    cfg: dict | None = None,
    late_hold: bool = False,
) -> dict[str, Any]:
    """Build a place_scaled_entry decision from stored structure levels.

    Zone-wait records store decision=WAIT; placement needs BUY + levels.

    For a synthetic *offset* zone the stop and target are re-derived from *ask*
    so ``ai_watch_synth_stop_pct`` means "this far below what I paid".

    For a *double_bottom* zone the stop stays under support (structure). Only
    target is re-based off fill so R stays honest if we fill near the top of
    the band.

    A model zone is left alone: those levels come from real structure (support,
    prior day's low), not a percentage, and must not be second-guessed here.
    """
    d = dict(structure)
    d["decision"] = "BUY"
    d["wait_kind"] = None

    cfg = cfg if isinstance(cfg, dict) else {}
    if late_hold:
        import desk_late_hold as _lh
        cfg = dict(cfg)
        cfg["ai_watch_synth_stop_pct"] = _lh.stop_pct(cfg)
    if arm_at_last(cfg):
        d["synthetic"] = True
        d["zone_kind"] = "at_last"
    if not d.get("synthetic"):
        return d
    try:
        px = float(ask or 0)
    except (TypeError, ValueError):
        px = 0.0
    if px <= 0:
        return d
    try:
        rr = max(0.25, float(cfg.get("ai_watch_synth_rr", 0.6) or 0.6))
    except (TypeError, ValueError):
        rr = 0.6

    zone_kind = str(d.get("zone_kind") or "").lower()
    if arm_at_last(cfg):
        d["zone_kind"] = "at_last"
        zone_kind = "at_last"
    if zone_kind == "double_bottom":
        # Keep structural stop under the shelf; size risk from fill → stop.
        try:
            stop = float(d.get("stop_price") or 0)
        except (TypeError, ValueError):
            stop = 0.0
        if stop <= 0 or stop >= px:
            try:
                support = float(d.get("support") or 0)
            except (TypeError, ValueError):
                support = 0.0
            try:
                stop_below = max(0.0, float(
                    cfg.get("ai_watch_db_stop_below_pct", 0.5) or 0.5)) / 100.0
            except (TypeError, ValueError):
                stop_below = 0.005
            if support > 0:
                stop = support * (1.0 - stop_below)
        if stop > 0 and stop < px:
            d["stop_price"] = round(
                stop, 4 if px < 1 else 3 if px < 100 else 2)
            d["target_1"] = round(
                px + rr * (px - stop),
                4 if px < 1 else 3 if px < 100 else 2)
            d["reward_risk"] = round(rr, 2)
    else:
        try:
            stop_pct = max(0.0, float(
                cfg.get("ai_watch_synth_stop_pct", 5.0) or 5.0)) / 100.0
        except (TypeError, ValueError):
            stop_pct = 0.05
        stop = px * (1.0 - stop_pct)
        if stop <= 0 or stop >= px:
            return d
        d["stop_price"] = round(stop, 4 if px < 1 else 3 if px < 100 else 2)
        d["target_1"] = round(
            px + rr * (px - stop), 4 if px < 1 else 3 if px < 100 else 2)
        d["reward_risk"] = round(rr, 2)

    # Ensure sell-strategy fields survive placement recompute.
    if d.get("scale_out_pct") is None:
        d["scale_out_pct"] = _opt_float(
            cfg.get("ai_watch_synth_scale_out_pct"), 50.0)
    if d.get("trail_pct") is None:
        d["trail_pct"] = _opt_float(cfg.get("ai_watch_synth_trail_pct"), 2.5)
    d.setdefault("strategy", "day_scalp_v0")
    if late_hold:
        import desk_late_hold as _lh
        d = _lh.stamp_decision(d)
    return d


def ensure_structure(
    record: dict,
    cfg: dict,
    now: float,
) -> dict:
    """Resolve / refresh entry structure via ``ai_positions.evaluate_entry``.

    Mutates *record* in place: sets ``structure``, ``structure_ts``, and may
    set status to ``invalidated`` on ``hard_no``. Returns the (possibly
    empty) event dict from logging, or a skip event if budget/quote fails.
    Callers must enforce the structure call budget before invoking.
    """
    import ai_positions as cp
    import ai_trading as gt

    if not isinstance(record, dict):
        return {"kind": "structure_skip", "reason": "bad_record"}

    sym = str(record.get("symbol") or "").upper().strip()
    if not sym:
        return {"kind": "structure_skip", "reason": "no_symbol"}

    ask = record.get("last_ask")
    if ask is None or float(ask or 0) <= 0:
        try:
            ask = gt._latest_ask(sym)
        except Exception:
            ask = None
    try:
        ask_f = float(ask) if ask is not None else 0.0
    except (TypeError, ValueError):
        ask_f = 0.0
    if ask_f <= 0:
        return {
            "kind": "structure_skip",
            "symbol": sym,
            "reason": "no_ask",
        }

    acct = gt.get_account()
    equity = 0.0
    if isinstance(acct, dict) and acct.get("ok"):
        try:
            equity = float(acct.get("equity") or 0)
        except (TypeError, ValueError):
            equity = 0.0
    if equity <= 0:
        equity = 100_000.0  # paper default if account unavailable

    try:
        risk_pct = float(cfg.get("ai_risk_pct", 1.0) or 1.0)
    except (TypeError, ValueError):
        risk_pct = 1.0

    reason = str(record.get("reason") or "")
    backend = str(cfg.get("ai_entry_backend") or cfg.get("ai_backend") or "cli")
    model = cfg.get("ai_entry_model") or cfg.get("ai_model")
    cli_bin = cfg.get("ai_cli_bin") or cfg.get("cli_bin")

    _record_structure_call(now)
    try:
        decision = cp.evaluate_entry(
            sym,
            ask_f,
            equity,
            reason=reason,
            risk_pct=risk_pct,
            model=str(model) if model else "",
            cli_bin=str(cli_bin) if cli_bin else None,
            backend=backend,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "kind": "structure_fail",
            "symbol": sym,
            "reason": str(e)[:200],
        }

    if decision is None:
        return {
            "kind": "structure_fail",
            "symbol": sym,
            "reason": "evaluate_entry_none",
        }

    try:
        normalized = cp.normalize_entry_decision(decision) or decision
    except Exception:
        normalized = decision
    if not isinstance(normalized, dict):
        return {
            "kind": "structure_fail",
            "symbol": sym,
            "reason": "bad_decision",
        }

    record["structure"] = normalized
    record["structure_ts"] = float(now)
    record["last_ask"] = ask_f

    wait_kind = normalized.get("wait_kind")
    wait_kind_s = (
        str(wait_kind).lower().strip() if wait_kind is not None else ""
    )
    if wait_kind_s == "hard_no":
        record["status"] = "invalidated"

    event: dict[str, Any]
    if bool(cfg.get("ai_persist_entry_decisions", True)):
        try:
            event = cp.log_entry_decision(
                sym, normalized, reason="watch_structure")
        except Exception:
            event = {
                "kind": "entry_decision",
                "symbol": sym,
                "decision": normalized.get("decision"),
                "wait_kind": normalized.get("wait_kind"),
            }
    else:
        event = {
            "kind": "structure_ok",
            "symbol": sym,
            "decision": normalized.get("decision"),
            "wait_kind": normalized.get("wait_kind"),
        }

    dec = str(normalized.get("decision") or "").upper()
    if dec == "BUY":
        event = dict(event)
        event.setdefault("kind", "structure_buy")
    elif wait_kind_s:
        event = dict(event)
        if event.get("kind") in (None, "entry_decision"):
            pass
        event.setdefault("structure_kind", f"structure_{wait_kind_s}")
    return event


# Above this, a relative-volume reading is a broken ratio rather than a hot
# name — the historical log carries values into the tens of thousands, and the
# two sides of that ratio have to come off one feed at one bar size to mean
# anything. Treated as unknown rather than clamped: a wrong number that looks
# plausible is worse than an absent one, and clamping 80,000x to the ceiling
# would hand it the seat.
_RVOL_SANE_MAX = 100.0


def _rank_rvol(rec: dict, live_lookup=None) -> float | None:
    """Best trustworthy relative volume for *rec*, or None.

    Live first: a name can cool off between admission and the poll that would
    buy it, so the admit-time stamp is the fallback, not the answer.
    """
    lookup = live_lookup if live_lookup is not None else _desk_rvol
    sym = str(rec.get("symbol") or "").upper().strip()
    vals = []
    if sym:
        try:
            vals.append(_f_or_none(lookup(sym)))
        except Exception:  # noqa: BLE001
            vals.append(None)
    vals.append(_f_or_none(rec.get("rvol")))
    vals.append(_f_or_none(rec.get("admit_rvol")))
    for v in vals:
        if v is None:
            continue
        if v <= 0 or v > _RVOL_SANE_MAX:
            continue
        return float(v)
    return None


# Monotonic poll counter. Only _arm_streak reads it, to tell "the previous
# poll" from "some earlier poll". Process-local on purpose — see _arm_streak.
_POLL_SEQ = 0



def _confirm_slip_limits(cfg: dict | None) -> tuple[float, float]:
    """Max ask move between streak-pass and place: (pct, absolute $)."""
    cfg = cfg or {}
    try:
        pct = max(0.0, float(cfg.get("ai_entry_confirm_max_slip_pct", 1.0) or 0.0))
    except (TypeError, ValueError):
        pct = 1.0
    try:
        px = max(0.0, float(cfg.get("ai_entry_confirm_max_slip_px", 0.10) or 0.0))
    except (TypeError, ValueError):
        px = 0.10
    return pct, px


def _confirm_slip_ok(
    confirm_ask: float | None,
    send_ask: float | None,
    cfg: dict | None = None,
) -> tuple[bool, str]:
    """True when send ask is still the same tape state as the streak pass.

    FRVO 2026-09-03: streak 2 at ask 17.52, then stream_required / ask 18.32
    before submit — confirm and send were not one atomic quote. Refuse rather
    than chase the jump.
    """
    try:
        c = float(confirm_ask or 0)
        s = float(send_ask or 0)
    except (TypeError, ValueError):
        return False, "bad_ask"
    if c <= 0 or s <= 0:
        return False, "bad_ask"
    pct_lim, px_lim = _confirm_slip_limits(cfg)
    jump = abs(s - c)
    jump_pct = 100.0 * jump / c
    # Either threshold may bind; 0 disables that leg.
    if px_lim > 0 and jump > px_lim + 1e-9:
        return False, f"jump_${jump:.4f}>${px_lim:.2f}"
    if pct_lim > 0 and jump_pct > pct_lim + 1e-9:
        return False, f"jump_{jump_pct:.2f}%>{pct_lim:.2f}%"
    return True, ""


def _arm_confirm_ticks(cfg: dict | None) -> int:
    """How many consecutive polls must agree before a buy is placed."""
    try:
        return max(1, int((cfg or {}).get("ai_watch_arm_confirm_ticks", 1) or 1))
    except (TypeError, ValueError):
        return 1


def _arm_streak(rec: dict, ok: bool, *, seq: int | None = None) -> int:
    """Count consecutive arm-YES verdicts on *rec*; any NO resets it.

    Mirrors the exit's confirmation streak. Kept on the record so it survives
    the poll but not a restart, which is the right lifetime: after a restart
    the desk should re-earn its evidence rather than act on a count it cannot
    see the readings behind.

    CONSECUTIVE IS ENFORCED BY POLL NUMBER, not by remembering to reset on
    every refusal. A count that only advances when the previous YES came from
    the immediately preceding poll cannot be fooled by the paths that leave
    the loop early — stale_quote, stream_required, tape_only, or a batch-stage
    NO, none of which called the reset. Before this, IREN on 2026-09-03 logged
    twenty-one arm-YES verdicts and `streak=1` on every one, five of them on
    consecutive polls: the 2s ``sync_watch_from_source_panels`` rebuild dropped
    ``arm_streak`` (it was not in the carry-over list), so the counter restarted
    every ~13s and ai_watch_arm_confirm_ticks=2 could only be satisfied by a
    name the sync happened to skip. That made arming a race, not a
    confirmation, and it is why five of the eighteen qualifying runs that day
    opened while thirteen did not.

    A restart resets it by construction: the module-level sequence starts over,
    so the stored poll number can no longer be "the previous one".
    """
    if not isinstance(rec, dict):
        return 1 if ok else 0
    if not ok:
        rec.pop("arm_streak", None)
        rec.pop("arm_streak_poll", None)
        rec.pop("arm_confirm_rsi_max", None)
        return 0
    n = int(rec.get("arm_streak") or 0)
    if seq is None:
        n += 1
    else:
        try:
            prev = int(rec.get("arm_streak_poll"))
        except (TypeError, ValueError):
            prev = None
        # Streak restart also drops the confirm-window RSI peak — a new
        # evidence run must not inherit a hot print from a broken streak.
        if not (prev is not None and prev == int(seq) - 1 and n > 0):
            rec.pop("arm_confirm_rsi_max", None)
        n = n + 1 if (prev is not None and prev == int(seq) - 1 and n > 0) else 1
        rec["arm_streak_poll"] = int(seq)
    rec["arm_streak"] = n
    return n


def _arm_gate_snapshot(rec: dict, ask: float | None = None) -> dict:
    """The handful of inputs the arm gate actually reads.

    Field names match cm_rsi_allows_buy / macd_allows_buy / exhaustion_allows_
    buy, so diffing two snapshots points straight at the input that moved
    between the batch verdict and the post-refresh one. Values are read from
    ``indicator`` first and the record second, because refresh_engine_rsi and
    refresh_engine_macd stamp the former while the seed path fills the latter.

    Cheap and total: no fetch, no raise.
    """
    ind = rec.get("indicator") if isinstance(rec, dict) else None
    ind = ind if isinstance(ind, dict) else {}

    def _pick(key):
        v = ind.get(key)
        if v is None and isinstance(rec, dict):
            v = rec.get(key)
        return v

    return {
        "ask": ask,
        "cm_rsi": _pick("cm_rsi"),
        "cm_rsi_rising": _pick("cm_rsi_rising"),
        "cm_rsi_src": _pick("cm_rsi_src"),
        "pctr": _pick("pctr"),
        "pctr_rising": _pick("pctr_rising"),
        "macd_gap": _pick("macd_gap"),
        "macd_src": _pick("macd_src"),
    }


def _log_arm_recheck(
    cp: Any,
    sym: str,
    *,
    stage: str,
    ok: bool,
    why: str | None,
    before: dict,
    after: dict,
    why_first: str | None = None,
    streak: int | None = None,
    need: int | None = None,
    px_src: str | None = None,
) -> None:
    """Record the arm verdicts that happen AFTER the shadow row is written.

    should_arm_buy runs twice per buy-ready poll: once on the batch quote,
    which shadow.jsonl records, and once on the re-pulled quote with
    re-stamped indicators, which nothing recorded. A name that armed on the
    batch and was vetoed on the refresh therefore left no trace at all -- not
    a shadow row, not an event, only a block_reason on a record that does not
    survive the session. GTLB did exactly that 163 times on 2026-09-02 and no
    log on disk could say why it never bought. The confirm streak was equally
    dark.

    Written to events.jsonl rather than shadow.jsonl on purpose: every
    existing report counts ``arm_ok is True`` shadow rows as arms, and a
    second row per arm would silently inflate all of them.

    ``stage`` is "refresh" (post-repull verdict), "confirm" (streak not yet
    met) or "pass" (cleared both, entry gates next).

    Never raises: instrumentation must not be able to stop a trade.
    """
    try:
        changed = sorted(
            k for k in after
            if k != "ask" and before.get(k) != after.get(k)
        )
        cp.log_event(
            "arm_recheck",
            symbol=sym,
            stage=stage,
            ok=bool(ok),
            why=why or None,
            why_first=why_first or None,
            streak=streak,
            need=need,
            px_src=px_src,
            changed=changed or None,
            before=before,
            after=after,
        )
    except Exception:  # noqa: BLE001
        pass


def rvol_ranked(state: dict, *, live_lookup=None) -> list[tuple[str, dict]]:
    """Watch records, strongest relative volume first, then the best setup.

    RVOL leads, as the operator asked. ``_signal_rank`` (EXH and MACD gap,
    both trending) breaks ties beneath it — so among names the tape is
    treating alike, the seat goes to the one whose own signal has turned
    rather than to whichever the loop reached first.

    RVOL is a float and two names essentially never tie on it exactly, so
    ``ai_watch_rank_rvol_band`` rounds it into buckets first: at 0.5 a 3.4x
    and a 3.2x are one group and the signal decides between them. At 0 (the
    default) the volume ordering is exact and the signal legs will almost
    never get a say — which is the honest cost of ranking a continuous
    measure first.

    Seats are scarce — ``ai_max_buys_per_poll`` is 1 and the book holds two on
    a small account — but the poll walked ``state.items()``, which is the order
    symbols happened to be written to the watch file. When several names
    qualified in the same poll the seat went to whichever the loop reached
    first, so admission order decided the trade rather than the tape.

    The live reading is looked up here rather than read off the record, because
    ``rec["rvol"]`` is only stamped once the poll reaches that symbol — after
    this sort has already run. Ranking off the record alone therefore always
    ordered by the admit-time stamp, which is the staleness this exists to
    avoid. ``_desk_rvol`` reads the cached dashboard row, so this costs no
    quote call.

    A record with no usable reading sorts last rather than first: unknown is
    not strong, and scoring it 0 would rank it above a name at 0.5x.

    Ordering only. Every record is still evaluated, so shadow, reject and
    blocker rows are unchanged — this decides who gets the seat, not who is
    looked at.
    """
    band = _rank_rvol_band()
    move_band = _rank_move_band()

    def _bucket(v: float, width: float) -> float:
        return -v if width <= 0 else -(int(v / width) * width)

    def rank(item: tuple[str, Any]) -> tuple:
        rec = item[1] if isinstance(item[1], dict) else {}
        tier, strength = _signal_rank(rec)
        v = _rank_rvol(rec, live_lookup)
        rv = (1, 0.0) if v is None else (0, _bucket(v, band))

        if move_band <= 0:
            # Move ranking off: RVOL leads, signal breaks ties beneath it.
            return (rv[0], rv[1], tier, -strength)

        # THE MOVE LEADS. The purpose of ranking at all is to spend the one
        # seat per poll on a name that is actually travelling: the shelf
        # trails 0.25% behind price, so a trade whose whole move is 0.2%
        # cannot finish above its own fill no matter how well it is managed.
        # RVOL does not measure that — it says a name is being traded hard,
        # which a heavily traded name that goes nowhere also satisfies. The
        # profitable session on 08-24 differed from every other day in exactly
        # one respect: its median peak was +0.95% against +0.12%-+0.31%.
        #
        # Banded for the same reason RVOL is: a raw float decides every
        # contest by itself and nothing beneath it is ever consulted.
        m = _rank_move(rec)
        mv = (1, 0.0) if m is None else (0, _bucket(m, move_band))
        return (mv[0], mv[1], rv[0], rv[1], tier, -strength)

    return sorted(list(state.items()), key=rank)


def _rank_move(rec: dict) -> float | None:
    """How far this name has actually travelled today, in percent.

    Live off the dashboard row first, admit-time stamp as the fallback — the
    same live-before-stale rule _rank_rvol applies, and for the same reason: a
    name can stop moving between admission and the poll that would buy it.

    SIGNED, not absolute. It was abs() on the argument that a big move is a
    big move and direction is the gates' job. That held while a day-change
    FLOOR kept decliners out of the pool. With those floors removed
    (2026-08-28, the operator's call: percent gained today is backward-looking
    and says nothing about whether a name is about to move), abs() would sort
    the day's worst decliners straight to the front of a one-seat queue —
    today's trending list is IREN -12.2%, PYPL -11.4%, MRVL -9.2%. Ordering
    is not admission, so they would still be refused, but they would occupy
    the top of the queue while doing it.
    """
    sym = str(rec.get("symbol") or "").upper().strip()
    vals = []
    if sym:
        try:
            vals.append(_f_or_none(_desk_pct_change(sym)))
        except Exception:  # noqa: BLE001
            vals.append(None)
    vals.append(_f_or_none(rec.get("pct_change")))
    vals.append(_f_or_none(rec.get("admit_pct_change")))
    for v in vals:
        if v is not None:
            return float(v)
    return None


_ENTRIES_TODAY: dict[str, object] = {"day": "", "counts": {}, "ts": 0.0}


def _entries_today(symbol: str) -> int:
    """How many times this symbol has been BOUGHT today.

    Read from the fill log, not a counter in memory: this desk restarts many
    times in a session and an in-process tally would silently reset the cap
    every time, which is the same shape as a knob nothing reads.

    Cached for a few seconds because the poll asks per symbol per pass.
    """
    import datetime as _dt
    from zoneinfo import ZoneInfo as _Z
    et = _Z("America/New_York")
    today = _dt.datetime.now(et).date().isoformat()
    now = time.time()
    if (_ENTRIES_TODAY.get("day") != today
            or now - float(_ENTRIES_TODAY.get("ts") or 0.0) > 5.0):
        counts: dict[str, int] = {}
        try:
            import json as _json
            rows = _json.load(open("alpaca_trade_log.json", encoding="utf-8"))
            todays = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                stamp = str(r.get("time") or "")
                if not stamp:
                    continue
                try:
                    t = _dt.datetime.fromisoformat(
                        stamp.replace("Z", "+00:00")).astimezone(et)
                except ValueError:
                    continue
                if t.date().isoformat() == today:
                    todays.append((stamp, r))
            todays.sort(key=lambda x: x[0])
            # A strike is a POSITION, not an attempt. An entry limit that
            # never filled is cancelled at the TTL and logged as
            # "no position" — the name did not churn, it just did not get a
            # fill, and burning the daily allowance on that punishes bad luck
            # with the book. GAP wore four strikes for two actual trades on
            # 2026-08-28 while passing every signal gate.
            pend: dict[str, int] = {}
            for _stamp, r in todays:
                k = str(r.get("ticker") or "").upper().strip()
                if not k:
                    continue
                act = str(r.get("action") or "")
                if act == "BUY":
                    pend[k] = pend.get(k, 0) + 1
                    counts[k] = counts.get(k, 0) + 1
                elif (act == "CANCELED"
                        and "no position" in str(r.get("note") or "")
                        and pend.get(k)):
                    # That BUY never became a position — take the strike back.
                    pend[k] -= 1
                    counts[k] = max(0, counts.get(k, 0) - 1)
                elif act.startswith("SELL"):
                    pend[k] = 0
        except Exception:  # noqa: BLE001
            # Unreadable log must not silently disable the cap OR block the
            # desk: keep whatever was last counted for this day.
            if _ENTRIES_TODAY.get("day") == today:
                return int((_ENTRIES_TODAY.get("counts") or {}).get(
                    str(symbol or "").upper().strip(), 0))
            return 0
        _ENTRIES_TODAY["day"] = today
        _ENTRIES_TODAY["counts"] = counts
        _ENTRIES_TODAY["ts"] = now
    return int((_ENTRIES_TODAY.get("counts") or {}).get(
        str(symbol or "").upper().strip(), 0))


def _rank_move_band() -> float:
    """Width of the percent-move bucket used for ranking. 0 = move ranking off."""
    try:
        return max(0.0, float(
            (_push_cfg() or {}).get("ai_watch_rank_move_band", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _rank_rvol_band() -> float:
    """Width of the RVOL bucket used for ranking, in multiples. 0 = exact."""
    try:
        return max(0.0, float(
            (_push_cfg() or {}).get("ai_watch_rank_rvol_band", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


# How wide a separation still counts as "more". Beyond a few standard
# deviations the gap is not meaningfully better, and letting it run unbounded
# would let one enormous ratio outrank a name that is better on both legs.
_SEP_CAP = 4.0


def _signal_rank(rec: dict) -> tuple[int, float]:
    """(tier, strength) for one candidate — lower tier wins, higher strength.

    Seats are scarce: ai_max_buys_per_poll is 1, so when several names qualify
    in the same poll exactly one gets the trade. Ranking by RVOL alone gave
    the seat to the busiest name rather than the best setup — volume says a
    name is being traded, not that its own signal has turned.

    The operator's rule: the seat goes to the strongest EXH and MACD gap that
    are TRENDING. So direction is the tier and size is the tiebreak, in that
    order — a small gap that is opening beats a wide one that is closing,
    because the wide one is a move already over.

        0  both MACD gap and %R rising   the confluence, the best evidence
        1  MACD gap rising               the entry lever turning on its own
        2  %R rising                     supporting only
        3  neither, or unreadable        no direction to trade

    Strength inside a tier is the MACD separation in standard deviations of
    its own histogram (capped) plus %R as a fraction of its range, so a name
    that is better on both legs outranks one that is better on either. A
    reading that cannot be had scores 0 rather than being guessed at, which
    sorts it behind anything measurable in the same tier — unknown is not
    strong, the same rule _rank_rvol already applies.

    Ordering only. Every record is still evaluated and every gate still runs;
    this decides who is offered the seat first, never whether it is allowed.
    """
    ind = rec.get("indicator") if isinstance(rec, dict) else None
    ind = ind if isinstance(ind, dict) else {}

    macd_up = bool(ind.get("macd_gap_rising"))
    exh_up = bool(ind.get("pctr_rising"))
    if macd_up and exh_up:
        tier = 0
    elif macd_up:
        tier = 1
    elif exh_up:
        tier = 2
    else:
        tier = 3

    strength = 0.0
    sep = _f_or_none(ind.get("macd_sep_ratio"))
    if sep is not None and sep > 0:
        strength += min(float(sep), _SEP_CAP)
    ex = exhaustion_pct(rec)
    if ex is not None:
        strength += max(0.0, min(100.0, float(ex))) / 100.0
    return tier, strength


def _record_desk_event(kind: str, t: float) -> None:
    """Mark when a state-changing pass ran (book sync, book paint), so an
    exact replay runs it at the same moments live did. Never raises."""
    try:
        import desk_io
        desk_io.set_pass(t)
    except Exception:  # noqa: BLE001
        pass
    try:
        import session_recorder
        session_recorder._append("decisions", {"ts": float(t), "ev": kind})
    except Exception:  # noqa: BLE001
        pass


def _record_arm_pass(touched: dict, t0: float) -> None:
    """One row per poll: every seated name's outcome and the inputs the arm
    check read, for the session recorder's decisions stream.

    A replay can then be compared with live check by check — which name,
    which poll, which input first differed — instead of only by the buys that
    came out the end. Never raises: telemetry must not stop the poll.
    """
    try:
        import session_recorder
        rows = []
        for sym, rec in touched.items():
            if not isinstance(rec, dict):
                continue
            ind = rec.get("indicator") if isinstance(rec.get("indicator"), dict) else {}
            rows.append({
                "s": sym,
                "st": rec.get("status"),
                "b": rec.get("block_code"),
                "px": _f_or_none(rec.get("last_ask")),
                "src": rec.get("last_ask_src") or rec.get("price_src"),
                "age": _f_or_none(rec.get("last_ask_age_sec")),
                "r": _f_or_none(ind.get("pctr")),
                "rs": _f_or_none(ind.get("pctr_slow")),
                "rsr": ind.get("pctr_slow_rising"),
                "rf": ind.get("pctr_falling"),
                "rsrc": ind.get("pctr_src"),
                "bage": _f_or_none(ind.get("bars_age_sec")),
            })
        session_recorder._append("decisions", {"ts": float(t0), "ev": "arm", "rows": rows})
    except Exception:  # noqa: BLE001
        pass


def poll_once(*, cfg: dict, now: float | None = None) -> list[dict]:
    """One RTH watch poll: refresh quotes, restructure if needed, arm/buy.

    Paper path only: placements go through ``place_scaled_entry`` and
    ``record_external_buy``. Returns a list of event dicts.
    """
    import ai_positions as cp
    import ai_trading as gt

    events: list[dict] = []
    cfg = cfg if isinstance(cfg, dict) else {}
    t0 = float(now if now is not None else time.time())
    _record_desk_event("poll", t0)

    global _POLL_SEQ
    _POLL_SEQ += 1
    poll_seq = _POLL_SEQ

    if not cfg.get("ai_watch_enabled", True):
        return [{"kind": "watch_skip", "reason": "disabled"}]

    # Drop leftover open watches from a prior ET day (first RTH poll after roll).
    # Independent of open→closed close-edge expiry in the trader loop.
    try:
        expire_stale_watches_for_new_day(t0)
    except Exception:
        pass

    # Watching window: 04:00 ET → EOD liquidate (default 15:50). Structure /
    # quotes refresh here; buys only when trading_hours_active (RTH).
    if not watch_session_active(cfg, t0):
        if past_eod_liquidate_time(cfg, t0):
            return [{"kind": "watch_skip", "reason": "eod_liquidate_window"}]
        return [{"kind": "watch_skip", "reason": "before_watch_start"}]

    try:
        market_open = bool(gt.market_is_open())
    except Exception:
        market_open = False
    allow_buys = trading_hours_active(cfg, t0, market_open=market_open)

    try:
        ready = bool(gt.is_ready())
    except Exception:
        ready = False
    if not ready:
        return [{"kind": "watch_skip", "reason": "trader_not_ready"}]

    # Free names stuck on status=submitted after the position is gone (UI
    # "sent"). Without this they never re-enter _ARMABLE_STATUSES.
    try:
        freed = release_orphaned_submits()
        for sym in freed:
            events.append({
                "kind": "submit_released",
                "symbol": sym,
                "reason": "no_position_no_orders",
            })
            try:
                cp.log_event(
                    "submit_released", symbol=sym,
                    reason="no_position_no_orders")
            except Exception:
                pass
    except Exception:
        pass

    # ai_max_buys_per_poll is a *per poll* cap, so start each poll's budget
    # here. reset_poll_counters() was only ever called from the research path
    # (ai_suggest), so on this path the counter just accumulated: after three
    # lifetime buys every later READY name was skipped with "buy_cap" until a
    # research run at 08:30/11:30/14:30 happened to clear it.
    try:
        gt.reset_poll_counters()
    except Exception:
        pass

    # Do not re-seed here — book thread runs sync_watch_from_source_panels
    # so this poll only evaluates symbols currently mirrored from the panels.

    with _WATCH_LOCK:
        state = load_watch()
    if not state:
        return events

    # Re-assert Finnhub priority/subscribe for the live book every poll —
    # cheap when already subscribed; repairs WS drops after admit.
    try:
        ensure_watch_stream(list(state.keys()))
    except Exception:
        pass

    # Only the records this poll actually touched get written back, so a
    # concurrent sync's adds/drops survive (see merge_watch_records).
    touched: dict[str, dict] = {}

    # Live indicator state, read once per poll off the cached /api/state. It is
    # stamped onto each record so should_arm_buy and the UI's READY badge read
    # the same value — the alternative (each recomputing it) is how the zone-pad
    # mismatch let the UI show READY while the poll refused to arm.
    try:
        indicators = _engine_indicator_map()
    except Exception:
        indicators = {}

    try:
        from desk_risk import dynamic_max_price
        eq = float(dashboard_state().get("ai_positions", {}).get("account", {}).get("equity") or 0.0)
        max_price_f = dynamic_max_price(eq, cfg)
    except Exception:
        max_price = cfg.get("ai_max_price")
        max_price_f = float(max_price) if max_price is not None else None
    # The band's floor, enforced where the order is placed. Admission abstains
    # on an unpriced row, and research seeds arrive unpriced, so the door alone
    # let CLF ($12.80, three fills) and INFQ ($14) trade on 2026-09-24.
    min_price_f = _f_or_none(cfg.get("ai_watch_min_price"))
    if min_price_f is not None and min_price_f <= 0:
        min_price_f = None

    try:
        risk_pct = float(cfg.get("ai_risk_pct", 1.0) or 1.0)
    except (TypeError, ValueError):
        risk_pct = 1.0

    equity_cache: float | None = None

    def _equity() -> float:
        nonlocal equity_cache
        if equity_cache is not None:
            return equity_cache
        acct = gt.get_account()
        if isinstance(acct, dict) and acct.get("ok"):
            try:
                equity_cache = float(acct.get("equity") or 0)
            except (TypeError, ValueError):
                equity_cache = 0.0
        else:
            equity_cache = 0.0
        return float(equity_cache or 0.0)

    shadow_on = bool(cfg.get("ai_shadow_log_enabled", True))
    # Consecutive stale-tape polls before a name is dropped for having no
    # quote feed at all. 0 disables the drop entirely.
    try:
        _stale_drop_after = int(cfg.get("ai_watch_stale_tape_drop_polls", 0) or 0)
    except (TypeError, ValueError):
        _stale_drop_after = 0

    # One quote call for the whole book, before the per-record fan-out below.
    # Each record that survives the tape prefilter asks for _latest_ask and
    # _latest_bid; unbatched that is two REST round trips per name per poll.
    # Priming here makes those cache reads, so the poll costs one call whether
    # the book holds 3 names or 30. Failure is silent by design — the
    # per-symbol path still works, it is just the expensive one.
    try:
        gt.prime_quotes(list(state.keys()))
    except Exception:
        pass

    for sym_key, rec in rvol_ranked(state):
        if not isinstance(rec, dict):
            continue
        sym = str(rec.get("symbol") or sym_key or "").upper().strip()
        if not sym:
            continue
        rec = dict(rec)
        rec["symbol"] = sym
        status = str(rec.get("status") or "").lower().strip()
        if status in _TERMINAL_STATUSES:
            continue
        # Levered / inverse ETPs must never occupy the AI watch book. Movers
        # already filters them; momentum/trending historically did not, so
        # MST/MSTX/TSLL sat on the book from the open with a dead tape.
        if status not in ("submitted", "filled") and is_levered_etp(sym):
            held = False
            try:
                held = bool(gt.has_open_position(sym))
            except Exception:
                held = False
            if not held:
                try:
                    events.append(cp.log_event(
                        "watch_drop", symbol=sym, reason="levered_etp"))
                except Exception:
                    events.append({
                        "kind": "watch_drop",
                        "symbol": sym,
                        "reason": "levered_etp",
                    })
                drop_watch_symbols([sym])
                continue
        if (
            status not in ("submitted", "filled")
            and _dead_reentry_blocked(sym, t0, cfg)
        ):
            events.append({
                "kind": "watch_drop",
                "symbol": sym,
                "reason": "dead_reentry",
            })
            try:
                events[-1] = cp.log_event(
                    "watch_drop", symbol=sym, reason="dead_reentry")
            except Exception:
                pass
            drop_watch_symbols([sym])
            continue

        # Scout-only short TTL + never-armable / far-exh eviction (square bus).
        if status not in ("submitted", "filled"):
            if _maybe_scout_ttl_drop(
                rec, sym=sym, cfg=cfg, now=t0, events=events, cp=cp, gt=gt,
            ):
                continue
            if _maybe_never_armable_evict(
                rec, sym=sym, cfg=cfg, now=t0, events=events, cp=cp, gt=gt,
            ):
                continue
            if _maybe_dead_unknown_evict(
                rec, sym=sym, cfg=cfg, now=t0, events=events, cp=cp, gt=gt,
            ):
                continue
            if _maybe_far_exh_evict(
                rec, sym=sym, cfg=cfg, now=t0, events=events, cp=cp, gt=gt,
            ):
                continue
            # Keep seat class fresh for UI / steal even when not dropping.
            try:
                stamp_exh_seat_fields(rec, cfg)
                _track_far_exh_seat(rec, cfg, now=t0)
            except Exception:
                pass

        live_rv = _desk_rvol(sym)
        if live_rv is not None:
            rec["rvol"] = live_rv

        sig = indicators.get(sym)
        if isinstance(sig, dict):
            # Keep this in step with what reads it. should_arm_buy gates on
            # ai_watch_arm_require, whose default names cm_rsi_rising — and
            # that key was not copied here, so sig.get() returned None on every
            # record and the gate could never be satisfied by anything. Across
            # 2026-08-07 cm_rsi_rising was False or None in all 2017 shadow
            # rows and True in none, while the engine was publishing it as True
            # on the wire. _entry_features and _shadow_row read cm_rsi and pctr
            # off the same dict, so both were logged as null for every arm and
            # reject, which is worse than not logging them: the columns exist
            # and read as a measurement.
            rec["indicator"] = {
                "proximity_pct": sig.get("proximity_pct"),
                "status": sig.get("status"),
                "buy_signal": sig.get("buy_signal"),
                "sell_signal": sig.get("sell_signal"),
                "cm_ok": sig.get("cm_ok"),
                "pctr_ok": sig.get("pctr_ok"),
                "cm_rsi_rising": sig.get("cm_rsi_rising"),
                "macd_ok": sig.get("macd_ok"),
                # The MACD LEVELS, not just the verdict. This dict REPLACES
                # the previous indicator map, and the 8/26 redesign made MACD
                # the entry lever while copying only macd_ok — so
                # macd_allows_buy looked for macd_gap on a record that had
                # just had it wiped, and refused every name with
                # "no_macd_data" while the engine held +0.0425 on BHVN.
                # Same shape as pctr below: the gate trades off the LEVEL and
                # the DIRECTION, and a single bit answers neither.
                "macd_fast": sig.get("macd_fast"),
                "macd_slow": sig.get("macd_slow"),
                "macd_gap": sig.get("macd_gap"),
                "macd_hist": sig.get("macd_hist"),
                "macd_sep_ratio": sig.get("macd_sep_ratio"),
                "macd_bull": sig.get("macd_bull"),
                "macd_cross": sig.get("macd_cross"),
                "macd_gap_rising": sig.get("macd_gap_rising"),
                "macd_gap_falling": sig.get("macd_gap_falling"),
                "macd_gap_prev": sig.get("macd_gap_prev"),
                "cm_rsi": sig.get("cm_rsi"),
                # Raw %R and its direction, not just the derived booleans. The
                # exhaustion rules below trade off the LEVEL and the TURN, and
                # pctr_ok collapses both into one bit that answers neither
                # "how overbought" nor "which way is it going".
                "pctr": sig.get("pctr"),
                # Name the source on the way in. This dict REPLACES any
                # previous indicator map, so leaving pctr_src out published an
                # engine %R with no provenance until ensure_live_exhaustion
                # re-stamped it later in the same poll — and any read landing
                # in that window (the arm gate, the wire, the book) saw
                # "pctr_not_live_missing" against a perfectly good number.
                # _shadow_row already defaults this to "engine"; the record
                # now agrees with the log instead of contradicting it.
                "pctr_src": sig.get("pctr_src") or "engine",
                "pctr_slow": sig.get("pctr_slow"),
                "pctr_rising": sig.get("pctr_rising"),
                "pctr_falling": sig.get("pctr_falling"),
                "pctr_slow_rising": sig.get("pctr_slow_rising"),
                "pctr_slow_falling": sig.get("pctr_slow_falling"),
                "pctr_ob": sig.get("pctr_ob"),
                "pctr_tight": sig.get("pctr_tight"),
                "cm_rsi_green": sig.get("cm_rsi_green"),
                # Copied for the same reason cm_rsi_rising is: it used to be
                # set only by the local recompute in apply_live_exhaustion, so
                # with that off it would read False for every name forever.
                "cm_rsi_low": sig.get("cm_rsi_low"),
                # Where the engine's bars came from, carried with the reading
                # so the arm gate can refuse an RSI drawn on the REST fallback
                # instead of the live tape. See cm_rsi_allows_buy.
                "cm_rsi_src": sig.get("bars_src"),
                "cm_rsi_age_sec": sig.get("bars_age_sec"),
                # And the same two for MACD, which is now THE entry lever.
                # Added 2026-08-27. ai_watch_require_realtime_macd went on
                # yesterday and refuses a reading with no provenance —
                # correctly, "absence is not a pass" — but this dict replaces
                # the indicator map wholesale, so the fields the guard reads
                # never survived to reach it. Every realtime name showed
                # "MACD src?" and no position could open, while the same rows
                # published a perfectly good bars_src="realtime" upstream.
                # Third field to be lost to this exact whitelist: see macd_gap
                # and pctr_src above. Same bars, so the same two keys.
                "macd_src": sig.get("bars_src"),
                "macd_age_sec": sig.get("bars_age_sec"),
                "ts": t0,
            }
        elif "indicator" in rec:
            # Engine dropped it — do not keep asserting a stale reading.
            rec.pop("indicator", None)

        # Real-time pre-filter. _latest_ask/_latest_bid are one Alpaca REST
        # round trip *each, per symbol, per poll* — with a full book that is
        # ~120 calls/min against a 200/min limit shared with the engine, RS
        # screener and dashboard. When the live tape puts price clearly outside
        # the zone there is nothing to decide, so skip both calls.
        far, stream_px = stream_says_far_from_zone(rec, cfg)
        if far:
            rec["last_trade"] = stream_px
            rec["last_poll_ts"] = t0
            # Keep last_ask on the tape so the book / EXH / arm all see the
            # same print the dashboard is showing (FGI 11.69 leftover).
            try:
                # Take the print and its clock from ONE stream_quote call.
                # This used to write last_ask and last_ask_src from stream_px
                # unconditionally, then set the age only when a separate
                # stream_quote() happened to return — so a miss left a NEW
                # price wearing the PREVIOUS reading's age, or none at all.
                # That is the desk's oldest bug class in mirror form: not a
                # static value with a moving age, but a moving value with a
                # static one. Either way the pair is a fiction, and every
                # staleness guard downstream reads it as fact.
                # tape[1] is checked BEFORE any assignment on purpose. The
                # first version of this fix assigned last_ask and src, then
                # last_ask_age_sec = float(tape[1]) — and stream_quote can
                # return a None age, so float(None) raised TypeError straight
                # into the enclosing `except (TypeError, ValueError): pass`
                # with src already written. That published src="stream" with
                # age=None on seven rows: the very split this block exists to
                # prevent, reintroduced by assignment order. Validate the
                # whole tuple first, then write all three or none of them.
                tape = stream_quote(sym)
                if (tape is not None and tape[0] and tape[1] is not None
                        and float(tape[0]) > 0):
                    _age_f = float(tape[1])
                    rec["last_ask"] = float(tape[0])
                    note_px_ring(rec, float(tape[0]), t0)
                    rec["last_ask_age_sec"] = _age_f
                    # Keep the quote clock in the map (same as
                    # apply_decision_price). Far-path used to skip this, so
                    # public_snapshot recomputed age from an older map ts
                    # while last_ask_src stayed stream → stream+stale_quote.
                    rec["last_ask_ts"] = float(t0) - _age_f
                    _set_quote_ts(str(sym).upper().strip(),
                                  float(t0) - _age_f)
                    # PPBT Sep2 honesty: age-gate the stream label (same as
                    # 2s paint). Old print → stale_tape, never stream+stale.
                    if _age_f <= decision_max_age_sec(cfg):
                        rec["last_ask_src"] = "stream"
                        clear_tape_data_block_if_stream_fresh(rec, cfg)
                    else:
                        rec["last_ask_src"] = "stale_tape"
                    honesty_restamp_stream_src(rec, cfg)
            except (TypeError, ValueError):
                pass
            # Still warm %R so the UI column and shadow log are honest —
            # without this, far names never populate exhaustion either.
            try:
                ensure_live_exhaustion(
                    rec, float(stream_px), cfg, t0, sig=sig)
            except Exception:
                pass
            zone = _structure_levels(rec.get("structure"))
            above = bool(zone and stream_px > max(zone[0], zone[1]))
            # Breakdown (far below): rebuild the synth zone under the tape so
            # the row does not sit on a permanent below_zone skip. Above stays
            # a pure wait — no rebuild from a trade print.
            if (
                not above
                and stream_px
                and stream_px > 0
                and _desk_source(rec)
            ):
                try:
                    sev = ensure_offset_zone_if_needed(
                        rec, float(stream_px), cfg, t0)
                    if sev:
                        events.append(sev)
                except Exception:
                    pass
                # Zone may now sit under the print → not far; fall through to
                # REST quote / arm path instead of stamping below forever.
                far2, _ = stream_says_far_from_zone(rec, cfg)
                if not far2:
                    far = False
            if far:
                # last_ask is deliberately NOT updated from a trade print — it
                # means "ask" everywhere else, including the UI's READY badge.
                set_block_reason(
                    rec, "above_zone" if above else "below_zone", now=t0,
                    detail=f"tape {stream_px:g}",
                )
                if shadow_on:
                    try:
                        cp.log_shadow_sample(_shadow_row(
                            rec, price=stream_px, price_src="tape",
                            arm_ok=None, arm_why="prefilter_far", now=t0))
                    except Exception:
                        pass
                touched[sym] = rec
                continue

        # Decision print: fresh tape, else a REST ask fetched now. Never a
        # leftover last_ask from a prior poll.
        try:
            bid = gt._latest_bid(sym)
        except Exception:
            bid = None
        ask_f, px_src, px_age = apply_decision_price(rec, cfg, t0)
        # Does the poll's own pricing call reach these rows, and what does it
        # return? decision_price asked out-of-band gives fresh ages (DPRO 0.1s,
        # SOFI 0.1s, ASST 0.4s) for the exact symbols published as None, under
        # the real config, yet the record keeps None and a fill at the arm site
        # changed nothing — so either this line is not reached for those rows
        # or it returns None here. Counting both ends that question; seven
        # hypotheses have already died on it.
        try:
            import ai_trading as _gt
            _gt._QUOTE_PATH_STATS["poll_priced_rows"] += 1
            if px_age is None:
                _gt._QUOTE_PATH_STATS["poll_priced_age_none_%s" % (px_src or "none")] += 1
        except Exception:  # noqa: BLE001
            pass
        # Class C file-path: never leave stale_quote / tape_only on disk when
        # eng/live print OR a young dated row age is present (SNXX blip;
        # CAPR false stale_tape beside tape_age≤ceiling). Promote label,
        # then paint-trust young field over lagging last_ask_ts (same as
        # apply_tape_blocker), re-stamp ask from the row, clear tape-data.
        _promoted = promote_stream_src_if_print_fresh(rec, cfg, now=t0)
        _paint_trusted = _paint_trust_young_stream_field(rec, cfg, now=t0)
        if _promoted or _paint_trusted:
            try:
                _epx = float(rec.get("last_ask") or 0)
            except (TypeError, ValueError):
                _epx = 0.0
            if _epx > 0:
                ask_f = _epx
            px_src = "stream"
            px_age = rec.get("last_ask_age_sec")
            clear_tape_data_block_if_stream_fresh(rec, cfg)
        elif price_src_fresh(px_src):
            clear_tape_data_block_if_stream_fresh(rec, cfg)
        tape_only = str(px_src or "").strip().lower() == "stale_tape"
        rec["last_poll_ts"] = t0

        structure = rec.get("structure") if isinstance(rec.get("structure"), dict) else None

        # Mom/ST: attach or re-anchor a mechanical pullback zone (missing levels,
        # hard_no, or price has run above a frozen synth top). Before invalidate.
        # Arm-at-last: every watching name gets a tape-centered band.
        if ask_f > 0 and (arm_at_last(cfg) or _desk_source(rec)):
            sev = ensure_offset_zone_if_needed(rec, ask_f, cfg, t0)
            if sev:
                events.append(sev)
                try:
                    # A zone is redrawn far more often than it MOVES: 2199
                    # rows on 2026-08-06 held 439 distinct zones across 36
                    # names, and drowned the 27 rows that explained the day.
                    # Scoped per symbol so each name is compared with its own
                    # last zone rather than whichever name logged most
                    # recently.
                    cp.log_state_event(
                        "synth_zone",
                        (sev.get("entry_low"), sev.get("entry_high")),
                        scope=sym,
                        symbol=sym,
                        entry_low=sev.get("entry_low"),
                        entry_high=sev.get("entry_high"),
                        anchor=sev.get("anchor"),
                        reason=sev.get("reason"),
                    )
                except Exception:
                    pass
            structure = rec.get("structure") if isinstance(rec.get("structure"), dict) else None

        # Live %R for exhaustion rules. Prefer after zone work so the double-
        # bottom / band fetch has just warmed _ohlc_cache. In-zone and below
        # are required for arming; above-zone still gets a stamp when cheap
        # (cache hit) so the column is not blank on the rest of the book.
        if ask_f > 0:
            try:
                near = _price_in_or_below_zone(rec, ask_f)
                if near or bool(cfg.get("ai_watch_exhaustion_all_rows", True)):
                    ensure_live_exhaustion(
                        rec, ask_f, cfg, t0, sig=indicators.get(sym))
            except Exception:
                pass

        # hard_no only kills non-desk (or desk when synth disabled / failed)
        if structure is not None:
            wk = structure.get("wait_kind")
            if wk is not None and str(wk).lower().strip() == "hard_no":
                if _desk_source(rec) and ask_f > 0:
                    sev = ensure_offset_zone_if_needed(rec, ask_f, cfg, t0)
                    if sev:
                        events.append(sev)
                        structure = rec.get("structure")
                if (
                    isinstance(structure, dict)
                    and str(structure.get("wait_kind") or "").lower().strip()
                    == "hard_no"
                ):
                    rec["status"] = "invalidated"
                    touched[sym] = rec
                    try:
                        events.append(cp.log_event(
                            "invalidated", symbol=sym, reason="hard_no"))
                    except Exception:
                        events.append({
                            "kind": "invalidated",
                            "symbol": sym,
                            "reason": "hard_no",
                        })
                    continue

        # Optional LLM structure only when still no usable zone and budget allows.
        # Desk names already got a synth zone above — skip model to avoid hard_no loop.
        # Stale alone is enough. The old gate also required the structure to be
        # *unusable*, so a stale-but-parseable zone was never refreshed by
        # either path and ai_structure_ttl_sec did nothing for exactly the
        # records that needed it. In practice ensure_offset_zone_if_needed above
        # has already re-anchored most stale records; this still covers names
        # with no quote, or when the synth zone is disabled.
        if _structure_stale(rec, cfg, t0):
            if (
                not _desk_source(rec)
                and structure_calls_remaining(cfg, t0) > 0
                and ask_f > 0
            ):
                sev = ensure_structure(rec, cfg, t0)
                if sev:
                    events.append(sev)
                if str(rec.get("status") or "").lower() in _TERMINAL_STATUSES:
                    touched[sym] = rec
                    continue
            elif ask_f <= 0:
                try:
                    events.append(cp.log_event(
                        "watch_skip", symbol=sym, reason="no_ask"))
                except Exception:
                    events.append({
                        "kind": "watch_skip",
                        "symbol": sym,
                        "reason": "no_ask",
                    })
                touched[sym] = rec
                continue

        if ask_f <= 0:
            set_block_reason(rec, "no_quote", now=t0, detail="no rest or tape")
            touched[sym] = rec
            continue

        def _skip(reason: str, *, detail: str | None = None, **extra):
            set_block_reason(rec, reason, now=t0, detail=detail)
            try:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym, reason=reason, ask=ask_f, **extra))
            except Exception:
                events.append({
                    "kind": "watch_skip",
                    "symbol": sym,
                    "reason": reason,
                })
            touched[sym] = rec

        if max_price_f is not None and ask_f >= max_price_f:
            _skip("above_max_price", max_price=max_price_f)
            continue
        floor_f = _min_price_for(rec.get("source"), cfg, default=min_price_f or 0.0) \
            if min_price_f is not None else None
        if floor_f is not None and floor_f > 0 and ask_f + 1e-12 < floor_f:
            _skip("below_min_price", min_price=floor_f)
            continue

        # Stale tape (unknown or old age, no REST). Show the print, do not arm.
        if tape_only or px_src == "stale_tape":
            # Prefer a live zone blocker when we already have structure so the
            # column still says above/below rather than a blank "no quote".
            b_code, _b_label = derive_blocker(rec, pad_pct=0.0)
            if b_code in ("above_zone", "below_zone", "in_zone", "no_structure"):
                if b_code == "in_zone":
                    set_block_reason(
                        rec, "stale_quote", now=t0, detail="tape age unknown or old")
                else:
                    set_block_reason(
                        rec, b_code, now=t0, detail="tape")
            else:
                set_block_reason(
                    rec, "stale_quote", now=t0, detail="tape age unknown or old")
            if shadow_on:
                try:
                    cp.log_shadow_sample(_shadow_row(
                        rec, price=ask_f, price_src="tape",
                        arm_ok=None, arm_why="tape_only", now=t0))
                except Exception:
                    pass
            # A name IEX cannot quote at all can never arm, but it still costs
            # a book slot, a poll and REST budget every cycle, and it reads to
            # the operator as a setup that might fire. Four of eleven rows were
            # in this state at 12:23 ET on 2026-08-31, some carrying quotes
            # days old (NCRA 239,722s, PSQL 2.8 days).
            #
            # Dropped only after a run of CONSECUTIVE stale polls, and the
            # streak resets on any usable quote: a thin name pausing between
            # prints is normal and must not be evicted for it. Same treatment
            # attempt_cap and dead_reentry already get.
            _n_stale = int(rec.get("stale_tape_streak") or 0) + 1
            rec["stale_tape_streak"] = _n_stale
            if 0 < _stale_drop_after <= _n_stale:
                try:
                    events.append(cp.log_event(
                        "watch_drop", symbol=sym, reason="no_quote_feed",
                        polls=_n_stale, src=px_src))
                except Exception:  # noqa: BLE001
                    events.append({"kind": "watch_drop", "symbol": sym,
                                   "reason": "no_quote_feed"})
                drop_watch_symbols([sym])
                continue
            # Time-based eviction (default ~6 min): poll streak above is
            # optional and ships off; this catches Finnhub-dead names that
            # paint stale_tape / need stream indefinitely.
            if _maybe_stale_timeout_drop(
                rec, sym=sym, px_src=px_src, cfg=cfg, now=t0,
                events=events, cp=cp, gt=gt,
            ):
                continue
            if _maybe_no_trade_after_subscribe_drop(
                rec, sym=sym, cfg=cfg, now=t0,
                events=events, cp=cp, gt=gt,
            ):
                continue
            touched[sym] = rec
            continue

        # Any usable quote clears the stale streak — the drop above is for
        # names the feed cannot price at all, not for a quiet minute.
        rec.pop("stale_tape_streak", None)
        # Tape recovered: drop the data-condition refuse so State and ready
        # can recompute. Without this, derive_blocker kept returning the
        # stored stale_quote after a fresh stream print landed.
        # stream_required / await_stream clear the same way once px_src is stream.
        if str(rec.get("block_code") or "").strip().lower() in (
            "stale_quote", "no_quote_age", "no_quote",
            "stream_required", "await_stream",
        ):
            clear_block_reason(rec)

        # Entry-only: refuse REST (and any non-stream) when the desk demands
        # a live stream print to arm. Same sites as the stale_tape block above
        # and the pre-place refresh_arm_market_data checks below.
        _sr = stream_price_required_block(px_src, cfg)
        if _sr:
            # Post-admit subscribe grace: Finnhub may not have printed yet.
            # Paint await_stream (not sticky need-stream) and re-assert the
            # WS sub — do not start the dead-tape drop clock here.
            if _within_subscribe_grace(rec, cfg, t0):
                try:
                    ensure_watch_stream([sym])
                except Exception:
                    pass
                set_block_reason(
                    rec, "await_stream", now=t0,
                    detail=f"{px_src or 'not_stream'} subscribe_grace")
                _clear_stale_feed_since(rec)
                touched[sym] = rec
                continue
            set_block_reason(rec, _sr, now=t0, detail=px_src or "not_stream")
            if _maybe_stale_timeout_drop(
                rec, sym=sym, px_src=px_src, cfg=cfg, now=t0,
                events=events, cp=cp, gt=gt,
            ):
                continue
            if _maybe_no_trade_after_subscribe_drop(
                rec, sym=sym, cfg=cfg, now=t0,
                events=events, cp=cp, gt=gt,
            ):
                continue
            touched[sym] = rec
            continue
        # Fresh stream print: clear any stale-feed clock.
        _clear_stale_feed_since(rec)

        # Arm / buy
        try:
            bid_f: float | None
            if bid is None:
                bid_f = None
            else:
                bid_f = float(bid)
        except (TypeError, ValueError):
            bid_f = None
        if bid_f is None:
            # 82% of arm-evaluated rows reached the shadow log with no bid, so
            # _spread_r could not be computed for them and the one gate that
            # prices crossing (ai_max_spread_r) had 18% of the record it was
            # waiting on. The cached quote is free — prime_quotes already
            # filled it, and it self-expires on _QUOTE_TTL_SEC.
            #
            # Deliberately NOT gt._latest_bid(): that falls back to the ASK
            # when no bid is known, which would record a zero spread — a name
            # whose book we cannot see would read as the tightest on the desk,
            # which is the opposite of true and would poison the threshold.
            try:
                hit = gt._cached_quote(sym)
            except Exception:  # noqa: BLE001
                hit = None
            if hit is not None and hit[1] is not None:
                try:
                    cached_bid = float(hit[1])
                    bid_f = cached_bid if cached_bid > 0 else None
                except (TypeError, ValueError):
                    bid_f = None

        # Session/capital refuses first so State never says "buy" / "above
        # zone" for a name we will not arm even if last sits in the band.
        try:
            cool = float(cfg.get("ai_reentry_cooldown_sec", 900.0) or 0.0)
        except (TypeError, ValueError):
            cool = 0.0
        if cool > 0:
            last_exit = _recent_exit_ts(sym)
            if last_exit and (t0 - last_exit) < cool:
                _skip("reentry_cooldown",
                      detail=f"{int(cool - (t0 - last_exit))}s left")
                continue
        # THREE STRIKES. A name that has already been round-tripped N times
        # today is done, whatever the tape says. Seven names produced thirty
        # of thirty-three trades on 2026-08-28 — ASST eight times, BULL,
        # PATH and SRPT five each — and the cooldown only spaces those out,
        # it never stops them. Counted off the fill log rather than a
        # counter in memory, so it survives the restarts this desk does
        # several times a day.
        try:
            cap = int(cfg.get("ai_watch_max_entries_per_symbol_day", 0) or 0)
        except (TypeError, ValueError):
            cap = 0
        if cap > 0:
            tries = _entries_today(sym)
            if tries >= cap:
                # Dropped, not parked. A name that cannot be opened again
                # today is not a watch candidate — leaving it on the book
                # spends a row, a quote and a poll slot on something with no
                # reachable outcome, and reads to the operator as a setup
                # that might still fire. Same treatment dead_reentry gets.
                try:
                    events.append(cp.log_event(
                        "watch_drop", symbol=sym, reason="attempt_cap",
                        entries_today=tries))
                except Exception:
                    events.append({"kind": "watch_drop", "symbol": sym,
                                   "reason": "attempt_cap"})
                drop_watch_symbols([sym])
                continue
        if _dead_reentry_blocked(sym, t0, cfg):
            try:
                events.append(cp.log_event(
                    "watch_drop", symbol=sym, reason="dead_reentry"))
            except Exception:
                events.append({
                    "kind": "watch_drop",
                    "symbol": sym,
                    "reason": "dead_reentry",
                })
            drop_watch_symbols([sym])
            continue
        wash_until = float(_wash_cooldown_until.get(sym) or 0.0)
        if wash_until > t0:
            _skip("wash_cooldown",
                  detail=f"{int(wash_until - t0)}s left")
            continue

        # A row must not be refused for lacking a quote age until something
        # has tried to give it one. _row_tape_stale fails closed on an unknown
        # age — correctly — but a record that arrives without one is then
        # refused, continued, and never priced, so it can never recover.
        #
        # Measured 2026-08-31 11:54: six rows sat IN their entry zone carrying
        # last_ask_age_sec None while decision_price, asked in the same process
        # at the same moment, returned 0.3s / 0.5s / 2.4s / 4.4s / 5.6s / 5.6s
        # for those exact symbols. The age existed; the record never got it.
        #
        # apply_decision_price is used rather than stamping an age onto the
        # price already on the record: it writes price, src and age from ONE
        # decision_price call, so the pair stays a single event. The earlier
        # attempt at this took the age from the quote cache and stamped it onto
        # whatever price was already there, which is how a stale print gets to
        # look fresh — the desk's oldest bug, and it was reverted for it.
        #
        # Bounded: only fires for a row that has no age, and reads the batch
        # prime_quotes already warmed, so it does not spend a new REST call
        # against the 200/min budget the far prefilter exists to protect.
        # Missing OR past the freshness bound. Age alone is not evidence the
        # quote is old — it is time since we last ASKED, and rows are re-priced
        # roughly every 20s while the poll runs at 10s. Measured 13:07 ET:
        # SQFT published 23.2s against a live 5.4s, NEOV 22.1s against 1.3s,
        # ASST 23.9s against 2.1s, PATH 21.1s against 3.4s. Refusing those as
        # stale is refusing a quote nobody requested. Ask, then judge.
        #
        # Bounded: only fires for a row that would otherwise be refused right
        # here, and reads the batch prime_quotes already warmed.
        _cur_age = row_quote_age_sec(rec, now=t0)
        if _cur_age is None or _cur_age > decision_max_age_sec(cfg):
            try:
                _px, _src, _age = apply_decision_price(rec, cfg, t0)
                if _px and _px > 0:
                    ask_f = float(_px)
            except Exception:  # noqa: BLE001
                pass

        ok_arm, why = should_arm_buy(rec, ask=ask_f, bid=bid_f, cfg=cfg, now=t0)
        # The counterfactual record. arm_ok False with in_zone True is the row
        # that pays for this whole mechanism: price was in the zone and the
        # desk declined, and nothing else on disk says what that cost.
        if shadow_on:
            try:
                cp.log_shadow_sample(_shadow_row(
                    rec, price=ask_f, price_src="quote", bid=bid_f,
                    arm_ok=bool(ok_arm), arm_why=why or "", now=t0))
            except Exception:
                pass
        if not ok_arm:
            if why in ("wait_setup", "hard_no", "spread", "above_zone",
                       "below_zone", "reward_risk", "no_structure",
                       "late_hold_closed", "late_hold_not_late_admit"):
                _skip(why)
            else:
                set_block_reason(rec, why or "blocked", now=t0)
                touched[sym] = rec
            continue

        # Buy-ready on this poll's batch quote can be up to a few seconds
        # stale. Re-pull NBBO + EXH/RSI before we treat the name as a buy
        # (State, gates, and place all read what we stamp here).
        _why_first = why
        _arm_before = _arm_gate_snapshot(rec, ask_f)
        ask_f, px_src, _age_fresh, bid_f = refresh_arm_market_data(
            rec, cfg, t0, gt=gt, sig=indicators.get(sym))
        if ask_f <= 0 or px_src in ("none", "stale_tape"):
            _skip("stale_quote", detail=px_src or "arm_refresh")
            continue
        _sr = stream_price_required_block(px_src, cfg)
        if _sr:
            _skip(_sr, detail=px_src or "not_stream")
            continue
        # Peak RSI across the confirm window must be visible to mistimed_heat
        # on later ticks (GTLB: confirm ~59.3, pass 53.3). Current RSI alone
        # is enough when the pass print is already ≥ the floor.
        ok_arm, why = should_arm_buy(rec, ask=ask_f, bid=bid_f, cfg=cfg, now=t0)
        if not ok_arm:
            _log_arm_recheck(
                cp, sym, stage="refresh", ok=False, why=why,
                why_first=_why_first, px_src=px_src,
                before=_arm_before, after=_arm_gate_snapshot(rec, ask_f))
            _arm_streak(rec, False, seq=poll_seq)
            if why in ("wait_setup", "hard_no", "spread", "above_zone",
                       "below_zone", "reward_risk", "no_structure",
                       "late_hold_closed", "late_hold_not_late_admit"):
                _skip(why)
            else:
                detail = (
                    mistimed_heat_detail(rec, cfg)
                    if why == "mistimed_heat" else None
                )
                set_block_reason(rec, why or "blocked", now=t0, detail=detail)
                touched[sym] = rec
            continue

        # The buy must survive as long as the sell does. MACD is computed on
        # the FORMING minute bar, so a single poll can catch it momentarily
        # bullish inside a bar that is otherwise bearish. GAP on 2026-08-28:
        #
        #   13:31:33  arm=False  macd_bearish
        #   13:31:46  arm=True   last_overbought_macd_armed   <- bought
        #   13:33:14  arm=False  macd_bearish
        #
        # Bearish either side, bullish for one thirteen-second poll, and the
        # position closed 79 seconds later on macd_negative. The hard sell has
        # required ai_exit_macd_confirm_ticks agreeing reads since this
        # morning, for exactly this reason; the entry required one. An exit
        # held to a higher standard of evidence than the entry will always
        # buy noise and sell signal.
        need_arm = _arm_confirm_ticks(cfg)
        streak = _arm_streak(rec, True, seq=poll_seq)
        # After streak update (which may clear peak on a restart), note this
        # tick's RSI so the next poll's mistimed_heat sees the window max.
        _note_confirm_rsi(rec)
        _arm_after = _arm_gate_snapshot(rec, ask_f)
        if streak < need_arm:
            _log_arm_recheck(
                cp, sym, stage="confirm", ok=False, why="arm_confirming",
                why_first=_why_first, px_src=px_src,
                streak=streak, need=need_arm,
                before=_arm_before, after=_arm_after)
            set_block_reason(rec, "arm_confirming", now=t0,
                             detail=f"{streak}/{need_arm}")
            touched[sym] = rec
            continue
        _log_arm_recheck(
            cp, sym, stage="pass", ok=True, why=why,
            why_first=_why_first, px_src=px_src,
            streak=streak, need=need_arm,
            before=_arm_before, after=_arm_after)
        # Package B: freeze the quote that earned streak-pass so the pre-place
        # refresh can refuse a jump / stale stream instead of chasing it.
        rec["confirm_ask"] = float(ask_f)
        rec["confirm_ask_ts"] = float(t0)
        rec["confirm_px_src"] = str(px_src or "") or None

        # Position / buy-cap gates (fail closed on errors — never place blind)
        try:
            if gt.has_open_position(sym):
                _skip("already_held")
                continue
        except Exception as e:  # noqa: BLE001
            _skip(f"gate_error:has_open_position:{e}"[:200])
            continue
        try:
            if not gt.can_open_new_position(sym):
                _skip("max_positions")
                continue
        except Exception as e:  # noqa: BLE001
            _skip(f"gate_error:can_open_new_position:{e}"[:200])
            continue
        try:
            if gt.buys_left_this_poll() <= 0:
                _skip("buy_cap")
                continue
        except Exception as e:  # noqa: BLE001
            _skip(f"gate_error:buys_left_this_poll:{e}"[:200])
            continue

        structure = rec.get("structure")
        if not isinstance(structure, dict):
            _skip("no_structure")
            continue

        equity = _equity()
        if equity <= 0:
            _skip("no_equity")
            continue

        # Portfolio-level risk gates. These lived only in the research path, so
        # ai_daily_loss_limit_r, ai_max_open_risk_pct, ai_max_spread_pct,
        # ai_min_dollar_volume and the already-managed check did not bind on the
        # path that places essentially every live trade. Fail closed.
        try:
            gate_ok, gate_why = cp.pre_entry_gate(
                sym,
                ask=ask_f,
                bid=bid_f,
                account_equity=equity,
                score=rec.get("score"),
                min_score=float("-inf"),
                max_price=max_price_f,
                risk_pct=risk_pct,
                # Percent-of-mid spread deliberately NOT enforced here. Quotes
                # come from IEX, a few percent of the consolidated tape, so its
                # book is artificially wide and would block legitimate fills —
                # the reason the spread check was removed from should_arm_buy
                # in the first place (see test_should_arm_in_zone_despite_wide_
                # spread). ai_max_spread_pct still binds on the research path,
                # where the name is being judged rather than filled.
                max_spread_pct=0.0,
                # The R-denominated cap IS available here, because it asks a
                # different question: not "is this book wide" (IEX always looks
                # wide) but "would crossing it cost an unacceptable fraction of
                # the money at risk". Off by default (0) precisely because the
                # quote it reads is the same untrusted IEX book — turn it on
                # only once the server's realized entry_slippage_r says what
                # crossing actually costs. See ai_max_spread_r in config.py.
                stop_price=_stop_of(rec),
                max_spread_r=float(cfg.get("ai_max_spread_r", 0.0) or 0.0),
                min_dollar_volume=(
                    float(cfg["ai_min_dollar_volume"])
                    if cfg.get("ai_min_dollar_volume") not in (None, "", 0, 0.0)
                    else None
                ),
                daily_loss_limit_r=float(cfg.get("ai_daily_loss_limit_r", 3.0)),
                max_open_risk_pct=float(cfg.get("ai_max_open_risk_pct", 5.0)),
                now=t0,
            )
        except Exception as e:  # noqa: BLE001
            _skip(f"gate_error:pre_entry_gate:{e}"[:200])
            continue
        if not gate_ok:
            _skip(_blocker_for_gate(gate_why), detail=gate_why)
            continue

        if not allow_buys:
            # Structure/zone ready, but no paper entry until RTH (market hours).
            _skip("not_trading_hours")
            continue

        # Duel day: only registered A/X champions (winner-only after trial).
        try:
            import ai_duel as duel
            if duel.duel_enabled(cfg):
                src_w = rec.get("duel_source") or rec.get("source")
                if not duel.allow_entry_for_source(cfg, src_w, sym, now=t0):
                    _skip("duel_blocked")
                    continue
        except Exception:
            pass

        # Re-pull again immediately before place — gates above can take long
        # enough that the buy-ready refresh is no longer the live print.
        confirm_ask = rec.get("confirm_ask")
        ask_f, px_src2, _age2, bid2_f = refresh_arm_market_data(
            rec, cfg, t0, gt=gt, sig=indicators.get(sym))
        if ask_f <= 0 or px_src2 in ("none", "stale_tape"):
            _arm_streak(rec, False, seq=poll_seq)
            for _k in ("confirm_ask", "confirm_ask_ts", "confirm_px_src"):
                rec.pop(_k, None)
            _skip("confirm_stale", detail=px_src2 or "stale_quote",
                  confirm_ask=confirm_ask)
            continue
        _sr2 = stream_price_required_block(px_src2, cfg)
        if _sr2:
            # After a clean streak pass, a non-stream print is confirm_stale
            # (FRVO-class), not a fresh stream_required veto with no provenance.
            _arm_streak(rec, False, seq=poll_seq)
            for _k in ("confirm_ask", "confirm_ask_ts", "confirm_px_src"):
                rec.pop(_k, None)
            _skip("confirm_stale", detail=px_src2 or "not_stream",
                  confirm_ask=confirm_ask)
            continue
        slip_ok, slip_why = _confirm_slip_ok(confirm_ask, ask_f, cfg)
        if not slip_ok:
            _arm_streak(rec, False, seq=poll_seq)
            for _k in ("confirm_ask", "confirm_ask_ts", "confirm_px_src"):
                rec.pop(_k, None)
            _skip("confirm_slip", detail=slip_why, confirm_ask=confirm_ask,
                  send_ask=ask_f)
            continue
        if bid2_f is None:
            bid2_f = bid_f
        ok_arm2, why2 = should_arm_buy(rec, ask=ask_f, bid=bid2_f, cfg=cfg, now=t0)
        if not ok_arm2:
            _arm_streak(rec, False, seq=poll_seq)
            for _k in ("confirm_ask", "confirm_ask_ts", "confirm_px_src"):
                rec.pop(_k, None)
            _skip(f"recheck_{why2}")
            continue

        place_decision = _decision_for_place(
            structure, ask=ask_f, cfg=cfg,
            late_hold=(why2 == "last_late_hold"))
        if arm_at_last(cfg):
            place_decision["skip_zone"] = True
            if not place_decision.get("zone_kind"):
                place_decision["zone_kind"] = "at_last"
        # Stamp the rule that authorised this entry onto the decision, so the
        # outcome row can be sliced by it later. Read here rather than in
        # _decision_for_place, which sees the structure but not the record.
        place_decision["entry_exhaustion"] = exhaustion_pct(rec)
        place_decision["entry_exhaustion_state"] = exhaustion_state(rec, cfg)
        # Arm class (square vs heating) — stop logging only seed text.
        _arm_why = str(why2 or "")
        if exh_square_arm_enabled(cfg) and _arm_why in (
                "overbought", "last_overbought"):
            _arm_why = "square"
        elif _arm_why in (
                "oversold_triangle", "last_oversold_triangle", "zone_oversold_triangle"):
            _arm_why = "oversold_triangle"
        elif not _arm_why:
            _arm_why = str(
                place_decision.get("entry_exhaustion_state") or "unknown")
        place_decision["arm_why"] = _arm_why
        stamp_exh_seat_fields(rec, cfg)
        maybe_freeze_exh_seat_class_admit(rec)
        place_decision["exh_seat_class"] = str(
            rec.get("exh_seat_class") or "") or None
        _admit_cls = str(rec.get("exh_seat_class_admit") or "").strip().lower()
        if _admit_cls not in _KNOWN_EXH_SEAT_CLASSES:
            _admit_cls = str(rec.get("exh_seat_class") or "").strip().lower()
        place_decision["exh_seat_class_admit"] = (
            _admit_cls if _admit_cls in _KNOWN_EXH_SEAT_CLASSES else None
        )
        place_decision["square_since"] = _f_or_none(rec.get("square_since"))
        place_decision["os_square_since"] = _f_or_none(rec.get("os_square_since"))
        place_decision["left_os_since"] = _f_or_none(rec.get("left_os_since"))
        # Dual-%R proof at fill — auditable against TV ■ (no sticky).
        _ind = rec.get("indicator") if isinstance(rec.get("indicator"), dict) else {}
        place_decision["pctr"] = _f_or_none(_ind.get("pctr"))
        place_decision["pctr_slow"] = _f_or_none(_ind.get("pctr_slow"))
        place_decision["pctr_gap"] = _f_or_none(_ind.get("pctr_gap"))
        place_decision["pctr_ob"] = (
            bool(_ind.get("pctr_ob")) if _ind.get("pctr_ob") is not None else None
        )
        place_decision["pctr_os"] = (
            bool(_ind.get("pctr_os")) if _ind.get("pctr_os") is not None else None
        )
        place_decision["pctr_tight"] = (
            bool(_ind.get("pctr_tight")) if _ind.get("pctr_tight") is not None
            else None
        )
        place_decision["sticky_used"] = False
        # This desk runs the exhaustion gate; ai_suggest's does not. Name the
        # path on the row so the two never average together again.
        place_decision["entry_path"] = (
            "late_hold" if place_decision.get("late_hold") else "watch"
        )
        if isinstance(place_decision, dict):
            place_decision = dict(place_decision)
            place_decision["source"] = rec.get("duel_source") or rec.get("source")
            place_decision["duel_source"] = place_decision.get("source")
            # bid2_f and the decision's own stop, so spread_r is measured
            # against the R this trade actually takes — not the structure's.
            place_decision["features"] = _entry_features(
                rec, ask=ask_f, bid=bid2_f,
                stop=place_decision.get("stop_price"))
            if isinstance(place_decision["features"], dict):
                place_decision["features"]["arm_why"] = _arm_why
                place_decision["features"]["sticky_used"] = False
        rec["status"] = "armed"
        set_block_reason(rec, "placing", now=t0)
        try:
            result = cp.place_scaled_entry(
                sym,
                place_decision,
                equity,
                risk_pct=risk_pct,
                current_ask=ask_f,
                # bid2_f is the same quote should_arm_buy was given, so a
                # passive anchor prices off the book the gate actually saw.
                current_bid=bid2_f,
                current_last=float(rec.get("last_trade") or ask_f or 0) or None,
                duel_source=str(
                    rec.get("duel_source") or rec.get("source") or ""
                ) or None,
            )
        except Exception as e:  # noqa: BLE001
            err = str(e)[:200]
            set_block_reason(rec, "order_failed", now=t0, detail=err)
            if str(rec.get("status") or "") == "armed":
                rec["status"] = "watching"
            try:
                events.append(cp.log_event(
                    "entry_fail", symbol=sym, reason=err))
            except Exception:
                events.append({
                    "kind": "entry_fail",
                    "symbol": sym,
                    "reason": err,
                })
            touched[sym] = rec
            continue

        if isinstance(result, dict) and result.get("ok"):
            rec["status"] = "submitted"
            clear_block_reason(rec)
            try:
                gt.record_external_buy(sym, {
                    "reason": str(rec.get("reason") or "")[:120],
                    "score": rec.get("score"),
                    "stop_price": result.get("stop_price"),
                    "target_1": result.get("target_1"),
                    "source": "entry_watch",
                })
            except Exception:
                pass
            # place_scaled_entry already logs entry_ok — do not double-log
            # (2026-08-11 had every fill appear twice in events.jsonl).
        else:
            err = ""
            if isinstance(result, dict):
                err = str(result.get("error") or "place_failed")[:200]
            else:
                err = "place_failed"
            wash_hit = (
                "wash" in err.lower()
                or "40310000" in err
                or err.strip().lower() == "wash_trade"
            )
            if wash_hit:
                cool_s = float(
                    cfg.get("ai_wash_cooldown_sec", _WASH_COOLDOWN_SEC)
                    or _WASH_COOLDOWN_SEC
                )
                _wash_cooldown_until[sym] = t0 + max(60.0, cool_s)
                set_block_reason(rec, "wash_trade", now=t0, detail=err)
            else:
                set_block_reason(rec, err, now=t0, detail=err)
            try:
                events.append(cp.log_event(
                    "entry_fail", symbol=sym, reason=err,
                    wash_cooldown=wash_hit))
            except Exception:
                events.append({
                    "kind": "entry_fail",
                    "symbol": sym,
                    "reason": err,
                })
            # Stay watching for non-wash retries; wash cools via _wash_cooldown_until
            if str(rec.get("status") or "") == "armed":
                rec["status"] = "watching"

        touched[sym] = rec

    _record_arm_pass(touched, t0)

    # Demand-driven stale_tape_cap + preferential unarmable-stale steal (A1).
    # Build inclusion-cleared funnel candidates first; cap only drops when a
    # young-stream admittee is waiting (no blind churn into vacuum). Steal
    # still fires when stream-ready seats < 2. No reseed cool on either path.
    try:
        with _WATCH_LOCK:
            _cap_state = dict(load_watch() or {})
        for _k, _v in touched.items():
            if isinstance(_v, dict):
                _cap_state[_k] = _v
        # Inclusion-cleared shortlist from the last sync funnel (cheap file
        # read). Young-stream check happens inside cap/steal helpers.
        _steal_cands: list[dict] = []
        try:
            _funnel_path = REPORT_DIR / "admit_funnel.json"
            if _funnel_path.is_file():
                _funnel = json.loads(_funnel_path.read_text(encoding="utf-8"))
                for _cs in (_funnel or {}).get("kept_symbols") or []:
                    _sym = str(_cs or "").upper().strip()
                    if not _sym or _sym in _cap_state:
                        continue
                    _steal_cands.append({"symbol": _sym})
        except Exception:
            _steal_cands = []
        _cap_dropped = _enforce_stale_tape_seat_cap(
            _cap_state, cfg=cfg, now=t0, events=events, cp=cp, gt=gt,
            candidates=_steal_cands)
        for _s in _cap_dropped:
            touched.pop(_s, None)
            _cap_state.pop(_s, None)
        _steal_dropped = _preferential_unarmable_steal(
            _cap_state, cfg=cfg, now=t0, events=events, cp=cp, gt=gt,
            candidates=_steal_cands)
        for _s in _steal_dropped:
            touched.pop(_s, None)
            _cap_state.pop(_s, None)
        _preheat_dropped = _preferential_preheat_steal(
            _cap_state, cfg=cfg, now=t0, events=events, cp=cp, gt=gt,
            candidates=_steal_cands)
        for _s in _preheat_dropped:
            touched.pop(_s, None)
            _cap_state.pop(_s, None)
        # Active pre-square farm: pull square/pre_square from the same
        # soft-seed universe and steal far seats (incl. flood) for them.
        _farm_cands: list[dict] = list(_steal_cands)
        try:
            for _row in _soft_seed_source_rows(cfg, now=t0):
                if not isinstance(_row, dict):
                    continue
                _sym = str(_row.get("symbol") or "").upper().strip()
                if not _sym or _sym in _cap_state:
                    continue
                _out = dict(_row)
                stamp_exh_seat_fields(_out, cfg)
                if str(_out.get("exh_seat_class") or "") in (
                        "pre_square", "square", "os_square", "os_triangle"):
                    _farm_cands.append(_out)
        except Exception:
            pass
        _far_dropped = _preferential_far_exh_steal(
            _cap_state, cfg=cfg, now=t0, events=events, cp=cp, gt=gt,
            candidates=_farm_cands)
        for _s in _far_dropped:
            touched.pop(_s, None)
            _cap_state.pop(_s, None)
    except Exception:
        pass

    # Plan B burst path (premarket mention burst → first closed 1m bar at/
    # after 09:30 with CM RSI-2 >= 70). Isolated from Plan A arm gates:
    # this block never OR's into cool/EXH/soft_ob/mistimed, and cannot
    # weaken those refusals. strength_signal logs only; strength_trade is
    # behind TWO gates that both ship safe (enabled False, dry_run True).
    # See docs/PLAN_B_BURST.md. Closed bars only — not 5-second polls.
    try:
        import strength_signal
        _sigs = strength_signal.evaluate(
            list(touched.keys()), cfg, t0, ew=sys.modules[__name__])
        if _sigs:
            import strength_trade
            strength_trade.consider(_sigs, cfg, t0, cp=cp, gt=gt)
    except Exception:
        pass

    merge_watch_records(touched)
    return events
