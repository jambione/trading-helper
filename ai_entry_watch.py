"""Agreement-based session watch queue for AI paper entries.

Research (slow clock) upserts symbols that clear the agreement gate.
A later poller (Task 6) arms/buys from stored structure; this module only
owns load/save/upsert/invalidation of queue state.
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
