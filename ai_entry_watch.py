"""Agreement-based session watch queue for AI paper entries.

Research (slow clock) upserts symbols that clear the agreement gate.
A later poller (Task 6) arms/buys from stored structure; this module owns
load/save/upsert/invalidation plus pure zone/spread arming predicates.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_paths import resolve_report_dir  # noqa: E402

REPORT_DIR = resolve_report_dir()
WATCH_STATE_PATH = REPORT_DIR / "entry_watch_state.json"

_EMPTY_RECORD_DEFAULTS: dict[str, Any] = {
    "structure": None,
    "structure_ts": 0.0,
    "last_poll_ts": 0.0,
    "last_ask": None,
}

_ARMABLE_STATUSES = frozenset({"watching", "armed"})


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
        rec: dict[str, Any] = {
            "symbol": sym,
            "status": "watching",
            "agreement": bool(row.get("agreement")),
            "score": _score_from_row(row),
            "reason": str(row.get("reason") or prev.get("reason") or ""),
            "structure": prev.get("structure", _EMPTY_RECORD_DEFAULTS["structure"]),
            "structure_ts": float(
                prev.get("structure_ts", _EMPTY_RECORD_DEFAULTS["structure_ts"]) or 0.0
            ),
            "last_poll_ts": float(
                prev.get("last_poll_ts", _EMPTY_RECORD_DEFAULTS["last_poll_ts"]) or 0.0
            ),
            "last_ask": prev.get("last_ask", _EMPTY_RECORD_DEFAULTS["last_ask"]),
            "updated_ts": float(now),
        }
        # Preserve non-default statuses that should not be clobbered by research
        # only when still actively managed? Spec: create/update with watching.
        # Always set watching on eligible refresh so re-research re-opens queue.
        state[sym] = rec

    save_watch(state)
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


def spread_ok(
    bid: float | None,
    ask: float,
    max_spread_pct: float,
) -> bool:
    """True if bid/ask spread as % of mid is within *max_spread_pct*.

    When *max_spread_pct* <= 0, spread is not enforced (always OK).
    Missing/invalid bid with enforcement on → not OK.
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
        return False
    try:
        b = float(bid)
    except (TypeError, ValueError):
        return False
    if b <= 0 or a < b:
        return False
    mid = (a + b) / 2.0
    if mid <= 0:
        return False
    spr = 100.0 * (a - b) / mid
    return spr <= msp + 1e-12


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


def should_arm_buy(
    record: dict,
    *,
    ask: float,
    bid: float | None,
    cfg: dict,
) -> tuple[bool, str]:
    """Whether a watch record may auto-arm a paper buy at *ask*.

    Returns ``(True, "zone")`` when armable, else ``(False, reason)`` where
    reason is one of: ``not_watching``, ``no_structure``, ``hard_no``,
    ``wait_setup``, ``spread``, ``above_zone``, ``below_zone``, ``reward_risk``.
    """
    if not isinstance(record, dict):
        return False, "not_watching"
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
    try:
        min_rr = float(cfg.get("ai_min_reward_risk", 0) or 0)
    except (TypeError, ValueError):
        min_rr = 0.0
    if min_rr > 0 and rr + 1e-12 < min_rr:
        return False, "reward_risk"

    try:
        max_spread = float(cfg.get("ai_max_spread_pct", 1.0) or 0.0)
    except (TypeError, ValueError):
        max_spread = 1.0
    if not spread_ok(bid, ask, max_spread):
        return False, "spread"

    try:
        pad = float(cfg.get("ai_entry_zone_pad_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        pad = 0.0

    try:
        a = float(ask)
    except (TypeError, ValueError):
        return False, "below_zone"

    if ask_in_zone(a, entry_low, entry_high, pad):
        return True, "zone"

    frac = max(0.0, pad) / 100.0
    high_bound = max(entry_low, entry_high) * (1.0 + frac)
    if a > high_bound:
        return False, "above_zone"
    return False, "below_zone"
