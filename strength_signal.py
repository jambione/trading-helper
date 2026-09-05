#!/usr/bin/env python3
"""Log the strength entry as it happens. Places nothing, ever.

THE RULE (validated 2026-09-05 over 2026-08-24..09-04, n=363)

    setup    EXH crosses UP through 75      — the name heads into overbought
    trigger  first CLOSED bar within 20 where CM RSI-2 >= 90 and EXH >= 60
    fill     the NEXT bar's open
    exit     EXH leaves overbought; 2% ratchet; -5% hard; 120m cap

    +0.86%/trade, 10/10 sessions, 3.9 sigma vs same-name controls, breakeven
    at a ~1.0% round trip, and still +0.43% with the 20 best trades removed.

    The same setup entered on RSI-2 <= 20 — waiting for the pullback — loses
    1.25%/trade at 0/10 sessions. A name that keeps running never prints a
    low RSI-2, so "wait for the dip" selects the ones that rolled over.

WHY THIS EXISTS RATHER THAN A CONFIG CHANGE
    That measurement has no holdout. Roughly seventy configurations were
    searched over the same ten sessions, and three results looked this strong
    earlier the same day and dissolved under a control. So the rule earns
    forward validation on sessions it has never seen, not capital. This
    module writes what it would have done; tools/score_strength.py prices it
    afterwards. Nothing here can reach the order path.

WHY BARS AND NOT POLLS
    The poll runs every ~5s and EXH moves inside a forming minute, so a
    crossing detected on a poll is not the crossing the backtest measured.
    Every reading here comes off CLOSED 1-minute bars, one evaluation per
    bar per symbol, which is the same series the rule was fitted on.

THE ONE-BAR CONSTRAINT
    Filling at the next bar's open keeps +0.86%; one full bar later is
    -0.16% and 3/10 sessions. If this is ever wired to the order path, the
    order has to be in within a minute of the signal bar's close — and the
    latency between this row's `fired_at` and its `bar_ts` is the number
    that says whether the desk can do that. It is logged for that reason.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.abspath(__file__))

_LOCK = threading.Lock()
# (symbol, ET date) -> {"last_bar": float, "cross_ts": float|None,
#                       "cross_i": int|None, "fired": bool}
_STATE: dict[tuple[str, str], dict] = {}

FAST = 21          # rte_fast_length — the desk's own %R window
DEFAULTS = {
    "ai_strength_signal_enabled": True,
    "ai_strength_cross": 75.0,       # EXH level the setup crosses upward
    "ai_strength_rsi_min": 90.0,     # CM RSI-2 that triggers the entry
    "ai_strength_exh_floor": 60.0,   # EXH must still be engaged at the trigger
    "ai_strength_wait_bars": 20,     # bars the setup stays live after crossing
    "ai_strength_one_per_day": True,
}


def _cfg(cfg: dict, key: str):
    val = (cfg or {}).get(key)
    return DEFAULTS[key] if val is None else val


def _f(cfg: dict, key: str) -> float:
    try:
        return float(_cfg(cfg, key))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def log_path() -> str:
    base = os.environ.get("AI_REPORT_DIR") or os.path.join(ROOT, "ai_reports")
    return os.path.join(base, "strength_signals.jsonl")


def exh_at(rows, i: int, n: int = FAST) -> float | None:
    """100 + Williams %R over the trailing *n* bars — the desk's definition.

    rows are (high, low, close), the shape ai_entry_watch.symbol_ohlc returns.
    """
    if i < 0 or i >= len(rows):
        return None
    lo_i = max(0, i - n + 1)
    win = rows[lo_i:i + 1]
    try:
        hh = max(float(r[0]) for r in win)
        ll = min(float(r[1]) for r in win)
        c = float(rows[i][2])
    except (TypeError, ValueError, IndexError):
        return None
    if hh <= ll:
        return None
    return 100.0 * (c - ll) / (hh - ll)


def evaluate(symbols, cfg: dict, now: float, *, ew=None) -> list[dict]:
    """Evaluate every watched name on its newest CLOSED bar.

    Returns the signal rows written (usually none). Never raises: a logging
    experiment must not be able to take the poll down.
    """
    out: list[dict] = []
    if not bool(_cfg(cfg, "ai_strength_signal_enabled")):
        return out
    if ew is None:
        import ai_entry_watch as ew  # noqa: PLC0415
    cross = _f(cfg, "ai_strength_cross")
    rsi_min = _f(cfg, "ai_strength_rsi_min")
    floor = _f(cfg, "ai_strength_exh_floor")
    try:
        wait = int(_cfg(cfg, "ai_strength_wait_bars"))
    except (TypeError, ValueError):
        wait = 20
    one_per_day = bool(_cfg(cfg, "ai_strength_one_per_day"))
    day = datetime.fromtimestamp(now, ET).strftime("%Y-%m-%d")

    for raw in symbols or ():
        sym = str(raw or "").upper().strip()
        if not sym:
            continue
        try:
            row = _evaluate_one(sym, day, cfg, now, ew, cross, rsi_min,
                                floor, wait, one_per_day)
        except Exception:
            continue
        if row:
            out.append(row)
    if out:
        _append(out)
    return out


def _evaluate_one(sym, day, cfg, now, ew, cross, rsi_min, floor, wait,
                  one_per_day) -> dict | None:
    rows = ew.symbol_ohlc(sym, cfg, now)
    if not rows or len(rows) < FAST + 3:
        return None
    stamps = ew._cached_ohlc_stamps(sym, cfg, now)
    if not stamps or len(stamps) != len(rows):
        return None

    # The last row may still be forming, so the newest CLOSED bar is the one
    # before it. Evaluating a forming bar would fire on a reading that can
    # still change, which is not the series the rule was measured on.
    i = len(rows) - 2
    if i < FAST:
        return None
    bar_ts = float(stamps[i])

    key = (sym, day)
    with _LOCK:
        st = _STATE.get(key)
        if st is None:
            st = {"last_bar": 0.0, "cross_ts": None, "cross_i": None,
                  "fired": False}
            _STATE[key] = st
        if bar_ts <= st["last_bar"]:
            return None                      # already scored this bar
        st["last_bar"] = bar_ts
        if st["fired"] and one_per_day:
            return None
        prev_cross_i, prev_cross_ts = st["cross_i"], st["cross_ts"]

    e_now, e_prev = exh_at(rows, i), exh_at(rows, i - 1)
    if e_now is None or e_prev is None:
        return None

    # SETUP: crossing up through the level, on closed bars.
    if e_prev < cross <= e_now:
        with _LOCK:
            _STATE[key]["cross_i"] = i
            _STATE[key]["cross_ts"] = bar_ts
        return None                          # the trigger is a later bar

    if prev_cross_i is None or (i - prev_cross_i) > wait:
        return None
    if e_now < floor:
        return None

    closes = [float(r[2]) for r in rows[:i + 1]]
    try:
        series = ew.cm_rsi_series(closes, 2)
    except Exception:
        return None
    if not series:
        return None
    rsi = float(series[-1])
    if rsi < rsi_min:
        return None

    with _LOCK:
        _STATE[key]["fired"] = True

    return {
        "kind": "strength_signal",
        "symbol": sym,
        "day": day,
        # bar_ts is what a scorer must fill from: the entry is the NEXT
        # bar's open. fired_at minus bar_ts is the desk's latency, and the
        # rule dies if that exceeds one bar.
        "bar_ts": bar_ts,
        "fired_at": float(now),
        "latency_sec": round(float(now) - bar_ts, 1),
        "exh": round(e_now, 2),
        "exh_prev": round(e_prev, 2),
        "cm_rsi": round(rsi, 2),
        "price": round(float(rows[i][2]), 4),
        "cross_ts": prev_cross_ts,
        "bars_since_cross": i - prev_cross_i,
        # Stamped so a row stays interpretable if the knobs are ever changed.
        "rule": {"cross": cross, "rsi_min": rsi_min, "exh_floor": floor,
                 "wait_bars": wait},
    }


def _append(rows: list[dict]) -> None:
    path = log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    except Exception:
        pass


def reset_state() -> None:
    """Tests only."""
    with _LOCK:
        _STATE.clear()
