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
    "already_extended": "extended",
    "wait_exh": "wait EXH",
    "wait_rsi": "wait RSI",
    "exh_not_tight": "EXH wide",
    "exh_rsi": "ready",
    "last_exh_rsi": "ready",
    "in_zone_fade_ok": "in zone",
    "overbought_hot": "OB hot",
    "dead_reentry": "dead today",
    "loser_reentry": "dead today",
    "thin_rvol": "rvol low",
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
    # Tape is fresh (check above). A leftover data-condition refuse is not a
    # real poller veto — same rule as derive_blocker fall-through.
    if not code or code in (
        "in_zone", "placing", "at_last",
        "stale_quote", "no_quote_age", "no_quote",
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
        # Poller may have stamped below_zone before the print recovered into
        # the armable dip window; re-evaluate geometry so the column is not
        # stuck on a stale below while price is still a valid buy.
        #
        # Same for tape-data refuses: stale_quote / no_quote_age must clear
        # once _row_tape_stale is false. Keeping them in `stored` locked
        # stream+young-age rows on "stale quote" after the 60s ceiling
        # recovered the print (2026-09-01).
        if code in (
            "below_zone", "above_zone", "in_zone",
            "stale_quote", "no_quote_age", "no_quote",
        ):
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
    try:
        ok, why = should_arm_buy(rec, ask=float(px), bid=None, cfg=_push_cfg())
    except Exception:
        return None
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
    if _row_tape_stale(row):
        row["ready"] = False
        row["block_code"] = "stale_quote"
        row["blocker"] = format_blocker("stale_quote")
        row["block_reason"] = row["blocker"]
        if not row.get("block_detail"):
            row["block_detail"] = "tape age unknown or old"
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
            px, _age = got
            if px and px > 0:
                r["price"] = px
                r["last_ask"] = px
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


_RESEARCH_SOURCES = frozenset({
    "research", "xai", "anthropic", "grok", "claude", "a", "x", "ax", "ai",
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
        with _dash_urlopen(f"{DASHBOARD_URL}/api/state") as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise TypeError(f"unexpected payload type {type(data).__name__}")
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
        if not _price_under_cap(r.get("price"), max_price):
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


def desk_candidate_rows(cfg: dict | None = None) -> list[dict]:
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

    Order matters: seeds run strongest-claim first and `seen` makes the first
    one to name a symbol own its row. Momentum and trending come first because
    their rows carry price, pct_change and rvol measured off the desk — the
    numbers the gate actually judges. Research and a bro call are reasons to
    look, and neither should overwrite a row that already has evidence in it.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    rows: list[dict] = []
    seen: set[str] = set()
    try:
        from desk_risk import dynamic_max_price
        eq = float(dashboard_state().get("ai_positions", {}).get("account", {}).get("equity") or 0.0)
        max_price = dynamic_max_price(eq, cfg)
    except Exception:
        max_price = cfg.get("ai_max_price", cfg.get("claude_max_price"))
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
            for _, r in scored[:n]:
                if r["symbol"] in seen:
                    continue
                seen.add(r["symbol"])
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
                if not s or not s[0].isalpha() or s in seen:
                    continue
                if not _price_under_cap(r.get("price"), max_price):
                    continue
                # Known-thin tape never seeds: same floor as passes_inclusion.
                try:
                    rv = float(r.get("rvol")) if r.get("rvol") is not None else None
                except (TypeError, ValueError):
                    rv = None
                if rv is not None and min_rvol > 0 and rv < min_rvol:
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
                if open_seed_min_pct > 0:
                    seed_pct = _pct_change_value(r.get("pct_change"))
                    if seed_pct is None or seed_pct < open_seed_min_pct:
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
                pct = _pct_change_value(r.get("pct_change"))
                rank = float(tr_rank.get(s) or 0.0)
                if not on_tr:
                    rank = abs(pct or 0.0)
                reason = (
                    f"mom+trending {tr_rank[s]:.0f}"
                    if on_tr
                    else "momentum desk"
                )
                open_scored.append((rank, {
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
                    "criteria": ["mom_open", "mom_trending"] if on_tr else ["mom_open"],
                    "bypass_inclusion": False,
                    "mom_open_soft": True,
                }))
            open_scored.sort(key=lambda t: t[0], reverse=True)
            added = 0
            for _, r in open_scored:
                if added >= n:
                    break
                if r["symbol"] in seen:
                    continue
                seen.add(r["symbol"])
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
                    # Known-thin tape never seeds.
                    if rvol is not None and tr_min_rvol > 0 and rvol < tr_min_rvol:
                        continue
                    # Need at least one claim: score, day move, or elevated rvol.
                    score_ok = score > min_score
                    pct_ok = pct is not None and pct >= tr_min_pct
                    rvol_ok = (
                        rvol is not None
                        and tr_min_rvol > 0
                        and rvol >= tr_min_rvol
                    )
                    # Long-only: refuse red days when we know the change.
                    if pct is not None and pct <= 0:
                        continue
                    # On the TREND panel and green is enough to shortlist
                    # (score ranks; it is not a hard floor). require_look_ext
                    # still needs a numeric claim so the EXT-only path stays
                    # tight. SENS +9.8% / score 4.8 sat on the panel and
                    # never reached the book.
                    if require_ext and not (score_ok or pct_ok or rvol_ok):
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
                    rows.append({
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
                    })
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
                    if not s or not s[0].isalpha() or s in seen:
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
                        continue
                    pct = _pct_change_value(pct_src)
                    if pct is None or pct < mv_min_pct:
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
                    seen.add(s)
                    row = dict(r)
                    row["symbol"] = s
                    row["source"] = "movers"
                    row["rvol"] = rvol
                    row["pct_change"] = pct
                    row["price"] = px_src
                    row["quote_src"] = src_used
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
                    for prev in rows:
                        if str(prev.get("symbol") or "").upper().strip() != s:
                            continue
                        crit = list(prev.get("criteria") or [])
                        if "bro_call" not in crit:
                            crit.append("bro_call")
                            prev["criteria"] = crit
                        break
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

    return rows


# symbol -> consecutive qualifying polls, for admission dwell.
_admit_ticks: dict[str, int] = {}

# symbol -> monotonic ts of the last engine push (debounce; see below).
_pushed_at: dict[str, float] = {}

# Last sync's rejections, for the wire (why a name is NOT on the book).
_last_rejected: list[dict] = []


def last_rejected() -> list[dict]:
    """Most recent inclusion-gate rejections: [{symbol, reason, criteria}]."""
    return list(_last_rejected)


def _push_cfg() -> dict:
    try:
        from config import load_config
        return load_config() or {}
    except Exception:
        return {}


def push_candidates_to_engine(symbols: list[str]) -> dict:
    """Ask the signal engine to start computing indicators for *symbols*.

    The engine only evaluates its own watchlist, which is fed by Discord
    mentions — so the trending names on this book had no indicator data at all
    and an indicator gate would reject everything. Push the delta (never the
    full list on every 2s tick) so the engine has bars/state ready by the time
    the gate asks for it, one scan_interval_sec later.
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
    if not wanted:
        return {"pushed": 0, "known": 0}
    known = set(_engine_indicator_map())
    missing = [s for s in wanted if s not in known]
    if not missing:
        return {"pushed": 0, "known": len(known)}

    # Hard cap. Finnhub's free tier allows ~50 concurrent WS subscriptions
    # across the whole desk, and finnhub_stream.request_subscribe does not
    # enforce it — it only mentions it in a docstring. Overflow symbols get no
    # trades, so no forming bars, so no indicator state, so the inclusion gate
    # rejects them. That failure is completely silent, which is worse than
    # admitting fewer names, so leave headroom for the engine's own tickers.
    try:
        cap = int(_push_cfg().get("ai_watch_engine_push_max", 24) or 0)
    except (TypeError, ValueError):
        cap = 24
    if cap > 0:
        room = max(0, cap - len(known))
        if room <= 0:
            return {"pushed": 0, "known": len(known), "capped": True}
        missing = missing[:room]

    # Debounce. This runs inside the 2s book sync, but a freshly pushed symbol
    # does not appear in the indicator map until the engine's next scan
    # (scan_interval_sec, 60s) — so without this it stays "missing" and we
    # re-POST it ~30 times per symbol while waiting.
    now = time.monotonic()
    try:
        hold = float(_push_cfg().get("scan_interval_sec", 60) or 60) * 2.0
    except (TypeError, ValueError):
        hold = 120.0
    missing = [m for m in missing if m not in _pushed_at or (now - _pushed_at[m]) > hold]
    if not missing:
        return {"pushed": 0, "known": len(known), "debounced": True}
    for m in missing:
        _pushed_at[m] = now
    try:
        with _dash_urlopen(
            f"{DASHBOARD_URL}/api/tickers/add-bulk",
            # src="book" marks these as data subscriptions, not momentum
            # candidates — the Momentum panel filters them out so pushing the
            # whole book does not bury the panel it seeds from.
            data=json.dumps({"tickers": missing, "src": "book"}).encode("utf-8"),
            method="POST",
        ):
            pass
        return {"pushed": len(missing), "known": len(known)}
    except Exception:
        return {"pushed": 0, "known": len(known), "error": True}


def live_print(symbol: str) -> tuple[float, float | None] | None:
    """Dashboard tape print: ``(price, age_sec_or_None)``.

    ``price_age_sec`` is the observation age. ``price_ts`` is a write clock
    and must not be used for freshness. Age None means the desk has a number
    but cannot prove it is live — callers must not treat that as fresh.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None
    for r in _dashboard_tickers():
        if not isinstance(r, dict):
            continue
        if str(r.get("ticker") or r.get("symbol") or "").upper().strip() != sym:
            continue
        try:
            px = float(r.get("price") or 0)
        except (TypeError, ValueError):
            return None
        if px <= 0:
            return None
        age: float | None
        try:
            age = float(r.get("price_age_sec"))
        except (TypeError, ValueError):
            age = None
        return px, age
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


def decision_price(
    symbol: str,
    cfg: dict | None,
    now: float | None = None,
) -> tuple[float | None, str, float | None]:
    """Price used to arm or flatten — never a leftover ``last_ask``.

    Returns ``(price, src, age_sec)`` where src is ``stream``, ``rest``,
    ``stale_tape``, or ``none``.

    Fresh dashboard tape (Finnhub/Alpaca stream, age ≤ max) wins: that is
    the print the operator sees. Otherwise a just-fetched REST ask. A tape
    print with unknown or old age is ``stale_tape`` and must not arm.
    """
    max_age = decision_max_age_sec(cfg)
    tape = live_print(symbol)
    if tape is not None:
        px, age = tape
        if age is not None and age <= max_age and px > 0:
            return px, "stream", age
    ask_f = 0.0
    try:
        import ai_trading as gt
        ask = gt._latest_ask(symbol)
        ask_f = float(ask) if ask is not None else 0.0
    except Exception:
        ask_f = 0.0
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
        dev = _ask_max_dev_pct(cfg)
        if (dev > 0 and tape is not None and tape[0] and tape[0] > 0
                and abs(ask_f - tape[0]) / tape[0] * 100.0 > dev):
            return tape[0], "stale_tape", tape[1]
        # The REST ask now carries the quote's own age when Alpaca supplied
        # one. It used to return None unconditionally, which read downstream
        # as "cannot prove it is live" and — because every guard failed open
        # — was then treated as fresh. A freshly FETCHED quote is not a
        # freshly QUOTED one: premarket 8/26 this path served an IEX ask of
        # 0.00 behind a quote 14 hours old. None still means unprovable.
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
        if age is not None:
            rec["last_ask_ts"] = float(now) - float(age)
            if _sym_k:
                _LAST_QUOTE_TS[_sym_k] = float(now) - float(age)
        else:
            rec.pop("last_ask_ts", None)
            if _sym_k:
                _LAST_QUOTE_TS.pop(_sym_k, None)
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
    if sym and _dead_reentry_blocked(sym, time.time(), cfg):
        return False, met, "dead_reentry"
    source = str(row.get("source") or "").strip().lower()
    is_research = source in _RESEARCH_SOURCES

    price = row.get("price")
    try:
        price_f = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_f = None
    min_price = float(cfg.get("ai_watch_min_price", 1.0) or 0.0)
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

    # Momentum and trending both need evidence of unusual activity, not just
    # popularity — a flat RVOL means nothing dislocated today regardless of
    # score or a flag. Research-sourced rows are untouched by this gate.
    #
    # A KNOWN-low RVOL rejects; an UNKNOWN one abstains. The producer
    # (trending_screener) publishes rvol=None on every row whenever the volume
    # refresh has not resolved, so failing closed on absence empties the book
    # outright rather than filtering it — 15 candidates to 0 on 2026-08-06.
    # Same rule apply_look_highlights already uses: "unknown rvol neither
    # passes nor blocks." Absence of evidence is not evidence of absence; the
    # remaining conjunctive gates still have to pass.
    if source in ("momentum", "trending") or mom_soft:
        if source == "trending":
            min_rvol = float(
                cfg.get("ai_watch_trending_min_rvol",
                        cfg.get("ai_watch_min_rvol", 2.0))
                or 0.0
            )
        else:
            min_rvol = float(cfg.get("ai_watch_min_rvol", 2.0) or 0.0)
        if min_rvol > 0:
            rvol_f = _f_or_none(row.get("rvol"))
            if rvol_f is not None:
                if rvol_f < min_rvol:
                    return False, met, "thin_rvol"
                met.append("rvol")

    # Soft mom_open path: after price + rvol (+ uptrend above), admit without
    # score / EXT / indicator gates.
    if mom_soft:
        met = list(dict.fromkeys(met))
        return True, met, ""

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
    for row in rows:
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym or sym in kept_syms:
            continue
        seen.add(sym)
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
    for rec in last_reject.values():
        rejected.append(rec)
        why = str(rec.get("reason") or "")
        if not why.startswith("dwell_"):
            _admit_ticks.pop(rec["symbol"], None)
    for gone in [s for s in _admit_ticks if s not in seen]:
        _admit_ticks.pop(gone, None)
    return kept, rejected


def research_candidate_rows() -> list[dict]:
    """Current AI Research board rows (Grok + Anthropic), as watch candidates."""
    out: list[dict] = []
    seen: set[str] = set()
    # Grok last so it wins on overlap when both boards list the same name.
    for path, default_src in (
        (ROOT / "claude_suggestions.json", "anthropic"),
        (ROOT / "suggestions.json", "anthropic"),
        (ROOT / "grok_suggestions.json", "xai"),
    ):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        file_src = str(raw.get("source") or default_src).lower().strip()
        if file_src in ("xai", "grok"):
            src_label = "xai"
        elif file_src in ("anthropic", "claude"):
            src_label = "anthropic"
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
            out.append({
                "symbol": s,
                "trending_score": _score_from_row(r),
                "score": _score_from_row(r),
                "reason": str(r.get("reason") or r.get("summary") or "research")[:80],
                "agreement": True,
                "source": src_label,
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

    # Candidate rows come from the dashboard over HTTP (two GETs, 2s timeout
    # each) — do that *outside* the lock so a slow/absent dashboard cannot stall
    # poll_once behind us for seconds at a time.
    candidates = desk_candidate_rows(cfg)

    # Make sure the engine is computing indicators for everything on the
    # shortlist, then admit only what clears the strict conjunctive gate.
    try:
        push_candidates_to_engine([r.get("symbol") for r in candidates])
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
        candidates, rejected = apply_inclusion_gate(candidates, cfg)
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
    except Exception:
        pass

    with _WATCH_LOCK:
        return _sync_watch_locked(candidates, t0, cfg)


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
        if _dead_reentry_blocked(sym, t0, cfg if isinstance(cfg, dict) else {}):
            continue
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
            if _dead_reentry_blocked(key, t0, cfg_d):
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
        if isinstance(prev.get("indicator"), dict) and prev["indicator"]:
            rec["indicator"] = dict(prev["indicator"])
        for k in (
            "block_code", "block_reason", "block_ts", "block_detail",
            "exh_was_overbought", "pctr_fall_since", "last_trade", "last_ask_src",
            "zone_touch_ts",
        ):
            if prev.get(k) is not None:
                rec[k] = prev[k]
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

    # Keep in-flight paper entries even if they left the panels (still managing).
    # Also keep daily A/X duel champions (research) — desk-only sync would drop them.
    for sym, rec in old.items():
        if not isinstance(rec, dict):
            continue
        key = str(sym or rec.get("symbol") or "").upper().strip()
        if not key or key in new_state:
            continue
        status = str(rec.get("status") or "").lower().strip()
        is_duel = bool(rec.get("duel") or rec.get("duel_source"))

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
        if status not in ("submitted", "filled") and not is_duel:
            try:
                _grace = float((cfg or {}).get("ai_watch_admit_grace_sec", 0) or 0)
            except (TypeError, ValueError):
                _grace = 0.0
            if _grace > 0 and not _dead_reentry_blocked(
                    key, t0, cfg if isinstance(cfg, dict) else {}):
                _seen = _f_or_none(rec.get("last_candidate_ts"))
                if _seen is not None and (t0 - _seen) <= _grace:
                    new_state[key] = dict(rec)
                    continue

        if status in ("submitted", "filled") or is_duel:
            if status in ("invalidated", "expired") and not is_duel:
                continue
            if (
                status not in ("submitted", "filled")
                and _dead_reentry_blocked(
                    key, t0, cfg if isinstance(cfg, dict) else {}
                )
            ):
                continue
            kept = dict(rec)
            kept["symbol"] = key
            new_state[key] = kept

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
    """MACD momentum for the book's MACD Gap column.

    The 8/26 redesign made MACD the entry lever and added the column, the
    renderer, the CSS and the arm gate — but nothing ever put the numbers on
    the wire, so every row rendered "—" while the engine had real values.
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


def is_overbought(record: dict, cfg: dict) -> bool | None:
    """True when %R has reached the overbought band. None when unknown.

    TV desk mode (both lines): red boxes = fast AND slow >= -threshold.
    Legacy: fast line only (100 + %R >= 100 - threshold).
    """
    if tv_exh_rsi_enabled(cfg):
        ind = record.get("indicator") if isinstance(record, dict) else None
        if not isinstance(ind, dict):
            return None
        if ind.get("pctr_ob") is True:
            return True
        fast = _f_or_none(ind.get("pctr"))
        slow = _f_or_none(ind.get("pctr_slow"))
        if fast is None or slow is None:
            return None if fast is None and slow is None else False
        try:
            thr = float(cfg.get("rte_threshold", 20) or 20)
        except (TypeError, ValueError):
            thr = 20.0
        return fast >= -thr and slow >= -thr
    ex = exhaustion_pct(record)
    if ex is None:
        return None
    try:
        thr = float(cfg.get("rte_threshold", 20) or 20)
    except (TypeError, ValueError):
        thr = 20.0
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


def macd_allows_buy(record: dict, cfg: dict) -> tuple[bool, str]:
    """Buy side of the MACD momentum gate.

    Opens positions on MACD bullish crossover where slow and fast lines have
    sufficient separation/gap (the farther apart, the more bullish the signal).
    """
    if not bool(cfg.get("ai_watch_arm_require_macd", False)):
        return True, "macd_off"
    ind = record.get("indicator") if isinstance(record, dict) else None
    ind = ind if isinstance(ind, dict) else {}

    fast = _f_or_none(ind.get("macd_fast") if ind.get("macd_fast") is not None else ind.get("macd_line"))
    slow = _f_or_none(ind.get("macd_slow") if ind.get("macd_slow") is not None else ind.get("macd_signal"))
    gap = _f_or_none(ind.get("macd_gap") if ind.get("macd_gap") is not None else ind.get("macd_hist"))
    if fast is None or slow is None or gap is None:
        if isinstance(record, dict):
            record["block_detail"] = "no realtime MACD (needs 1-min bars)"
        return False, "no_macd_data"

    # Was this reading drawn on the live tape, and how old is it? MACD became
    # the entry lever on 8/26 with no provenance check of its own, while the
    # levers it replaced both had one. bars_src flips per ticker mid-session,
    # so an ungated gate alternates between the Finnhub tape (0.3s at the
    # median, measured 8/26) and the Alpaca REST fallback (up to 60s) without
    # saying which it used. Refused rather than merely noted: an entry on a
    # 60s-old MACD is an entry on a different indicator.
    if bool(cfg.get("ai_watch_require_realtime_macd", False)):
        live, why = macd_reading_is_live(ind, cfg)
        if not live:
            if isinstance(record, dict):
                if why == "macd_src_unknown":
                    record["block_detail"] = "MACD source unknown"
                elif why == "macd_stale_bars":
                    age = _f_or_none(ind.get("macd_age_sec"))
                    record["block_detail"] = (
                        f"MACD bars {age:.1f}s old" if age is not None
                        else "MACD bars too old")
                else:
                    src = str(ind.get("macd_src") or ind.get("bars_src") or "")
                    record["block_detail"] = (
                        f"MACD drawn on {src}, not the tape")
            return False, why

    if fast <= slow or gap <= 0:
        if isinstance(record, dict):
            record["block_detail"] = f"fast {fast:.4f} <= slow {slow:.4f} (gap {gap:+.4f})"
        return False, "macd_bearish"

    # CONFLUENCE OVERRIDE — the operator's rule, 8/26: "if the MACD is open
    # and trending at ANY gap when EXH is at or past 70, that is an automatic
    # yes." Two independent readings agreeing is the evidence; the size of
    # the gap is not, so this deliberately runs BEFORE macd_min_gap and the
    # separation test and bypasses both.
    #
    # It cannot bypass the bearish check above — "open" means the lines are
    # apart, and a negative gap is not a narrow one. Nor can it collide with
    # the narrowing rule below, because it requires the gap to be RISING:
    # opening and closing are not both true.
    if bool(cfg.get("ai_watch_macd_exh_override", False)):
        try:
            need = float(cfg.get("ai_watch_macd_exh_override_min_pct", 70.0)
                         or 70.0)
        except (TypeError, ValueError):
            need = 70.0
        ex = exhaustion_pct(record)
        # BOTH lines trending up, not just both present. A %R at 85 that is
        # rolling over is a top, not a confirmation — it is the exact reading
        # the operator's original setup called "where the profit gain stops".
        # So the override needs the level AND the turn, on both indicators.
        macd_up = bool(ind.get("macd_gap_rising"))
        exh_up = bool(ind.get("pctr_rising"))
        # Same ceiling problem as exhaustion_allows_buy: %R pinned at the top
        # of its range is flat by construction, never rising, so the override
        # could never fire on the most extended names — the ones it exists
        # for. A pinned reading counts as "up" while it is not FALLING; a top
        # that has rolled over is still excluded, which was the point of
        # requiring the turn in the first place.
        if (not exh_up
                and bool(cfg.get("ai_watch_ob_allow_flat_when_macd_armed", False))
                and exhaustion_state(record, cfg) == "overbought"
                and not ind.get("pctr_falling")):
            exh_up = True
        # OR, not AND — the operator's call on 2026-08-28.
        #
        # Either leg alone now earns the bypass: a MACD gap that is opening,
        # or a %R at or past the threshold and rising. It was written as
        # confluence on the argument that two independent readings agreeing is
        # what justifies skipping macd_min_gap and the separation test.
        #
        # Recording the cost rather than arguing it again: this is the path
        # that took GAP at 13:31:46 today. Its separation was inside the noise
        # band and the position closed 79 seconds later on macd_negative. With
        # OR, a rising gap of any size reaches this branch, so the 1.5x entry
        # bar no longer stands between the desk and that trade — the arm
        # confirmation (ai_watch_arm_confirm_ticks) is what remains, and it
        # only requires the reading to survive, not to be large.
        macd_leg = macd_up
        exh_leg = exh_up and ex is not None and ex >= need
        if macd_leg or exh_leg:
            if isinstance(record, dict):
                _legs = []
                if macd_leg:
                    _legs.append(f"MACD opening {gap:+.4f}")
                if exh_leg:
                    _legs.append(f"EXH {ex:.1f}% rising (>= {need:.0f}%)")
                record["block_detail"] = " or ".join(_legs)
            return True, "macd_exh_confluence"

    try:
        min_gap = float(cfg.get("macd_min_gap", 0.005) or 0.005)
    except (TypeError, ValueError):
        min_gap = 0.005

    if gap < min_gap:
        if isinstance(record, dict):
            record["block_detail"] = f"gap {gap:+.4f} < min {min_gap:.4f}"
        return False, "macd_gap_too_close"

    try:
        sep_mult = float(cfg.get("macd_sep_mult", 0.8) or 0.8)
    except (TypeError, ValueError):
        sep_mult = 0.8

    # "Wide separation" — the rule the strategy is named for. It read
    # macd_hist_std / macd_std, and the engine publishes NEITHER: it computes
    # the rolling std internally and puts the finished quotient on the wire as
    # macd_sep_ratio. So `std` was None on every symbol, the whole check
    # short-circuited, and the live rule was bare `gap >= macd_min_gap` while
    # the doc and the commit title both said "wide gap". Same field-name class
    # of bug as price_age_sec and dollar_volume before it.
    #
    # gap >= sep_mult * std  <=>  (gap / std) >= sep_mult  <=>  ratio >= mult.
    # The raw std path is kept for any producer that does publish it.
    ratio = _f_or_none(ind.get("macd_sep_ratio"))
    std = _f_or_none(ind.get("macd_hist_std") if ind.get("macd_hist_std") is not None else ind.get("macd_std"))
    if sep_mult > 0:
        if ratio is not None:
            if ratio < sep_mult:
                if isinstance(record, dict):
                    record["block_detail"] = (
                        f"sep {ratio:.2f}x < {sep_mult:.1f}x std")
                return False, "macd_gap_insufficient"
        elif std is not None and std > 0:
            if gap < sep_mult * std:
                if isinstance(record, dict):
                    record["block_detail"] = f"gap {gap:+.4f} < {sep_mult:.1f}x std ({sep_mult * std:.4f})"
                return False, "macd_gap_insufficient"
        else:
            # Neither form on the record. The separation test is the strategy,
            # not a garnish, so an unmeasurable one is a refusal rather than a
            # silent pass — which is what it had been doing.
            if isinstance(record, dict):
                record["block_detail"] = "no separation reading (needs 50 bars)"
            return False, "macd_sep_unknown"

    if bool(cfg.get("macd_require_cross", False)):
        if not bool(ind.get("macd_cross")):
            if isinstance(record, dict):
                record["block_detail"] = "no bullish cross in confirm window"
            return False, "macd_no_recent_cross"

    # Is the gap OPENING or CLOSING? Every test above measures the SIZE of the
    # separation; none of them says which way it is going, and a wide gap that
    # is closing is momentum dying. Entering it buys the fade — a +0.03 gap
    # that was +0.08 two bars ago passes every size test on this list.
    #
    # A FLAT gap is allowed through: the rule is "do not open into a closing
    # gap", and flat is not closing. Tightening that to "must be actively
    # widening" is a second, stricter knob, not a reinterpretation of this one.
    #
    # Unknown direction is refused rather than waved through — same rule as
    # everywhere else on this desk, and here it means the name has too few
    # bars for the trend_lookback comparison to mean anything.
    if bool(cfg.get("ai_watch_macd_block_narrowing", False)):
        rising = ind.get("macd_gap_rising")
        falling = ind.get("macd_gap_falling")
        if rising is None and falling is None:
            if isinstance(record, dict):
                record["block_detail"] = "gap direction unknown (needs bars)"
            return False, "macd_gap_dir_unknown"
        if bool(falling):
            prev = _f_or_none(ind.get("macd_gap_prev"))
            if isinstance(record, dict):
                record["block_detail"] = (
                    f"gap closing {prev:+.4f} -> {gap:+.4f}"
                    if prev is not None else f"gap closing (now {gap:+.4f})")
            return False, "macd_gap_narrowing"

    return True, "macd_bullish_gap"


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
    engine drew on the REST fallback rather than the Finnhub tape. The source
    flips per ticker mid-session, so without the check the same gate is
    sometimes reading the live tape and sometimes not, with nothing to say
    which. Mirrors ai_watch_require_live_pctr on the %R side.
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


def exhaustion_allows_buy(record: dict, cfg: dict) -> tuple[bool, str]:
    """Buy side of the exhaustion / momentum gate.

    TV desk mode: both %R lines in the overbought band (red boxes),
    optionally close together, then CM RSI-2 at/under buy_max.

    Legacy: buy when fast %R is **rising**, or the name is already
    **overbought and not falling**. Heat min/max still apply there.

    A missing reading REFUSES under ai_watch_require_exhaustion_data.
    """
    if not bool(cfg.get("ai_watch_exhaustion_rules", True)):
        return True, "exhaustion_off"
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
    state = exhaustion_state(record, cfg)
    if state == "unknown":
        if bool(cfg.get("ai_watch_require_exhaustion_data", True)):
            return False, "no_exhaustion_data"
        if bool(cfg.get("ai_watch_exhaustion_fallback", True)):
            return True, "no_exhaustion_fallback"
        return False, "no_exhaustion_data"
    ind = record.get("indicator") if isinstance(record.get("indicator"), dict) else {}
    if state == "overbought":
        if ind.get("pctr_falling"):
            return False, "not_rising_overbought"
        if bool(cfg.get("ai_watch_ob_allow_hot", True)) and _hot_ob_source(record):
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
        return False, f"not_rising_{state}"
    if state == "overbought":
        return True, "overbought"
    return True, "heating"


def _macd_is_armed(record: dict) -> bool:
    """Bullish AND opening — the direction %R cannot express at 100%.

    Deliberately narrower than macd_allows_buy: no min-gap, no separation
    test, no confluence override. This answers one question — are the lines
    apart and still separating — because it is standing in for a %R turn, not
    re-deciding the entry. macd_allows_buy still runs on its own afterwards.

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


def exhaustion_exit_now(record: dict, cfg: dict) -> tuple[bool, str]:
    """Sell when %R leaves the overbought band (exhaustion_scalp only).

    Disabled under **continuation** (Option A): left_overbought was the
    2026-08-11 small-loss factory (median MFE +0.06R, 8/12 exits). Upside is
    broker T1 / runner trail / dead_trade / stop instead.

    Arms only after the position has actually been overbought. Until then
    there is nothing to exit *out of*.

    Returns (exit_now, reason).
    """
    if not left_overbought_exit_enabled(cfg):
        return False, "left_overbought_off"
    if not bool(cfg.get("ai_watch_exhaustion_rules", True)):
        return False, "exhaustion_off"
    ex = exhaustion_pct(record)
    if ex is None:
        return False, "no_exhaustion_data"
    try:
        thr = float(cfg.get("rte_threshold", 20) or 20)
    except (TypeError, ValueError):
        thr = 20.0
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
    }


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

    # MACD bullish crossover + wide separation gap (primary momentum entry gate)
    if bool(cfg.get("ai_watch_arm_require_macd", False)):
        macd_ok, macd_why = macd_allows_buy(record, cfg)
        if not macd_ok:
            return False, macd_why

    # Exhaustion first so (a) missing %R is named correctly, and (b) the soft
    # sell_signal veto below can reference exh_why without UnboundLocalError.
    exh_ok, exh_why = exhaustion_allows_buy(record, cfg)

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
        fade_ok = (
            bool(cfg.get("ai_watch_in_zone_ignore_fade", False))
            and str(exh_why).startswith("not_rising")
        )
        if last_mode and fade_ok:
            exh_ok, exh_why = True, "in_zone_fade_ok"
        elif fade_ok and in_zone:
            exh_ok, exh_why = True, "in_zone_fade_ok"
        elif zone_win <= 0:
            return False, exh_why
        # zone_win > 0: stay on the name; zone entry starts a wait below.

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
        if not exh_ok:
            return False, exh_why
        return True, f"last_{exh_why}"

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


def _arm_confirm_ticks(cfg: dict | None) -> int:
    """How many consecutive polls must agree before a buy is placed."""
    try:
        return max(1, int((cfg or {}).get("ai_watch_arm_confirm_ticks", 1) or 1))
    except (TypeError, ValueError):
        return 1


def _arm_streak(rec: dict, ok: bool) -> int:
    """Count consecutive arm-YES verdicts on *rec*; any NO resets it.

    Mirrors the exit's confirmation streak. Kept on the record so it survives
    the poll but not a restart, which is the right lifetime: after a restart
    the desk should re-earn its evidence rather than act on a count it cannot
    see the readings behind.
    """
    if not isinstance(rec, dict):
        return 1 if ok else 0
    if not ok:
        rec.pop("arm_streak", None)
        return 0
    n = int(rec.get("arm_streak") or 0) + 1
    rec["arm_streak"] = n
    return n


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
                    rec["last_ask"] = float(tape[0])
                    rec["last_ask_src"] = "stream"
                    rec["last_ask_age_sec"] = float(tape[1])
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
        tape_only = px_src == "stale_tape"
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
            touched[sym] = rec
            continue

        # Any usable quote clears the stale streak — the drop above is for
        # names the feed cannot price at all, not for a quiet minute.
        rec.pop("stale_tape_streak", None)
        # Tape recovered: drop the data-condition refuse so State and ready
        # can recompute. Without this, derive_blocker kept returning the
        # stored stale_quote after a fresh stream print landed.
        if str(rec.get("block_code") or "").strip().lower() in (
            "stale_quote", "no_quote_age", "no_quote",
        ):
            clear_block_reason(rec)

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
        ask_f, px_src, _age_fresh, bid_f = refresh_arm_market_data(
            rec, cfg, t0, gt=gt, sig=indicators.get(sym))
        if ask_f <= 0 or px_src in ("none", "stale_tape"):
            _skip("stale_quote", detail=px_src or "arm_refresh")
            continue
        ok_arm, why = should_arm_buy(rec, ask=ask_f, bid=bid_f, cfg=cfg, now=t0)
        if not ok_arm:
            _arm_streak(rec, False)
            if why in ("wait_setup", "hard_no", "spread", "above_zone",
                       "below_zone", "reward_risk", "no_structure",
                       "late_hold_closed", "late_hold_not_late_admit"):
                _skip(why)
            else:
                set_block_reason(rec, why or "blocked", now=t0)
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
        streak = _arm_streak(rec, True)
        if streak < need_arm:
            set_block_reason(rec, "arm_confirming", now=t0,
                             detail=f"{streak}/{need_arm}")
            touched[sym] = rec
            continue

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
        ask_f, px_src2, _age2, bid2_f = refresh_arm_market_data(
            rec, cfg, t0, gt=gt, sig=indicators.get(sym))
        if ask_f <= 0 or px_src2 in ("none", "stale_tape"):
            _skip("stale_quote", detail=px_src2)
            continue
        if bid2_f is None:
            bid2_f = bid_f
        ok_arm2, why2 = should_arm_buy(rec, ask=ask_f, bid=bid2_f, cfg=cfg, now=t0)
        if not ok_arm2:
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

    merge_watch_records(touched)
    return events
