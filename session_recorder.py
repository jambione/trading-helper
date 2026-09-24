"""session_recorder — append-only per-day JSONL.gz of desk inputs (no secrets).

Design: docs/SESSION_RECORDER_DESIGN.md. Files live under
``ai_reports/sessions/YYYY-MM-DD/`` (ai_reports is gitignored).

Streams (night-1):
  prints.jsonl.gz       every published price update (source, symbol, price, ts)
  quotes_meta.jsonl.gz  Alpaca request batches / 429s / failures
  sources.jsonl.gz      movers/trending/research nominations
  config_snap.json      bot_config + git SHA fingerprint (rare)

Low overhead: buffered writes, fail-open, no extra API calls. Cap the symbol
set to book ∪ sources ∪ positions when a filter is installed by the caller.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("session_recorder")

ET = ZoneInfo("America/New_York")
_ROOT = Path(__file__).resolve().parent
_DEFAULT_DIR = _ROOT / "ai_reports" / "sessions"

_lock = threading.Lock()
_buffers: dict[tuple[str, str], list[str]] = {}
_buf_bytes: dict[tuple[str, str], int] = {}
_FLUSH_BYTES = 64_000
_FLUSH_EVERY_SEC = 5.0
_last_flush_mono = 0.0
_enabled = True
_session_syms: set[str] | None = None  # None = record all


def _day_et(ts: float | None = None) -> str:
    return datetime.fromtimestamp(
        float(ts if ts is not None else time.time()), tz=ET
    ).strftime("%Y-%m-%d")


def session_dir(day: str | None = None) -> Path:
    d = _DEFAULT_DIR / (day or _day_et())
    return d


def set_enabled(flag: bool) -> None:
    global _enabled
    _enabled = bool(flag)


def set_symbol_filter(symbols: set[str] | None) -> None:
    """None = record every symbol; a set restricts prints/sources to that universe."""
    global _session_syms
    _session_syms = {str(s).upper() for s in symbols} if symbols is not None else None


def _allow_sym(symbol: str | None) -> bool:
    if _session_syms is None:
        return True
    if not symbol:
        return True
    return str(symbol).upper() in _session_syms


def _append(stream: str, obj: dict, *, day: str | None = None) -> None:
    if not _enabled:
        return
    try:
        day = day or _day_et(obj.get("ts"))
        line = json.dumps(obj, separators=(",", ":"), default=str) + "\n"
    except Exception:  # noqa: BLE001
        return
    key = (day, stream)
    with _lock:
        buf = _buffers.setdefault(key, [])
        buf.append(line)
        _buf_bytes[key] = _buf_bytes.get(key, 0) + len(line)
        need = _buf_bytes[key] >= _FLUSH_BYTES
    if need:
        flush(day=day)


def flush(*, day: str | None = None, force: bool = False) -> None:
    """Write buffered lines to gzip files. Safe to call from any thread."""
    global _last_flush_mono
    now_m = time.monotonic()
    if not force and (now_m - _last_flush_mono) < _FLUSH_EVERY_SEC:
        # Still flush if any buffer is large (caller already checked bytes).
        pass
    with _lock:
        items = list(_buffers.items())
        if day is not None:
            items = [((d, s), lines) for (d, s), lines in items if d == day]
        _buffers.clear()
        _buf_bytes.clear()
        _last_flush_mono = now_m
    for (d, stream), lines in items:
        if not lines:
            continue
        try:
            out_dir = session_dir(d)
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{stream}.jsonl.gz"
            with gzip.open(path, "ab") as f:
                for line in lines:
                    f.write(line.encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            log.debug("[RECORDER] flush %s/%s failed: %s", d, stream, e)


def record_print(
    symbol: str,
    price: float,
    *,
    src: str,
    ts: float | None = None,
    age_sec: float | None = None,
    trade_ts: float | None = None,
) -> None:
    if not _allow_sym(symbol):
        return
    try:
        px = float(price)
    except (TypeError, ValueError):
        return
    if px <= 0:
        return
    t = float(ts if ts is not None else time.time())
    obj = {
        "ts": t,
        "et": datetime.fromtimestamp(t, ET).strftime("%H:%M:%S"),
        "symbol": str(symbol).upper(),
        "price": px,
        "src": str(src or ""),
    }
    if age_sec is not None:
        try:
            obj["age_sec"] = float(age_sec)
        except (TypeError, ValueError):
            pass
    if trade_ts is not None:
        try:
            obj["trade_ts"] = float(trade_ts)
        except (TypeError, ValueError):
            pass
    _append("prints", obj)


def record_quotes_meta(
    *,
    kind: str,
    n_symbols: int = 0,
    ok: int = 0,
    n_429: int = 0,
    empty: int = 0,
    detail: str = "",
    process: str | None = None,
    ts: float | None = None,
) -> None:
    t = float(ts if ts is not None else time.time())
    proc = process or f"{os.getpid()}:{os.path.basename(sys_argv0())}"
    obj = {
        "ts": t,
        "et": datetime.fromtimestamp(t, ET).strftime("%H:%M:%S"),
        "process": proc,
        "kind": str(kind),
        "n_symbols": int(n_symbols),
        "ok": int(ok),
        "n_429": int(n_429),
        "empty": int(empty),
    }
    if detail:
        obj["detail"] = str(detail)[:200]
    _append("quotes_meta", obj)


def record_source(
    symbol: str,
    source: str,
    *,
    ts: float | None = None,
    pct: float | None = None,
    rvol: float | None = None,
    price: float | None = None,
) -> None:
    if not _allow_sym(symbol):
        return
    t = float(ts if ts is not None else time.time())
    obj = {
        "ts": t,
        "et": datetime.fromtimestamp(t, ET).strftime("%H:%M:%S"),
        "symbol": str(symbol).upper(),
        "source": str(source or ""),
    }
    for k, v in (("pct", pct), ("rvol", rvol), ("price", price)):
        if v is None:
            continue
        try:
            obj[k] = float(v)
        except (TypeError, ValueError):
            pass
    _append("sources", obj)


def record_config_snap(cfg: dict, *, git_sha: str = "", fingerprint: str = "") -> None:
    """Write a one-shot config snapshot (not gzipped; rare)."""
    if not _enabled or not isinstance(cfg, dict):
        return
    try:
        day = _day_et()
        out_dir = session_dir(day)
        out_dir.mkdir(parents=True, exist_ok=True)
        # Strip anything that looks like a secret.
        safe = {
            k: v for k, v in cfg.items()
            if not any(s in str(k).lower() for s in ("secret", "key", "token", "password"))
        }
        payload = {
            "ts": time.time(),
            "git_sha": git_sha,
            "fingerprint": fingerprint,
            "config": safe,
        }
        path = out_dir / "config_snap.json"
        path.write_text(json.dumps(payload, indent=1, default=str) + "\n")
    except Exception as e:  # noqa: BLE001
        log.debug("[RECORDER] config_snap failed: %s", e)


def sys_argv0() -> str:
    try:
        return sys.argv[0] or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


# Flush on process exit.
try:
    import atexit
    atexit.register(lambda: flush(force=True))
except Exception:  # noqa: BLE001
    pass
