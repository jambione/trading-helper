#!/usr/bin/env python3
"""Turn a burst+RSI signal into an order. TWO gates, both default to safe.

    ai_strength_trade_enabled   False   nothing happens at all
    ai_strength_trade_dry_run   True    decides and logs, places nothing

Both must be changed for a real order to leave this module. Enabling it
alone is not enough — that is deliberate, because the failure mode of a new
entry path is not "a bad trade", it is "an unintended loop placing orders",
and one flag is one typo away from that.

THE RULE (measured 2026-09-05 over 2026-08-24..09-04, 105 names)

    universe  premarket mention burst (04:00-09:30)
    trigger   first CLOSED 1m bar at/after 09:30 with CM RSI-2 >= 70
    exit      ratchet (the desk's local trail is already near-optimal here),
              -5% hard stop, and eventually an RSI-2 <= 30 reversal exit
    result    +2.56% mean, +1.46% median, 10/10 sessions, in-sample
              P(+3% before -3%) = 71%, against 49% for an ungated open entry

WHAT IS HONESTLY KNOWN, AND WHAT IS NOT
    Known: the exit neighbourhood is a plateau (48 of 48 cells positive), it
    survives a 1% round trip at the signal bar, and the entry beats a
    state-matched permutation null.

    Not known: any of it out of sample. Ten sessions, the ones it was found
    on, and four of five in-sample winners died the same day. Also the fill:
    the measured edge falls from +2.56% at the signal bar to +0.72% one bar
    later, and at a realistic 0.40% round trip that becomes 6/10 sessions
    with a negative median. The pre-registered bar was 7/10 and it missed.

    So this exists to be READY, not to be right. strength_signal's
    `latency_sec` is what says which of those two worlds the desk lives in,
    and until that has real days behind it, dry_run is the honest setting.

WHY THE LADDER IS NOT HERE
    Scale-outs measured worse on this rule at every tier tested: no ladder
    +1.76%, thirds at +3/+8 +1.56%, thirds at +2/+5 +1.37%. The ratchet does
    the capital protection; selling into strength gave back more than it
    banked. scale_out_pct is therefore 0 unless explicitly configured.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.abspath(__file__))

_LOCK = threading.Lock()
_PLACED: set[tuple[str, str]] = set()

RTH_OPEN_MIN = 9 * 60 + 30
RTH_LAST_ENTRY_MIN = 15 * 60          # no new entries in the last hour

DEFAULTS = {
    "ai_strength_trade_enabled": False,
    "ai_strength_trade_dry_run": True,
    "ai_strength_stop_pct": 5.0,       # the tested hard stop
    "ai_strength_target_pct": 8.0,     # sizes reward_risk; no partial sell
    "ai_strength_trail_pct": 2.0,      # tested optimum; desk default is 1.75
    "ai_strength_scale_out_pct": 0.0,  # the ladder measured worse
    "ai_strength_max_open": 3,         # of the desk's 5 seats
}


def _cfg(cfg: dict, key: str):
    v = (cfg or {}).get(key)
    return DEFAULTS[key] if v is None else v


def _f(cfg: dict, key: str) -> float:
    try:
        return float(_cfg(cfg, key))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def log_path() -> str:
    base = os.environ.get("AI_REPORT_DIR") or os.path.join(ROOT, "ai_reports")
    return os.path.join(base, "strength_trades.jsonl")


def _et_minutes(ts: float) -> int:
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def build_decision(price: float, cfg: dict) -> dict | None:
    """The decision dict place_scaled_entry expects, from a signal price.

    qualifies_as_entry requires entry/stop/target to be real positive
    numbers, so anything unrepresentable returns None rather than a
    half-formed order.
    """
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None
    if px <= 0:
        return None
    stop_pct = _f(cfg, "ai_strength_stop_pct")
    tgt_pct = _f(cfg, "ai_strength_target_pct")
    if stop_pct <= 0 or tgt_pct <= 0:
        return None
    stop = round(px * (1.0 - stop_pct / 100.0), 4)
    target = round(px * (1.0 + tgt_pct / 100.0), 4)
    if stop <= 0 or stop >= px:
        return None
    return {
        "decision": "BUY",
        "entry_low": px,
        "entry_high": px,
        "stop_price": stop,
        "target_1": target,
        "reward_risk": round((target - px) / (px - stop), 3),
        "scale_out_pct": _f(cfg, "ai_strength_scale_out_pct"),
        "trail_pct": _f(cfg, "ai_strength_trail_pct"),
        # The rule's entry is the bar after the signal, not a zone touch.
        # Without this the zone machinery would re-gate a decision that was
        # already made on a different basis.
        "skip_zone": True,
        "summary": "burst+RSI2 strength entry",
    }


def consider(signals, cfg: dict, now: float, *, cp=None, gt=None,
             equity: float | None = None) -> list[dict]:
    """Decide on each fired signal. Returns the decision rows it logged.

    Never raises: an entry experiment must not be able to take the poll down.
    """
    out: list[dict] = []
    if not signals:
        return out
    if not bool(_cfg(cfg, "ai_strength_trade_enabled")):
        return out
    dry = bool(_cfg(cfg, "ai_strength_trade_dry_run"))
    day = datetime.fromtimestamp(now, ET).strftime("%Y-%m-%d")
    for sig in signals:
        try:
            row = _consider_one(sig, cfg, now, day, dry, cp, gt, equity)
        except Exception as exc:                       # noqa: BLE001
            row = {"kind": "strength_trade", "action": "error",
                   "symbol": str((sig or {}).get("symbol") or ""),
                   "reason": type(exc).__name__, "ts": float(now)}
        if row:
            out.append(row)
    if out:
        _append(out)
    return out


def _consider_one(sig, cfg, now, day, dry, cp, gt, equity) -> dict | None:
    sym = str((sig or {}).get("symbol") or "").upper().strip()
    if not sym:
        return None
    base = {"kind": "strength_trade", "symbol": sym, "day": day,
            "ts": float(now), "dry_run": dry,
            "signal_bar_ts": (sig or {}).get("bar_ts"),
            "latency_sec": (sig or {}).get("latency_sec"),
            "cm_rsi": (sig or {}).get("cm_rsi")}

    def refuse(reason):
        return dict(base, action="skip", reason=reason)

    mins = _et_minutes(now)
    if mins < RTH_OPEN_MIN:
        return refuse("before_open")
    if mins >= RTH_LAST_ENTRY_MIN:
        return refuse("late_session")

    with _LOCK:
        if (sym, day) in _PLACED:
            return refuse("already_placed_today")

    if gt is not None:
        try:
            if gt.has_open_position(sym):
                return refuse("already_holding")
        except Exception:
            return refuse("position_check_failed")

    try:
        max_open = int(_cfg(cfg, "ai_strength_max_open"))
    except (TypeError, ValueError):
        max_open = 3
    if gt is not None and max_open > 0:
        # gt.open_position_count() is the desk's own accessor; its
        # effective_max_positions() is the book limit this rule must live
        # inside. Whichever is tighter wins — a new entry path should never
        # be the thing that fills the book.
        try:
            open_n = int(gt.open_position_count())
        except Exception:
            return refuse("position_count_failed")
        try:
            desk_max = int(gt.effective_max_positions())
        except Exception:
            desk_max = max_open
        cap = min(max_open, desk_max) if desk_max > 0 else max_open
        if open_n >= cap:
            return refuse("slots_full_%d_of_%d" % (open_n, cap))

    price = (sig or {}).get("price")
    decision = build_decision(price, cfg)
    if decision is None:
        return refuse("unpriceable")
    if cp is not None and not cp.qualifies_as_entry(decision):
        return refuse("not_qualified")

    base = dict(base, price=price, stop_price=decision["stop_price"],
                target_1=decision["target_1"],
                reward_risk=decision["reward_risk"])

    if dry:
        # The whole point of the dry run: a row identical to the live one
        # except that nothing was sent, so the two are comparable later.
        return dict(base, action="would_place")

    if cp is None:
        return refuse("no_broker")
    eq = equity
    if eq is None:
        try:
            import alpaca_trader
            eq = alpaca_trader.get_equity()
        except Exception:
            eq = None
    try:
        eq = float(eq or 0)
    except (TypeError, ValueError):
        eq = 0.0
    if eq <= 0:
        return refuse("no_equity")

    res = cp.place_scaled_entry(sym, decision, eq,
                                duel_source="strength_burst_rsi")
    with _LOCK:
        _PLACED.add((sym, day))
    ok = bool(isinstance(res, dict) and res.get("ok", True))
    return dict(base, action="placed" if ok else "place_failed",
                broker=(res if isinstance(res, dict) else {"result": str(res)}))


def _append(rows: list[dict]) -> None:
    path = log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r, default=str) + "\n")
    except Exception:
        pass


def reset_state() -> None:
    """Tests only."""
    with _LOCK:
        _PLACED.clear()
