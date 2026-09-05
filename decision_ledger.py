"""Decision ledger — one JSONL row per watch-poll (and entry/trail/exit events).

Package 1 (observe-only). Fail-open: write errors never raise into trading.

**Primary hook:** ``ai_positions.log_shadow_sample`` — densest existing path
(one row per watched symbol per ``poll_once``). Also mirrors selected
``log_event`` kinds (entry_ok / entry_fail / local_trail / flat-class exits).

Day-split files: ``ai_reports/decision_ledger/YYYY-MM-DD.jsonl`` (ET date).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from desk_arm_buckets import arm_bucket

ET = ZoneInfo("America/New_York")

# Event kinds from log_event that also get a ledger row (stage set below).
_EVENT_STAGE: dict[str, str] = {
    "entry_ok": "entry",
    "entry_fail": "entry",
    "local_trail": "trail",
    "local_trail_working": "trail",
    "desk_flatten": "exit",
    "unprotected_flatten": "exit",
    "arm_recheck": "arm",
}

_lock = threading.Lock()
# Test override — when set, all writes go here (single file, no day-split).
_PATH_OVERRIDE: Path | None = None


def _report_dir() -> Path:
    try:
        from ai_paths import resolve_report_dir
        return resolve_report_dir()
    except Exception:
        return Path(__file__).resolve().parent / "ai_reports"


def ledger_path_for_ts(ts: float | None = None) -> Path:
    """Day-split path for *ts* (unix). Uses ET calendar date."""
    if _PATH_OVERRIDE is not None:
        return _PATH_OVERRIDE
    t = float(ts if ts is not None else time.time())
    day = datetime.fromtimestamp(t, tz=ET).strftime("%Y-%m-%d")
    return _report_dir() / "decision_ledger" / f"{day}.jsonl"


def set_ledger_path_for_tests(path: Path | None) -> None:
    """Point writes at a temp file (tests). ``None`` restores day-split."""
    global _PATH_OVERRIDE
    _PATH_OVERRIDE = path


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _b(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return bool(v)


def _regime_fields() -> dict[str, Any]:
    try:
        from learn_stamps import regime_stamp
        st = regime_stamp()
        return {
            "git_version": st.get("git_version"),
            "config_fp": st.get("config_fp"),
        }
    except Exception:
        return {"git_version": None, "config_fp": None}


def _macd_flags(row: dict[str, Any], why: str | None) -> tuple[bool | None, bool | None]:
    """(macd_narrowing, macd_bearish) from indicator fields or arm_why."""
    falling = row.get("macd_gap_falling")
    rising = row.get("macd_gap_rising")
    narrowing: bool | None
    if falling is None and rising is None:
        narrowing = None
    else:
        narrowing = bool(falling)
    bearish: bool | None = None
    gap = _f(row.get("macd_gap") if row.get("macd_gap") is not None else row.get("macd_hist"))
    bull = row.get("macd_bull")
    if bull is not None:
        bearish = not bool(bull)
    elif gap is not None:
        bearish = gap <= 0
    w = str(why or "").strip().lower()
    if w == "macd_gap_narrowing":
        narrowing = True
    if w == "macd_bearish":
        bearish = True
    return narrowing, bearish


def row_from_shadow(sample: dict[str, Any]) -> dict[str, Any]:
    """Build a ledger row from a shadow sample (watch-poll hook)."""
    s = sample if isinstance(sample, dict) else {}
    why = s.get("arm_why")
    why_s = str(why).strip() if why is not None else None
    if why_s == "":
        why_s = None
    arm_ok = s.get("arm_ok")
    if arm_ok is not None:
        arm_ok = bool(arm_ok)
    narrowing, bearish = _macd_flags(s, why_s)
    tape_src = (
        s.get("last_ask_src")
        or s.get("price_src")
        or None
    )
    tape_age = _f(s.get("last_ask_age_sec"))
    if tape_age is None:
        tape_age = _f(s.get("tape_age_sec"))
    peak = _f(s.get("arm_confirm_rsi_max"))
    if peak is None:
        peak = _f(s.get("cm_rsi_peak"))
    stage = "watch"
    # Shadow rows with a real arm verdict are the arm gate sample.
    if arm_ok is not None or why_s:
        stage = "arm"
    if arm_ok is True:
        bucket = None
    elif why_s or arm_ok is False:
        bucket = arm_bucket(why_s)
    else:
        bucket = None
    out = {
        "ts": _f(s.get("ts")) if s.get("ts") is not None else time.time(),
        "symbol": str(s.get("symbol") or "").upper() or None,
        "stage": stage,
        "tape_src": str(tape_src).strip().lower() if tape_src else None,
        "tape_age_sec": tape_age,
        "exh_state": s.get("exhaustion_state"),
        "pctr_rising": _b(s.get("pctr_rising")),
        "cm_rsi": _f(s.get("cm_rsi")),
        "cm_rsi_peak": peak,
        "macd_narrowing": narrowing,
        "macd_bearish": bearish,
        "arm_ok": arm_ok,
        "arm_why": why_s,
        "arm_bucket": bucket,
    }
    out.update(_regime_fields())
    return out


def row_from_event(kind: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Build a ledger row from a ``log_event`` kind, or None if not mirrored."""
    stage = _EVENT_STAGE.get(str(kind or "").strip())
    if stage is None:
        return None
    f = fields if isinstance(fields, dict) else {}
    why = f.get("why") or f.get("reason") or f.get("arm_why")
    why_s = str(why).strip() if why is not None else None
    if why_s == "":
        why_s = None
    arm_ok: bool | None
    if kind == "entry_ok":
        arm_ok = True
    elif kind == "entry_fail":
        arm_ok = False
    elif kind == "arm_recheck":
        arm_ok = bool(f.get("ok")) if f.get("ok") is not None else None
    else:
        arm_ok = None
    narrowing, bearish = _macd_flags(f, why_s)
    out = {
        "ts": _f(f.get("ts")) if f.get("ts") is not None else time.time(),
        "symbol": str(f.get("symbol") or f.get("ticker") or "").upper() or None,
        "stage": stage,
        "tape_src": (
            str(f.get("px_src") or f.get("last_ask_src") or f.get("tape_src") or "")
            .strip().lower()
            or None
        ),
        "tape_age_sec": _f(f.get("tape_age_sec") or f.get("last_ask_age_sec")),
        "exh_state": f.get("exhaustion_state") or f.get("exh_state"),
        "pctr_rising": _b(f.get("pctr_rising")),
        "cm_rsi": _f(f.get("cm_rsi")),
        "cm_rsi_peak": _f(f.get("cm_rsi_peak") or f.get("arm_confirm_rsi_max")),
        "macd_narrowing": narrowing,
        "macd_bearish": bearish,
        "arm_ok": arm_ok,
        "arm_why": why_s,
        "arm_bucket": arm_bucket(why_s) if why_s else None,
        "event_kind": str(kind),
    }
    if stage == "arm" and arm_ok is False and not why_s:
        out["arm_bucket"] = "other"
    out.update(_regime_fields())
    return out


def append_row(row: dict[str, Any]) -> bool:
    """Append one ledger row. Returns True on success. Never raises."""
    try:
        if not isinstance(row, dict) or not row.get("symbol"):
            return False
        ts = _f(row.get("ts")) or time.time()
        row = dict(row)
        row["ts"] = round(float(ts), 2)
        path = ledger_path_for_ts(ts)
        line = json.dumps(row, default=str) + "\n"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
        return True
    except Exception:
        return False


def log_from_shadow(sample: dict[str, Any]) -> bool:
    """Shadow-poll hook. Fail-open."""
    try:
        return append_row(row_from_shadow(sample))
    except Exception:
        return False


def log_from_event(kind: str, **fields: Any) -> bool:
    """Selected log_event kinds. Fail-open."""
    try:
        row = row_from_event(kind, fields)
        if row is None:
            return False
        return append_row(row)
    except Exception:
        return False


__all__ = [
    "append_row",
    "ledger_path_for_ts",
    "log_from_event",
    "log_from_shadow",
    "row_from_event",
    "row_from_shadow",
    "set_ledger_path_for_tests",
]
