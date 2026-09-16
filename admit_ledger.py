"""Admit ledger — refused seed + inclusion candidates (observe-only).

Append-only day-split JSONL of every refused candidate at seed and inclusion
stages (uncapped). Fail-open: write errors never raise into trading / sync.

Does **not** change keep/arm/exit. Day files::

    ai_reports/admit_ledger/YYYY-MM-DD.jsonl   # ET calendar day

Gate grading (select / neutral / invert) comes after ≥1–2 RTH days of rows;
occupancy churn remains the weekly primary.
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
    return _report_dir() / "admit_ledger" / f"{day}.jsonl"


def set_ledger_path_for_tests(path: Path | None) -> None:
    """Point writes at a temp file (tests). ``None`` restores day-split."""
    global _PATH_OVERRIDE
    _PATH_OVERRIDE = path


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


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


def _pick_features(row: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    """Compact features from row/extra; omit nulls. Prefer canonical names."""
    src: dict[str, Any] = {}
    if isinstance(row, dict):
        src.update(row)
    if isinstance(extra, dict):
        src.update(extra)

    out: dict[str, Any] = {}

    price = _f(src.get("price"))
    if price is not None:
        out["price"] = price

    pct = _f(src.get("pct"))
    if pct is None:
        pct = _f(src.get("pct_change"))
    if pct is not None:
        out["pct"] = pct

    rvol = _f(src.get("rvol"))
    if rvol is not None:
        out["rvol"] = rvol

    score = _f(src.get("score"))
    if score is not None:
        out["score"] = score

    float_m = _f(src.get("float_m"))
    if float_m is None:
        float_m = _f(src.get("float"))
    if float_m is not None:
        out["float_m"] = float_m

    page = _f(src.get("price_age_sec"))
    if page is None:
        page = _f(src.get("rt_price_age_sec"))
    if page is not None:
        out["price_age_sec"] = page

    lage = _f(src.get("last_ask_age_sec"))
    if lage is not None:
        out["last_ask_age_sec"] = lage

    crit = src.get("criteria")
    if crit is not None:
        if isinstance(crit, (list, tuple)):
            out["criteria"] = [str(c) for c in crit if c is not None]
        else:
            out["criteria"] = crit

    return out


def _enabled(cfg: dict[str, Any] | None, stage: str) -> bool:
    c = cfg if isinstance(cfg, dict) else {}
    if not bool(c.get("ai_admit_ledger_enabled", True)):
        return False
    # Reserved for paired kept=true samples (v1 off). Read so SAFE_CONFIG_KEYS
    # stays live; writing kept rows is intentionally not implemented yet.
    _ = bool(c.get("ai_admit_ledger_kept_sample", False))
    if stage == "seed":
        return bool(c.get("ai_admit_ledger_seed", True))
    if stage == "inclusion":
        return bool(c.get("ai_admit_ledger_inclusion", True))
    return False


def log_refuse(
    *,
    stage: str,
    symbol: str,
    reason: str,
    source: str | None = None,
    row: dict | None = None,
    extra: dict | None = None,
    ts: float | None = None,
    cfg: dict | None = None,
) -> bool:
    """Append one refuse row. Never raises. Returns False on skip/error."""
    try:
        stage_s = str(stage or "").strip().lower()
        if stage_s not in ("seed", "inclusion"):
            return False
        if not _enabled(cfg, stage_s):
            return True  # disabled = intentional no-op success

        sym = str(symbol or "").upper().strip()
        if not sym:
            return False
        why = str(reason or "").strip() or "unknown"
        t = float(ts if ts is not None else time.time())
        day = datetime.fromtimestamp(t, tz=ET).strftime("%Y-%m-%d")

        out: dict[str, Any] = {
            "ts": round(t, 2),
            "day": day,
            "stage": stage_s,
            "symbol": sym,
            "reason": why,
            "kept": False,
        }
        src = str(source or "").strip().lower()
        if not src and isinstance(row, dict):
            src = str(row.get("source") or "").strip().lower()
        if src:
            out["source"] = src

        out.update(_pick_features(row, extra))
        out.update(_regime_fields())

        path = ledger_path_for_ts(t)
        line = json.dumps(out, default=str) + "\n"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
        return True
    except Exception:
        return False


__all__ = [
    "ledger_path_for_ts",
    "log_refuse",
    "set_ledger_path_for_tests",
]
