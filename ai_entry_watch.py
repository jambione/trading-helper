"""Agreement-based session watch queue for AI paper entries.

Research (slow clock) upserts symbols that clear the agreement gate.
The poller arms/buys from stored structure; this module owns load/save,
upsert/invalidation, zone/spread arming, rate-limited structure refresh,
and poll_once paper entry placement.
"""
from __future__ import annotations

import json
import sys
import time
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
_TERMINAL_STATUSES = frozenset({
    "filled", "submitted", "invalidated", "expired",
})

# Ring of structure LLM call timestamps (module-level budget window).
_structure_call_ts: list[float] = []
_STRUCTURE_BUDGET_WINDOW_SEC = 3600.0


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
        in_zone = False
        if (
            last_ask_f is not None
            and entry_low_f is not None
            and entry_high_f is not None
            and entry_low_f > 0
            and entry_high_f > 0
        ):
            in_zone = ask_in_zone(
                last_ask_f, entry_low_f, entry_high_f, pad_pct)
        ready = status == "armed" or (status == "watching" and in_zone)
        rows.append({
            "symbol": sym,
            "status": status or None,
            "wait_kind": wait_kind,
            "entry_low": entry_low_f,
            "entry_high": entry_high_f,
            "last_ask": last_ask_f,
            "score": score_f,
            "agreement": bool(rec.get("agreement")) if rec.get("agreement") is not None else None,
            "reason": str(rec.get("reason") or "")[:80] or None,
            "source": str(rec.get("source") or "research")[:24] or "research",
            "ready": bool(ready),
            "in_zone": bool(in_zone),
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
    in_zone = False
    if (
        last_ask_f is not None
        and entry_low_f is not None
        and entry_high_f is not None
        and entry_low_f > 0
        and entry_high_f > 0
    ):
        in_zone = ask_in_zone(last_ask_f, entry_low_f, entry_high_f, pad_pct)
    ready = status == "armed" or (status == "watching" and in_zone)
    if status == "armed" or ready:
        phase = "ready"
    elif status == "submitted":
        phase = "submitted"
    elif status == "filled":
        phase = "filled"  # upgraded to open if broker position present
    else:
        phase = "watching"
    src = str(rec.get("source") or "research").strip() or "research"
    return {
        "symbol": sym,
        "phase": phase,
        "status": status,
        "ready": bool(ready),
        "in_zone": bool(in_zone),
        "source": src,
        "score": score_f,
        "reason": str(rec.get("reason") or "")[:80] or None,
        "wait_kind": wait_kind,
        "entry_low": entry_low_f,
        "entry_high": entry_high_f,
        "last_ask": last_ask_f,
        "price": last_ask_f,
        "qty": None,
        "avg_entry": None,
        "pl": None,
        "plpc": None,
        "mkt_val": None,
        "is_position": False,
    }


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
            "price": current if current is not None else prev.get("price"),
            "last_ask": current if current is not None else prev.get("last_ask"),
            "qty": p.get("qty"),
            "avg_entry": p.get("avg_entry"),
            "pl": p.get("pl"),
            "plpc": p.get("plpc"),
            "mkt_val": p.get("mkt_val"),
        }

    # Display filter: only panel-live names, plus true open/submitted positions.
    try:
        live = live_panel_universe()
    except Exception:
        live = set()
    filtered: list[dict] = []
    for r in by_sym.values():
        sym = str(r.get("symbol") or "").upper()
        phase = str(r.get("phase") or "")
        if phase in ("open", "submitted", "filled") or r.get("is_position"):
            filtered.append(r)
        elif not live or sym in live:
            filtered.append(r)
    rows = filtered

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
            "last_ask": prev.get("last_ask", _EMPTY_RECORD_DEFAULTS["last_ask"]),
            "updated_ts": float(now),
        }
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


def _parse_hhmm(raw: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = str(raw or "").strip().split(":")
        return int(hh), int(mm)
    except Exception:
        return default


def _et_now(now: float | None = None):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    t0 = float(now if now is not None else time.time())
    return datetime.fromtimestamp(t0, tz=ZoneInfo("America/New_York"))


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
    """True on weekdays from ``ai_watch_start_time`` (default 09:00 ET) until EOD.

    AI Watch may seed/sync and refresh structure in this window. Paper *entries*
    still require regular-session market hours (see ``trading_hours_active``).
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
        str(cfg.get("ai_watch_start_time") or "09:00"), (9, 0))
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
_DESK_SOURCES = frozenset({"momentum", "trending", "mom", "st", "stocktwits"})


def _merge_source(prev_src: str, new_src: str) -> str:
    """Research beats desk heat; otherwise prefer the non-empty new source."""
    p = str(prev_src or "").strip().lower()
    n = str(new_src or "").strip().lower()
    if not n:
        return prev_src or "research"
    if not p:
        return new_src or "research"
    if p in _RESEARCH_SOURCES and n in _DESK_SOURCES:
        return prev_src  # keep AI thesis ownership
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


def _momentum_flagged_from_dashboard(max_price: Any) -> list[tuple[float, dict]]:
    """Momentum panel rows that currently show a flag (FIRST / NEW / BURST)."""
    try:
        import urllib.request
        with urllib.request.urlopen(
            "http://127.0.0.1:8888/api/state", timeout=2.0
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    tickers = data.get("tickers") or []
    if not isinstance(tickers, list):
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
        scored.append((rank, {
            "symbol": s,
            "trending_score": round(rank, 2),
            "score": round(rank, 2),
            "reason": reason[:40],
            "agreement": True,
            "source": "momentum",
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
    """Momentum desk names with |day change %| above *min_pct*."""
    try:
        import urllib.request
        with urllib.request.urlopen(
            "http://127.0.0.1:8888/api/state", timeout=2.0
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    tickers = data.get("tickers") or []
    if not isinstance(tickers, list):
        return []
    scored: list[tuple[float, dict]] = []
    for r in tickers:
        if not isinstance(r, dict):
            continue
        pct = _pct_change_value(r.get("pct_change"))
        if pct is None or abs(pct) <= float(min_pct):
            continue
        s = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
        if not s or not s[0].isalpha():
            continue
        if not _price_under_cap(r.get("price"), max_price):
            continue
        scored.append((abs(pct), {
            "symbol": s,
            "trending_score": round(abs(pct), 2),
            "score": round(abs(pct), 2),
            "reason": f"momentum chg {pct:+.0f}%",
            "agreement": True,
            "source": "momentum",
        }))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored


def desk_candidate_rows(cfg: dict | None = None) -> list[dict]:
    """Restrictive momentum + trending candidates for AI Watch.

    Rules (operator):
      • No AI Research names (handled by sync — research seed off).
      • Trending: score **> min** (default 10), **or** |day chg %| > min (50),
        **or** relative volume **> min** (default 1.0 = 100% of avg).
      • Momentum: FIRST / NEW / BURST flag on the desk,
        **or** |day pct_change| above min (default 50).

    Structure poller still defines zone/stop before arming a buy.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    rows: list[dict] = []
    seen: set[str] = set()
    max_price = cfg.get("ai_max_price", cfg.get("claude_max_price"))
    try:
        min_pct = float(cfg.get("ai_watch_min_pct_change", 50.0) or 50.0)
    except (TypeError, ValueError):
        min_pct = 50.0
    try:
        # Ratio units: 1.0 == 100% of average volume (same as desk RVOL display).
        min_rvol = float(cfg.get("ai_watch_min_rvol", 3.0) or 3.0)
    except (TypeError, ValueError):
        min_rvol = 1.0

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

    if cfg.get("ai_watch_seed_trending", True):
        try:
            path = ROOT / "trending_stocks.json"
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            tr_rows = raw.get("rows") or []
            try:
                min_score = float(cfg.get("ai_watch_trending_min_score", 10.0) or 10.0)
            except (TypeError, ValueError):
                min_score = 10.0
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
                    if not s or not s[0].isalpha() or s in seen:
                        continue
                    if not _price_under_cap(r.get("price"), max_price):
                        continue
                    try:
                        score = float(r.get("trending_score", r.get("score") or 0) or 0)
                    except (TypeError, ValueError):
                        score = 0.0
                    pct = _pct_change_value(r.get("pct_change"))
                    big_move = pct is not None and abs(pct) > float(min_pct)
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
                    high_rvol = rvol is not None and rvol > float(min_rvol)
                    # Score path OR big day-move OR elevated relative volume.
                    if score <= min_score and not big_move and not high_rvol:
                        continue
                    seen.add(s)
                    if high_rvol and score <= min_score and not big_move:
                        reason = f"trending rvol {rvol:.2f}x"
                        use_score = float(rvol) * 10.0  # rank key only
                    elif big_move and score <= min_score:
                        reason = f"trending chg {pct:+.0f}%"
                        use_score = abs(float(pct))
                    else:
                        reason = f"trending score {score:.1f}"
                        use_score = score
                    rows.append({
                        "symbol": s,
                        "trending_score": round(use_score, 2),
                        "score": round(use_score, 2),
                        "reason": reason,
                        "agreement": True,
                        "source": "trending",
                    })
                    if len([x for x in rows if x.get("source") == "trending"]) >= max(1, n):
                        break
        except Exception:
            pass

    return rows


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
    """Symbols allowed on AI Watch (flagged momentum + high trending only)."""
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
    """Rebuild AI Watch from restrictive Momentum + Trending only.

    Operator rules:
      • **No AI Research** names on the watch book.
      • Trending only if score > ``ai_watch_trending_min_score`` (default 10).
      • Momentum only if the desk row has FIRST / NEW / BURST.

    Structure / in-flight submitted-filled state is preserved when the symbol
    remains. Everything else is dropped from the book file.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    t0 = float(now if now is not None else time.time())
    old = load_watch()
    if not isinstance(old, dict):
        old = {}

    # Momentum (flagged) + trending (score>min) only — research excluded.
    merged: dict[str, dict] = {}
    for r in desk_candidate_rows(cfg):
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym:
            continue
        src = str(r.get("source") or "").lower()
        if src not in ("momentum", "mom", "trending", "st", "stocktwits"):
            continue
        merged[sym] = r

    # Empty sources at startup: keep prior state to avoid wipe race.
    if not merged and old:
        return old

    new_state: dict[str, Any] = {}
    for sym, row in merged.items():
        prev = old.get(sym) if isinstance(old.get(sym), dict) else {}
        prev_status = str(prev.get("status") or "").lower().strip()
        if prev_status in ("submitted", "filled"):
            status = prev_status
        else:
            status = "watching"
        new_state[sym] = {
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
            "last_ask": prev.get("last_ask", _EMPTY_RECORD_DEFAULTS["last_ask"]),
            "updated_ts": t0,
        }

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
        if status in ("submitted", "filled") or is_duel:
            if status in ("invalidated", "expired") and not is_duel:
                continue
            kept = dict(rec)
            kept["symbol"] = key
            new_state[key] = kept

    save_watch(new_state)
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
    """After research/open-bell: re-mirror Momentum + Trending + Research.

    ``rows`` is accepted for API compatibility; the live board files and desk
    heat are the source of truth (see ``sync_watch_from_source_panels``).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    # Optional: ensure latest research rows hit the wire before sync
    # (caller already wrote suggestions files).
    _ = rows
    return sync_watch_from_source_panels(cfg=cfg, now=now)


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
      ai_watch_zone_offset_pct  — % below last to set buy-zone *upper* (1.5)
      ai_watch_zone_width_pct   — zone depth below that upper (1.0)
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
        try:
            return max(0.0, float(cfg.get(key, default) or default))
        except (TypeError, ValueError):
            return default

    offset = _pct("ai_watch_zone_offset_pct", 1.5) / 100.0
    width = _pct("ai_watch_zone_width_pct", 1.0) / 100.0
    stop_pct = _pct("ai_watch_synth_stop_pct", 2.0) / 100.0
    try:
        rr = float(cfg.get("ai_watch_synth_rr", 3.0) or 3.0)
    except (TypeError, ValueError):
        rr = 3.0
    rr = max(1.0, rr)

    # Upper buy limit sits *below* the print so we wait for a dip.
    entry_high = px * (1.0 - offset)
    entry_low = entry_high * (1.0 - width)
    if entry_low <= 0 or entry_high <= 0 or entry_low >= entry_high:
        entry_high = px * 0.99
        entry_low = px * 0.98
    mid = (entry_low + entry_high) / 2.0
    stop = entry_low * (1.0 - stop_pct)
    if stop <= 0 or stop >= mid:
        stop = mid * 0.98
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
        "scale_out_pct": 40,
        "synthetic": True,
        "anchor_price": _r(px),
        "summary": (
            f"synth pullback: upper {_r(entry_high)} "
            f"({off_pct:.1f}% under {_r(px)})"
            + (f" · {reason}" if reason else "")
        ),
    }


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
    """
    if not isinstance(rec, dict) or not _desk_source(rec):
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
    # Keep a good existing zone; only fill gaps / replace hard_no / empty levels.
    if _structure_usable(structure) and not structure.get("synthetic"):
        return None
    if _structure_usable(structure) and structure.get("synthetic"):
        # Already have a frozen synth zone — leave it (wait for pullback).
        return None

    reason = str(rec.get("reason") or rec.get("source") or "")
    synth = build_offset_zone_structure(ask_f, cfg, reason=reason)
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
        "reason": "offset_from_last",
    }


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
        pad = float(cfg.get("ai_entry_zone_pad_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        pad = 0.0

    try:
        a = float(ask)
    except (TypeError, ValueError):
        return False, "below_zone"

    # Zone membership is the primary arm signal (matches UI READY). Check it
    # before spread so a wide IEX quote cannot block a price already in zone.
    if ask_in_zone(a, entry_low, entry_high, pad):
        return True, "zone"

    try:
        max_spread = float(cfg.get("ai_max_spread_pct", 1.0) or 0.0)
    except (TypeError, ValueError):
        max_spread = 1.0
    # Spread only as a soft pre-filter when still outside the zone (near-touch
    # paths). Missing bid does not fail — see spread_ok.
    if not spread_ok(bid, ask, max_spread):
        return False, "spread"

    frac = max(0.0, pad) / 100.0
    high_bound = max(entry_low, entry_high) * (1.0 + frac)
    if a > high_bound:
        return False, "above_zone"
    return False, "below_zone"


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


def _decision_for_place(structure: dict) -> dict[str, Any]:
    """Build a place_scaled_entry decision from stored structure levels.

    Zone-wait records store decision=WAIT; placement needs BUY + levels.
    """
    d = dict(structure)
    d["decision"] = "BUY"
    d["wait_kind"] = None
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

    # Watching window: 09:00 ET → EOD liquidate (default 15:50). Structure /
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

    # Do not re-seed here — book thread runs sync_watch_from_source_panels
    # so this poll only evaluates symbols currently mirrored from the panels.

    state = load_watch()
    if not state:
        return events

    try:
        max_price = cfg.get("ai_max_price")
        max_price_f = float(max_price) if max_price is not None else None
    except (TypeError, ValueError):
        max_price_f = None

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

    for sym_key, rec in list(state.items()):
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

        # Quotes
        try:
            ask = gt._latest_ask(sym)
        except Exception:
            ask = None
        try:
            bid = gt._latest_bid(sym)
        except Exception:
            bid = None
        try:
            ask_f = float(ask) if ask is not None else 0.0
        except (TypeError, ValueError):
            ask_f = 0.0
        if ask_f > 0:
            rec["last_ask"] = ask_f
        rec["last_poll_ts"] = t0

        structure = rec.get("structure") if isinstance(rec.get("structure"), dict) else None

        # Mom/ST: prefer a mechanical pullback zone when model has no usable zone
        # (missing levels / hard_no). Do this before invalidate so we can still trade.
        if ask_f > 0 and _desk_source(rec) and not _structure_usable(structure):
            sev = ensure_offset_zone_if_needed(rec, ask_f, cfg, t0)
            if sev:
                events.append(sev)
                try:
                    cp.log_event(
                        "synth_zone",
                        symbol=sym,
                        entry_low=sev.get("entry_low"),
                        entry_high=sev.get("entry_high"),
                        anchor=sev.get("anchor"),
                    )
                except Exception:
                    pass
            structure = rec.get("structure") if isinstance(rec.get("structure"), dict) else None

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
                    state[sym] = rec
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
        if _structure_stale(rec, cfg, t0) and not _structure_usable(
            rec.get("structure")
        ):
            if (
                not _desk_source(rec)
                and structure_calls_remaining(cfg, t0) > 0
                and ask_f > 0
            ):
                sev = ensure_structure(rec, cfg, t0)
                if sev:
                    events.append(sev)
                if str(rec.get("status") or "").lower() in _TERMINAL_STATUSES:
                    state[sym] = rec
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
                state[sym] = rec
                continue

        if ask_f <= 0:
            state[sym] = rec
            continue

        if max_price_f is not None and ask_f >= max_price_f:
            try:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym, reason="above_max_price",
                    ask=ask_f, max_price=max_price_f))
            except Exception:
                events.append({
                    "kind": "watch_skip",
                    "symbol": sym,
                    "reason": "above_max_price",
                })
            state[sym] = rec
            continue

        # Arm / buy
        try:
            bid_f: float | None
            if bid is None:
                bid_f = None
            else:
                bid_f = float(bid)
        except (TypeError, ValueError):
            bid_f = None

        ok_arm, why = should_arm_buy(rec, ask=ask_f, bid=bid_f, cfg=cfg)
        if not ok_arm:
            if why in ("wait_setup", "hard_no", "spread", "above_zone",
                       "below_zone", "reward_risk", "no_structure"):
                try:
                    events.append(cp.log_event(
                        "watch_skip", symbol=sym, reason=why, ask=ask_f))
                except Exception:
                    events.append({
                        "kind": "watch_skip",
                        "symbol": sym,
                        "reason": why,
                    })
            state[sym] = rec
            continue

        # Position / buy-cap gates (fail closed on errors — never place blind)
        try:
            if gt.has_open_position(sym):
                events.append(cp.log_event(
                    "watch_skip", symbol=sym, reason="already_held"))
                state[sym] = rec
                continue
        except Exception as e:  # noqa: BLE001
            try:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym,
                    reason=f"gate_error:has_open_position:{e}"[:200]))
            except Exception:
                events.append({
                    "kind": "watch_skip",
                    "symbol": sym,
                    "reason": "gate_error:has_open_position",
                })
            state[sym] = rec
            continue
        try:
            if not gt.can_open_new_position(sym):
                events.append(cp.log_event(
                    "watch_skip", symbol=sym, reason="max_positions"))
                state[sym] = rec
                continue
        except Exception as e:  # noqa: BLE001
            try:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym,
                    reason=f"gate_error:can_open_new_position:{e}"[:200]))
            except Exception:
                events.append({
                    "kind": "watch_skip",
                    "symbol": sym,
                    "reason": "gate_error:can_open_new_position",
                })
            state[sym] = rec
            continue
        try:
            if gt.buys_left_this_poll() <= 0:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym, reason="buy_cap"))
                state[sym] = rec
                continue
        except Exception as e:  # noqa: BLE001
            try:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym,
                    reason=f"gate_error:buys_left_this_poll:{e}"[:200]))
            except Exception:
                events.append({
                    "kind": "watch_skip",
                    "symbol": sym,
                    "reason": "gate_error:buys_left_this_poll",
                })
            state[sym] = rec
            continue

        structure = rec.get("structure")
        if not isinstance(structure, dict):
            state[sym] = rec
            continue

        equity = _equity()
        if equity <= 0:
            try:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym, reason="no_equity"))
            except Exception:
                events.append({
                    "kind": "watch_skip",
                    "symbol": sym,
                    "reason": "no_equity",
                })
            state[sym] = rec
            continue

        if not allow_buys:
            # Structure/zone ready, but no paper entry until RTH (market hours).
            try:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym, reason="not_trading_hours",
                    ask=ask_f))
            except Exception:
                events.append({
                    "kind": "watch_skip",
                    "symbol": sym,
                    "reason": "not_trading_hours",
                })
            state[sym] = rec
            continue

        # Duel day: only registered A/X champions (winner-only after trial).
        try:
            import ai_duel as duel
            if duel.duel_enabled(cfg):
                src_w = rec.get("duel_source") or rec.get("source")
                if not duel.allow_entry_for_source(cfg, src_w, sym, now=t0):
                    try:
                        events.append(cp.log_event(
                            "watch_skip", symbol=sym, reason="duel_blocked",
                            ask=ask_f))
                    except Exception:
                        events.append({
                            "kind": "watch_skip",
                            "symbol": sym,
                            "reason": "duel_blocked",
                        })
                    state[sym] = rec
                    continue
        except Exception:
            pass

        # Re-check live ask immediately before place (quotes can move during
        # gate checks). READY in the UI uses last_ask; only fill if still in zone.
        try:
            ask2 = gt._latest_ask(sym)
            ask2_f = float(ask2) if ask2 is not None else 0.0
        except Exception:
            ask2_f = 0.0
        if ask2_f > 0:
            ask_f = ask2_f
            rec["last_ask"] = ask_f
        try:
            bid2 = gt._latest_bid(sym)
            bid2_f = float(bid2) if bid2 is not None else None
        except Exception:
            bid2_f = bid_f
        ok_arm2, why2 = should_arm_buy(rec, ask=ask_f, bid=bid2_f, cfg=cfg)
        if not ok_arm2:
            try:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym, reason=f"recheck_{why2}",
                    ask=ask_f))
            except Exception:
                events.append({
                    "kind": "watch_skip",
                    "symbol": sym,
                    "reason": f"recheck_{why2}",
                })
            state[sym] = rec
            continue

        place_decision = _decision_for_place(structure)
        if isinstance(place_decision, dict):
            place_decision = dict(place_decision)
            place_decision["source"] = rec.get("duel_source") or rec.get("source")
            place_decision["duel_source"] = place_decision.get("source")
        rec["status"] = "armed"
        try:
            result = cp.place_scaled_entry(
                sym,
                place_decision,
                equity,
                risk_pct=risk_pct,
                current_ask=ask_f,
                duel_source=str(
                    rec.get("duel_source") or rec.get("source") or ""
                ) or None,
            )
        except Exception as e:  # noqa: BLE001
            try:
                events.append(cp.log_event(
                    "entry_fail", symbol=sym, reason=str(e)[:200]))
            except Exception:
                events.append({
                    "kind": "entry_fail",
                    "symbol": sym,
                    "reason": str(e)[:200],
                })
            state[sym] = rec
            continue

        if isinstance(result, dict) and result.get("ok"):
            rec["status"] = "submitted"
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
            try:
                events.append(cp.log_event(
                    "entry_ok",
                    symbol=sym,
                    stop_price=result.get("stop_price"),
                    target_1=result.get("target_1"),
                    ask=ask_f,
                ))
            except Exception:
                events.append({
                    "kind": "entry_ok",
                    "symbol": sym,
                    "ask": ask_f,
                })
        else:
            err = ""
            if isinstance(result, dict):
                err = str(result.get("error") or "place_failed")[:200]
            else:
                err = "place_failed"
            try:
                events.append(cp.log_event(
                    "entry_fail", symbol=sym, reason=err))
            except Exception:
                events.append({
                    "kind": "entry_fail",
                    "symbol": sym,
                    "reason": err,
                })
            # Stay armed/watching for retry next poll
            if str(rec.get("status") or "") == "armed":
                rec["status"] = "watching"

        state[sym] = rec

    save_watch(state)
    return events
