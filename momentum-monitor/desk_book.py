"""Session desk book: open bracket plans, R-tracking, stop ratchet, daily halt.

Persists to momentum-monitor/desk_book.json (runtime state).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
BOOK_PATH = HERE / "desk_book.json"

try:
    import desk_risk as dr
except ImportError:
    import sys
    sys.path.insert(0, str(HERE.parent))
    import desk_risk as dr


def _session_day(now: float | None = None) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ts = time.time() if now is None else now
    return datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def empty_book(day: str | None = None) -> dict[str, Any]:
    return {
        "session_day": day or _session_day(),
        "session_r": 0.0,
        "halted": False,
        "halt_reason": "",
        "positions": {},
        "closed": [],
    }


def load_book(path: Path = BOOK_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "positions" in data:
            return data
    except Exception:
        pass
    return empty_book()


def save_book(book: dict[str, Any], path: Path = BOOK_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(book, indent=2), encoding="utf-8")
    tmp.replace(path)


def rollover_if_needed(book: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    day = _session_day(now)
    if book.get("session_day") != day:
        return empty_book(day)
    return book


def is_halted(book: dict[str, Any], daily_loss_r: float = 2.0) -> bool:
    if book.get("halted"):
        return True
    try:
        if float(book.get("session_r") or 0) <= -abs(float(daily_loss_r)):
            return True
    except (TypeError, ValueError):
        pass
    return False


def apply_halt_if_needed(book: dict[str, Any], daily_loss_r: float = 2.0) -> dict[str, Any]:
    if is_halted(book, daily_loss_r):
        book["halted"] = True
        if not book.get("halt_reason"):
            book["halt_reason"] = (
                f"session_r={float(book.get('session_r') or 0):.2f}"
                f" <= -{abs(float(daily_loss_r)):g}R"
            )
    return book


def register_open(
    book: dict[str, Any],
    *,
    symbol: str,
    plan: dict[str, Any],
    buy_order_id: str | None,
    now: float | None = None,
) -> dict[str, Any]:
    book = rollover_if_needed(book, now)
    sym = symbol.upper()
    book.setdefault("positions", {})[sym] = {
        **plan,
        "symbol": sym,
        "phase": "initial",
        "buy_order_id": buy_order_id,
        "opened_ts": time.time() if now is None else now,
        "initial_stop": plan.get("stop"),
        # High water mark for the R-trail. Seeded at entry so the first manage
        # tick trails from a real price rather than from 0.
        "peak": plan.get("entry"),
    }
    return book


def note_close(
    book: dict[str, Any],
    symbol: str,
    *,
    realized_pnl: float | None,
    now: float | None = None,
    daily_loss_r: float = 2.0,
) -> dict[str, Any]:
    book = rollover_if_needed(book, now)
    sym = symbol.upper()
    pos = (book.get("positions") or {}).pop(sym, None)
    if not pos:
        return book
    risk = float(pos.get("risk_dollars") or 0)
    tr = None
    if realized_pnl is not None and risk > 0:
        tr = dr.trade_r(float(realized_pnl), risk)
        if tr is not None:
            book["session_r"] = float(book.get("session_r") or 0) + tr
    closed = book.setdefault("closed", [])
    closed.append({
        "symbol": sym,
        "realized_pnl": realized_pnl,
        "trade_r": tr,
        "session_r": book.get("session_r"),
        "ts": time.time() if now is None else now,
        "entry": pos.get("entry"),
        "qty": pos.get("qty"),
    })
    if len(closed) > 50:
        book["closed"] = closed[-50:]
    return apply_halt_if_needed(book, daily_loss_r)


def manage_open(
    book: dict[str, Any],
    *,
    live_positions: dict[str, dict],
    cfg: dict,
    replace_stop_fn: Callable[[str, float], dict],
    trail_fn: Callable[[str, float], dict] | None = None,
    now: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Ratchet stops on open book positions; detect closes when flat in broker."""
    book = rollover_if_needed(book, now)
    events: list[dict[str, Any]] = []
    be_at = float(cfg.get("be_at_r", 1.0))
    lock_at = float(cfg.get("lock_at_r", 2.0))
    trail_pct = float(cfg.get("trail_pct", 0) or 0)
    trail_r = float(cfg.get("trail_r", dr.DEFAULT_TRAIL_R))
    trail_step_r = float(cfg.get("trail_step_r", dr.DEFAULT_TRAIL_STEP_R))
    daily_loss_r = float(cfg.get("daily_loss_r", 2.0))
    live_map = {str(k).upper(): v for k, v in (live_positions or {}).items()}

    for sym in list((book.get("positions") or {}).keys()):
        pos = book["positions"][sym]
        if sym not in live_map:
            pl = None
            book = note_close(
                book, sym, realized_pnl=pl, now=now, daily_loss_r=daily_loss_r)
            events.append({
                "kind": "desk_close",
                "symbol": sym,
                "session_r": book.get("session_r"),
                "halted": book.get("halted"),
            })
            continue

        live = live_map[sym]
        current = live.get("current") or live.get("avg_entry") or pos.get("entry")
        entry = float(pos.get("entry") or live.get("avg_entry") or 0)
        r_ps = float(pos.get("r_per_share") or 0)
        ur = dr.unrealized_r(entry, r_ps, float(current or entry))
        if ur is None:
            continue
        phase = str(pos.get("phase") or "initial")
        init_stop = float(pos.get("initial_stop") or pos.get("stop") or 0)

        # High water mark, updated before any stop math: the R-trail is measured
        # from the peak, so a tick that moved the stop off a stale peak would
        # trail the wrong price. Recorded on the position because the manage tick
        # only ever sees the current print — nothing else remembers the high.
        cur_px = float(current or entry)
        peak = max(float(pos.get("peak") or entry), cur_px)
        pos["peak"] = peak

        # A broker-side trail owns the stop once placed; leave it alone.
        if phase == "trail":
            continue

        # At/after lock_at the position trails for the rest of its life. The old
        # code wrote entry+1R here, marked the position "locked", and excluded
        # that phase from further ratcheting — so the stop never moved again and
        # a name that ran to +8R still exited at +1R. Entering the trail is
        # unchanged (at exactly lock_at, peak-1R == entry+1R); what is new is
        # that it keeps climbing on every later tick.
        if ur >= lock_at or phase == "r_trail":
            if trail_pct > 0 and trail_fn is not None and phase != "r_trail":
                res = trail_fn(sym, trail_pct)
                if res.get("ok"):
                    pos["phase"] = "trail"
                    events.append({
                        "kind": "stop_ratchet", "symbol": sym,
                        "phase": "trail", "trail_pct": trail_pct,
                        "unreal_r": round(ur, 2),
                    })
                    continue
            want = dr.trail_stop_level(
                entry=entry, peak=peak, r_per_share=r_ps, trail_r=trail_r)
            cur_stop = float(pos.get("stop") or 0)
            # Raise-only, and only when the move is worth a cancel + submit.
            if want is not None and want >= cur_stop + trail_step_r * r_ps:
                res = replace_stop_fn(sym, want)
                if res.get("ok"):
                    pos["phase"] = "r_trail"
                    pos["stop"] = want
                    events.append({
                        "kind": "stop_ratchet", "symbol": sym,
                        "phase": "r_trail", "stop": want,
                        "peak": round(peak, 2), "trail_r": trail_r,
                        "unreal_r": round(ur, 2),
                    })
            elif phase != "r_trail":
                # Already high enough to be in the trail; record the phase so the
                # next tick keeps trailing even if this one had nothing to move.
                pos["phase"] = "r_trail"
            continue

        if ur >= be_at and phase == "initial":
            new_stop = dr.stop_for_phase(
                "be", entry=entry, initial_stop=init_stop, r_per_share=r_ps)
            res = replace_stop_fn(sym, new_stop)
            if res.get("ok"):
                pos["phase"] = "be"
                pos["stop"] = new_stop
                events.append({
                    "kind": "stop_ratchet", "symbol": sym,
                    "phase": "be", "stop": new_stop,
                    "unreal_r": round(ur, 2),
                })

    return book, events
