"""Phase B ledger — arm/limit/fill/exit/flatten rows (observe + dry).

Append-only day-split JSONL. Fail-open: write errors never raise into trading.

Day files::

    ai_reports/phase_b_ledger/YYYY-MM-DD.jsonl   # ET calendar day

Score only rows tagged ``phase_b`` / this ledger — never mix into RTH Phase 1/2.
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
_PATH_OVERRIDE: Path | None = None


def _report_dir() -> Path:
    try:
        from ai_paths import resolve_report_dir
        return resolve_report_dir()
    except Exception:
        return Path(__file__).resolve().parent / "ai_reports"


def ledger_path_for_ts(ts: float | None = None) -> Path:
    if _PATH_OVERRIDE is not None:
        return _PATH_OVERRIDE
    t = float(ts if ts is not None else time.time())
    day = datetime.fromtimestamp(t, tz=ET).strftime("%Y-%m-%d")
    return _report_dir() / "phase_b_ledger" / f"{day}.jsonl"


def set_ledger_path_for_tests(path: Path | None) -> None:
    global _PATH_OVERRIDE
    _PATH_OVERRIDE = path


def _enabled(cfg: dict | None) -> bool:
    c = cfg if isinstance(cfg, dict) else {}
    if "ai_phase_b_ledger_enabled" in c:
        return bool(c.get("ai_phase_b_ledger_enabled"))
    try:
        from config import load_config
        return bool((load_config() or {}).get("ai_phase_b_ledger_enabled", True))
    except Exception:
        return True


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def log_event(
    kind: str,
    *,
    symbol: str | None = None,
    reason: str | None = None,
    ts: float | None = None,
    cfg: dict | None = None,
    **fields: Any,
) -> bool:
    """Append one Phase B ledger row. Never raises. Returns False on skip/error."""
    try:
        if not _enabled(cfg):
            return True
        t = float(ts if ts is not None else time.time())
        day = datetime.fromtimestamp(t, tz=ET).strftime("%Y-%m-%d")
        out: dict[str, Any] = {
            "ts": round(t, 3),
            "day": day,
            "kind": str(kind or "event").strip().lower() or "event",
            "phase_b": True,
        }
        sym = str(symbol or "").upper().strip()
        if sym:
            out["symbol"] = sym
        if reason:
            out["reason"] = str(reason)
        for k, v in fields.items():
            if v is None:
                continue
            if isinstance(v, float):
                out[k] = round(v, 6) if abs(v) < 1e6 else v
            else:
                out[k] = v
        # Normalize a few numeric fields when present.
        for nk in ("last", "limit_px", "fill_px", "entry", "exit_px",
                   "entry_slip_pct", "mfe_r", "mfe_pct", "realized_r", "pl_pct"):
            if nk in out:
                fv = _f(out[nk])
                if fv is not None:
                    out[nk] = round(fv, 6)

        path = ledger_path_for_ts(t)
        line = json.dumps(out, default=str) + "\n"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
        return True
    except Exception:
        return False


def read_day(day: str | None = None, path: Path | None = None) -> list[dict]:
    """Load ledger rows for an ET day (or an explicit path)."""
    rows: list[dict] = []
    try:
        if path is not None:
            p = Path(path)
        else:
            d = day or datetime.now(tz=ET).strftime("%Y-%m-%d")
            p = _report_dir() / "phase_b_ledger" / f"{d}.jsonl"
        if not p.exists():
            return rows
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except Exception:
        return rows
    return rows


__all__ = [
    "ledger_path_for_ts",
    "log_event",
    "read_day",
    "set_ledger_path_for_tests",
]
