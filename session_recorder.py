"""session_recorder — append-only per-day JSONL.gz of desk inputs (no secrets).

Design: docs/SESSION_RECORDER_DESIGN.md. Files live under
``ai_reports/sessions/YYYY-MM-DD/`` (ai_reports is gitignored).

Streams:
  prints.jsonl.gz       every published price update (source, symbol, price, ts)
  quotes_meta.jsonl.gz  Alpaca request batches / 429s / failures
  sources.jsonl.gz      candidate-pool enter/leave per (symbol, source), with
                        the pct / rvol / price the row carried at that moment
  config.jsonl.gz       full bot_config (secrets stripped) + git SHA, written
                        at start and on every change
  inputs.jsonl.gz       external-data values the desk computed (SIP spread,
                        volume pace, open gap), so a replay needs no fetches
  discord.jsonl.gz      every Discord alert as it arrived (ticker, alert
                        line, flags, the desk's price and its age then)

Low overhead: buffered writes, fail-open, no extra API calls. Cap the symbol
set to book ∪ sources ∪ positions when a filter is installed by the caller.
"""
from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
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
_source_state: dict[str, Any] = {"day": None, "keys": {}}
_config_state: dict[str, Any] = {"digest": None}


def _day_et(ts: float | None = None) -> str:
    return datetime.fromtimestamp(
        float(ts if ts is not None else time.time()), tz=ET
    ).strftime("%Y-%m-%d")


def session_dir(day: str | None = None) -> Path:
    # Follow AI_REPORT_DIR like every other report writer, so a test run can
    # not append fixture rows to the live day's recording.
    try:
        import ai_paths
        base = ai_paths.resolve_report_dir() / "sessions"
    except Exception:  # noqa: BLE001
        base = _DEFAULT_DIR
    return base / (day or _day_et())


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
        need = (_buf_bytes[key] >= _FLUSH_BYTES
                or time.monotonic() - _last_flush_mono >= _FLUSH_EVERY_SEC)
    if need:
        flush()


def flush(*, day: str | None = None, force: bool = False) -> None:
    """Write buffered lines to gzip files. Safe to call from any thread.

    Each write holds an exclusive flock on the file: several processes append
    to quotes_meta, and two interleaved gzip members corrupt the file from
    that point on.
    """
    global _last_flush_mono
    with _lock:
        keys = [k for k in _buffers if day is None or k[0] == day]
        items = [(k, _buffers.pop(k)) for k in keys]
        for k in keys:
            _buf_bytes.pop(k, None)
        _last_flush_mono = time.monotonic()
    for (d, stream), lines in items:
        if not lines:
            continue
        try:
            out_dir = session_dir(d)
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{stream}.jsonl.gz"
            payload = gzip.compress("".join(lines).encode("utf-8"))
            with open(path, "ab") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(payload)
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
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
    event: str = "enter",
) -> None:
    if not _allow_sym(symbol):
        return
    t = float(ts if ts is not None else time.time())
    obj = {
        "ts": t,
        "et": datetime.fromtimestamp(t, ET).strftime("%H:%M:%S"),
        "symbol": str(symbol).upper(),
        "source": str(source or ""),
        "event": str(event),
    }
    for k, v in (("pct", pct), ("rvol", rvol), ("price", price)):
        if v is None:
            continue
        try:
            obj[k] = float(v)
        except (TypeError, ValueError):
            pass
    _append("sources", obj)


def record_input(kind: str, symbol: str, value, *, ts: float | None = None,
                 **extra) -> None:
    """One computed input (kind: sip_spread / rvol_pace / open_gap)."""
    if not _allow_sym(symbol):
        return
    t = float(ts if ts is not None else time.time())
    obj = {"ts": t, "kind": str(kind), "symbol": str(symbol).upper(), "value": value}
    obj.update({k: v for k, v in extra.items() if v is not None})
    _append("inputs", obj)


def record_discord(symbol: str, line: str, *, alert: dict | None = None,
                   price=None, age_sec=None, ts: float | None = None) -> None:
    """One Discord alert, at arrival (dashboard ingest)."""
    t = float(ts if ts is not None else time.time())
    obj = {"ts": t, "et": datetime.fromtimestamp(t, ET).strftime("%H:%M:%S"),
           "symbol": str(symbol).upper(), "line": str(line or "")[:300]}
    for k in ("burst", "card_brand", "header", "channel", "author", "kind"):
        if isinstance(alert, dict) and alert.get(k) not in (None, ""):
            obj[k] = alert.get(k)
    for k, v in (("price", price), ("age_sec", age_sec)):
        try:
            if v is not None:
                obj[k] = float(v)
        except (TypeError, ValueError):
            pass
    _append("discord", obj)


def record_source_set(rows: list[dict], *, ts: float | None = None,
                      note: dict | None = None) -> None:
    """Log (symbol, source) pairs entering and leaving the candidate pool.

    Called every book rebuild (~2s); only transitions are written, so the
    stream is small and a replay can rebuild who was nominated when. A
    process restart re-logs the whole pool as ``enter``.
    """
    if not _enabled:
        return
    t = float(ts if ts is not None else time.time())
    day = _day_et(t)
    if _source_state["day"] != day:
        _source_state["day"], _source_state["keys"] = day, {}
    prev: dict[str, tuple[str, str]] = _source_state["keys"]
    cur: dict[str, tuple[str, str]] = {}
    n_in = n_out = 0
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
        src = str(r.get("source") or r.get("src") or "").strip().lower()
        if not sym:
            continue
        k = f"{sym}|{src}"
        if k in cur:
            continue
        cur[k] = (sym, src)
        if k not in prev:
            n_in += 1
            pct = r.get("pct_change")
            record_source(sym, src, ts=t, event="enter",
                          pct=pct if pct is not None else r.get("pct"),
                          rvol=r.get("rvol"), price=r.get("price"))
    for k, (sym, src) in prev.items():
        if k not in cur:
            n_out += 1
            record_source(sym, src, ts=t, event="leave")
    if note and (n_in or n_out):
        # Who built this pool and what it saw, whenever the pool changes.
        _append("sources", {
            "ts": t, "et": datetime.fromtimestamp(t, ET).strftime("%H:%M:%S"),
            "event": "pool_note", "n_enter": n_in, "n_leave": n_out,
            "pool_n": len(cur),
            **{k: v for k, v in note.items() if isinstance(v, (str, int, float, bool)) or v is None}},
            day=day)
    _source_state["keys"] = cur


def record_config_snap(cfg: dict, *, git_sha: str = "", fingerprint: str = "") -> None:
    """Append the config (secrets stripped) when it differs from the last one."""
    if not _enabled or not isinstance(cfg, dict):
        return
    try:
        safe = {
            k: v for k, v in cfg.items()
            if not any(s in str(k).lower() for s in (
                "secret", "key", "token", "password", "webhook", "auth", "cred"))
        }
        digest = hashlib.sha256(
            json.dumps(safe, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:12]
        if digest == _config_state["digest"]:
            return
        _config_state["digest"] = digest
        t = time.time()
        _append("config", {
            "ts": t,
            "et": datetime.fromtimestamp(t, ET).strftime("%H:%M:%S"),
            "process": f"{os.getpid()}:{os.path.basename(sys_argv0())}",
            "git_sha": git_sha,
            "fingerprint": fingerprint,
            "digest": digest,
            "config": safe,
        })
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
