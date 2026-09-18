"""Fill ledger — broker-confirmed fills, append-only (audit truth).

``alpaca_trade_log.json`` is a **submission** log. Every one of its rows carries
``OrderStatus.PENDING_NEW``, it is capped at 1000 rolling rows, and it is
rewritten whole on every append — so it loses history, it cannot say what
filled or at what price, and a unit-test run can (and did) write MagicMock rows
into it. On 2026-09-16 a replay of that log showed 4 names still open while the
broker reported none. The log was the wrong one.

This module is the audit record instead. Two event kinds, both append-only and
never mutated:

    submit  — what the desk SENT (intent + order id), mirrored out of
              ``alpaca_trader._log_action`` so every order path is covered
    fill    — what the broker SAYS happened, written by :func:`poll_fills`
              from ``alpaca_trader.get_filled_orders``

Day files, split on the ET calendar day of the **fill** (not the poll)::

    ai_reports/fills/YYYY-MM-DD.jsonl

Rows are never edited in place. A partial fill followed by a complete fill
writes two rows; :func:`fold_orders` collapses the stream to current truth.
Re-polling an unchanged order appends nothing — see :func:`_fingerprint`.

Fail-open: write errors never raise into the trading path. The one deliberate
exception is :func:`_guard_path`, which raises under pytest when a write would
land on a production path — that is how the test suite is kept out of the live
ledger.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

_REPO_ROOT = Path(__file__).resolve().parent

_lock = threading.Lock()

# Test override — when set, all writes go here (single file, no day-split).
_PATH_OVERRIDE: Path | None = None

# Dedupe sets, one per day file, loaded lazily from disk so a restart mid
# session does not re-append fills already on record.
_seen: dict[Path, set[str]] = {}

# Poll pacing (module-level so the desk loop can call every tick cheaply).
_last_poll_mono: float = 0.0


# ── Paths ─────────────────────────────────────────────────────────────────────

def _report_dir() -> Path:
    try:
        from ai_paths import resolve_report_dir
        return resolve_report_dir()
    except Exception:
        return _REPO_ROOT / "ai_reports"


def _guard_path(path: Path) -> None:
    """Under pytest, refuse a write that would land inside the repo.

    Raising here is the point. ``alpaca_trade_log.json`` carries 18 SELL rows
    whose ``order_status`` is ``<MagicMock name='mock.close_position().status'>``
    — unit-test runs wrote into the production log, and nothing complained. A
    fail-open ledger would inherit exactly that bug.

    Tests get a real path two ways: :func:`set_ledger_path_for_tests`, or the
    ``AI_REPORT_DIR`` env override that already redirects the whole report tree.
    Either one moves the path out of the repo and this guard goes quiet. No
    production process sets ``PYTEST_CURRENT_TEST``, so it cannot fire live.
    """
    if _PATH_OVERRIDE is not None:
        return
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        resolved = path.resolve()
    except OSError:
        return
    if _REPO_ROOT == resolved or _REPO_ROOT in resolved.parents:
        raise RuntimeError(
            f"fill_ledger refused a production path under pytest: {resolved}. "
            "Use fill_ledger.set_ledger_path_for_tests(tmp_path / 'fills.jsonl') "
            "or set AI_REPORT_DIR to a temp directory."
        )


def ledger_path_for_ts(ts: float | None = None) -> Path:
    """Day-split path for *ts* (unix). Uses the ET calendar date."""
    if _PATH_OVERRIDE is not None:
        return _PATH_OVERRIDE
    t = float(ts if ts is not None else time.time())
    day = datetime.fromtimestamp(t, tz=ET).strftime("%Y-%m-%d")
    return _report_dir() / "fills" / f"{day}.jsonl"


def ledger_path_for_day(day: str) -> Path:
    """Day-split path for an ET date string ``YYYY-MM-DD``."""
    if _PATH_OVERRIDE is not None:
        return _PATH_OVERRIDE
    return _report_dir() / "fills" / f"{day!s}.jsonl"


def set_ledger_path_for_tests(path: Path | None) -> None:
    """Point writes at a temp file (tests). ``None`` restores day-split."""
    global _PATH_OVERRIDE
    _PATH_OVERRIDE = path
    with _lock:
        _seen.clear()


# ── Small helpers ─────────────────────────────────────────────────────────────

def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _s(v: Any) -> str:
    """Enum-safe string: ``OrderStatus.FILLED`` -> ``filled``."""
    if v is None:
        return ""
    out = str(v).strip()
    if "." in out and " " not in out:
        out = out.rsplit(".", 1)[-1]
    return out.lower()


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


def _enabled(cfg: dict[str, Any] | None) -> bool:
    c = cfg if isinstance(cfg, dict) else None
    if c is None:
        try:
            from config import load_config
            c = load_config()
        except Exception:
            c = {}
    return bool(c.get("ai_fill_ledger_enabled", True))


def _parse_ts(raw: Any) -> float | None:
    """Unix seconds from an Alpaca timestamp string, or None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text or text.lower() in ("none", "null"):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _day_for_fill(row: dict[str, Any], fallback_ts: float) -> tuple[str, float]:
    """(ET day, unix ts) for a fill row — keyed on ``filled_at`` when present.

    A fill printed at 15:59 ET and polled after midnight belongs to the session
    it happened in, not the one the poll woke up in.
    """
    t = _parse_ts(row.get("filled_at")) or _parse_ts(row.get("submitted_at"))
    if t is None:
        t = float(fallback_ts)
    return datetime.fromtimestamp(t, tz=ET).strftime("%Y-%m-%d"), t


# ── Append ────────────────────────────────────────────────────────────────────

def _append(path: Path, row: dict[str, Any]) -> bool:
    """Append one JSON line. Caller holds no lock; this takes it."""
    line = json.dumps(row, default=str) + "\n"
    with _lock:
        _guard_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    return True


def _fingerprint(row: dict[str, Any]) -> str:
    """Identity of one observed fill state.

    Re-polling an unchanged order yields the same fingerprint, so nothing is
    appended. A partial fill that later completes changes ``filled_qty`` and
    therefore appends a second row — which is the history we want.
    """
    return "|".join((
        str(row.get("order_id") or ""),
        _s(row.get("status")),
        f"{_f(row.get('filled_qty')) or 0.0:.6f}",
        f"{_f(row.get('filled_avg_price')) or 0.0:.6f}",
    ))


def _load_seen(path: Path) -> set[str]:
    """Fingerprints already on record in *path* (cached per path)."""
    with _lock:
        cached = _seen.get(path)
    if cached is not None:
        return cached

    found: set[str] = set()
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("event") == "fill":
                        found.add(_fingerprint(ev))
    except OSError:
        # Unreadable day file: better to risk a duplicate row than to drop a
        # fill. fold_orders() is idempotent over duplicates anyway.
        pass

    with _lock:
        _seen[path] = found
    return found


# ── Writers ───────────────────────────────────────────────────────────────────

def log_submit(
    *,
    action: str,
    symbol: str,
    order_id: str,
    order_status: Any = None,
    price: float | None = None,
    qty: float | None = None,
    trader_mode: str | None = None,
    trade_amount: float | None = None,
    note: str | None = None,
    ts: float | None = None,
    cfg: dict | None = None,
    extra: dict | None = None,
) -> bool:
    """Append one ``submit`` event. Never raises outside pytest.

    Mirrored out of ``alpaca_trader._log_action`` rather than wired into each
    of the 21 ``submit_order`` call sites, so a new order path is covered the
    day it is written instead of the day someone remembers to instrument it.
    """
    try:
        if not _enabled(cfg):
            return True  # disabled = intentional no-op success
        oid = str(order_id or "").strip()
        sym = str(symbol or "").upper().strip()
        if not oid or not sym:
            return False

        t = float(ts if ts is not None else time.time())
        row: dict[str, Any] = {
            "ts": round(t, 3),
            "day": datetime.fromtimestamp(t, tz=ET).strftime("%Y-%m-%d"),
            "event": "submit",
            "action": str(action or "").strip().upper(),
            "symbol": sym,
            "order_id": oid,
        }
        status = _s(order_status)
        if status:
            row["submit_status"] = status
        for key, val in (("price", _f(price)), ("qty", _f(qty)),
                         ("trade_amount", _f(trade_amount))):
            if val is not None:
                row[key] = val
        if trader_mode:
            row["trader_mode"] = str(trader_mode)
        if note:
            row["note"] = str(note)
        if isinstance(extra, dict):
            for k, v in extra.items():
                row.setdefault(str(k), v)
        row.update(_regime_fields())

        return _append(ledger_path_for_ts(t), row)
    except RuntimeError:
        raise  # the pytest production-path guard — must not be swallowed
    except Exception:
        return False


def record_fills(rows: list[dict] | None, *, ts: float | None = None,
                 cfg: dict | None = None) -> int:
    """Append broker fill rows that are not already on record.

    *rows* is the shape ``alpaca_trader.get_filled_orders`` returns. Returns
    the number of new events appended.
    """
    if not rows:
        return 0
    try:
        if not _enabled(cfg):
            return 0
    except Exception:
        return 0

    now = float(ts if ts is not None else time.time())
    written = 0
    for raw in rows:
        try:
            if not isinstance(raw, dict):
                continue
            oid = str(raw.get("id") or raw.get("order_id") or "").strip()
            sym = str(raw.get("symbol") or "").upper().strip()
            if not oid or not sym:
                continue

            day, filled_ts = _day_for_fill(raw, now)
            ev: dict[str, Any] = {
                "ts": round(filled_ts, 3),
                "day": day,
                "event": "fill",
                "symbol": sym,
                "order_id": oid,
                "client_order_id": str(raw.get("client_order_id") or ""),
                "side": _s(raw.get("side")),
                "status": _s(raw.get("status")),
                "type": _s(raw.get("type")),
                "order_class": _s(raw.get("order_class")),
                "qty": _f(raw.get("qty")),
                "filled_qty": _f(raw.get("filled_qty")),
                "filled_avg_price": _f(raw.get("filled_avg_price")),
                "submitted_at": str(raw.get("submitted_at") or ""),
                "filled_at": str(raw.get("filled_at") or ""),
                "observed_ts": round(now, 3),
            }

            path = ledger_path_for_day(day)
            seen = _load_seen(path)
            fp = _fingerprint(ev)
            if fp in seen:
                continue

            ev.update(_regime_fields())
            _append(path, ev)
            with _lock:
                _seen.setdefault(path, set()).add(fp)
            written += 1
        except RuntimeError:
            raise
        except Exception:
            continue
    return written


def poll_fills(cfg: dict | None = None, *, days: int = 2, limit: int = 500,
               min_interval_sec: float | None = None) -> dict[str, Any]:
    """Pull closed orders from the broker and record any new fills.

    Safe to call every desk tick: *min_interval_sec* (default
    ``ai_fill_ledger_poll_sec``) paces the actual broker call.
    """
    out: dict[str, Any] = {"ok": False, "polled": 0, "written": 0,
                           "skipped": None}
    global _last_poll_mono
    try:
        c = cfg if isinstance(cfg, dict) else None
        if c is None:
            try:
                from config import load_config
                c = load_config()
            except Exception:
                c = {}
        if not _enabled(c):
            out["skipped"] = "disabled"
            return out

        gap = min_interval_sec
        if gap is None:
            gap = _f(c.get("ai_fill_ledger_poll_sec", 60.0)) or 60.0
        now_mono = time.monotonic()
        if gap > 0 and (now_mono - _last_poll_mono) < gap:
            out["skipped"] = "paced"
            return out
        _last_poll_mono = now_mono

        import alpaca_trader
        if not alpaca_trader.is_active():
            out["skipped"] = "trader_off"
            return out

        rows = alpaca_trader.get_filled_orders(limit=limit, days=days)
        out["polled"] = len(rows or [])
        out["written"] = record_fills(rows, cfg=c)
        out["ok"] = True
        return out
    except RuntimeError:
        raise
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        return out


# ── Readers ───────────────────────────────────────────────────────────────────

def read_day(day: str | None = None) -> list[dict]:
    """All events for an ET day, in write order. Empty list when absent."""
    if day is None:
        day = datetime.now(tz=ET).strftime("%Y-%m-%d")
    path = ledger_path_for_day(day)
    rows: list[dict] = []
    try:
        if not path.exists():
            return rows
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return rows
    return rows


def fold_orders(events: list[dict] | None) -> dict[str, dict]:
    """Collapse an event stream to current truth, keyed by ``order_id``.

    The last ``fill`` event for an order wins on quantity and price, because
    events are appended in observation order and a partial that completes is
    observed later. ``submit`` events contribute the desk-side intent.
    """
    out: dict[str, dict] = {}
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        oid = str(ev.get("order_id") or "").strip()
        if not oid:
            continue
        cur = out.setdefault(oid, {
            "order_id": oid,
            "symbol": str(ev.get("symbol") or "").upper(),
            "submitted": False,
            "filled_qty": None,
            "filled_avg_price": None,
            "status": None,
            "events": 0,
        })
        cur["events"] += 1
        if ev.get("event") == "submit":
            cur["submitted"] = True
            cur.setdefault("action", ev.get("action"))
            cur.setdefault("submit_price", ev.get("price"))
            cur.setdefault("trader_mode", ev.get("trader_mode"))
        elif ev.get("event") == "fill":
            cur["filled_qty"] = ev.get("filled_qty")
            cur["filled_avg_price"] = ev.get("filled_avg_price")
            cur["status"] = ev.get("status")
            cur["side"] = ev.get("side")
            cur["filled_at"] = ev.get("filled_at")
            if not cur.get("symbol"):
                cur["symbol"] = str(ev.get("symbol") or "").upper()
    return out


def reconcile_day(day: str | None = None, *, cfg: dict | None = None,
                  limit: int = 500) -> dict[str, Any]:
    """Compare the ledger against the broker's own record for one ET day.

    ``broker_only`` is the finding that matters: a fill the broker made and the
    ledger never saw. ``ledger_only`` normally means the broker's lookback
    window no longer covers that day.
    """
    if day is None:
        day = datetime.now(tz=ET).strftime("%Y-%m-%d")

    report: dict[str, Any] = {
        "day": day, "ok": False,
        "ledger_orders": 0, "broker_orders": 0,
        "matched": 0, "broker_only": [], "ledger_only": [], "mismatched": [],
    }

    ledger = {
        oid: row for oid, row in fold_orders(read_day(day)).items()
        if row.get("status")
    }
    report["ledger_orders"] = len(ledger)

    try:
        import alpaca_trader
        if not alpaca_trader.is_active():
            report["error"] = "trader_off"
            return report
        raw = alpaca_trader.get_filled_orders(limit=limit, days=7) or []
    except Exception as e:  # noqa: BLE001
        report["error"] = str(e)
        return report

    broker: dict[str, dict] = {}
    for row in raw:
        try:
            d, _ = _day_for_fill(row, time.time())
            if d != day:
                continue
            oid = str(row.get("id") or "").strip()
            if oid:
                broker[oid] = row
        except Exception:
            continue
    report["broker_orders"] = len(broker)

    def _close(a: Any, b: Any, tol: float) -> bool:
        fa, fb = _f(a), _f(b)
        if fa is None or fb is None:
            return fa == fb
        return abs(fa - fb) <= tol

    for oid, brow in broker.items():
        lrow = ledger.get(oid)
        if lrow is None:
            report["broker_only"].append({
                "order_id": oid,
                "symbol": str(brow.get("symbol") or "").upper(),
                "filled_qty": _f(brow.get("filled_qty")),
                "filled_avg_price": _f(brow.get("filled_avg_price")),
            })
            continue
        qty_ok = _close(lrow.get("filled_qty"), brow.get("filled_qty"), 1e-6)
        px_ok = _close(lrow.get("filled_avg_price"),
                       brow.get("filled_avg_price"), 1e-4)
        if qty_ok and px_ok:
            report["matched"] += 1
        else:
            report["mismatched"].append({
                "order_id": oid,
                "symbol": str(brow.get("symbol") or "").upper(),
                "ledger_qty": lrow.get("filled_qty"),
                "broker_qty": _f(brow.get("filled_qty")),
                "ledger_px": lrow.get("filled_avg_price"),
                "broker_px": _f(brow.get("filled_avg_price")),
            })

    for oid, lrow in ledger.items():
        if oid not in broker:
            report["ledger_only"].append({
                "order_id": oid,
                "symbol": lrow.get("symbol"),
                "filled_qty": lrow.get("filled_qty"),
            })

    report["ok"] = (not report["broker_only"]) and (not report["mismatched"])
    return report


__all__ = [
    "fold_orders",
    "ledger_path_for_day",
    "ledger_path_for_ts",
    "log_submit",
    "poll_fills",
    "read_day",
    "reconcile_day",
    "record_fills",
    "set_ledger_path_for_tests",
]
