#!/usr/bin/env python3
"""Log the burst+RSI entry as it happens, with its latency. Places nothing.

THE RULE
    universe   names with a MENTION BURST premarket (04:00-09:30) today
    trigger    first CLOSED 1m bar at/after 09:30 with CM RSI-2 >= 70
    (measured) fill at the next bar's open; exit is an ATR-2.0x ratchet plus
               an RSI-2 <= 30 reversal exit, -5% hard

    Measured 2026-09-05 on 105 premarket-burst names, 2026-08-24..09-04:

        P(+3% before -3%)      71%     (an ungated open entry is 49%)
        entry + exit           +2.56% mean, +1.46% median, 10/10 sessions
        exit parameter plateau 48 of 48 neighbouring cells positive
        cost at the signal     +1.10% at 0.60% round trip

    The operator's model is that the entry only has to pick a direction --
    the ratchet handles where the move stops. 71% up-before-down is that
    precondition, and it is the first thing measured that supplies it.

WHY IT IS LOGGED AND NOT TRADED

    The edge is one bar wide, and the honest bar was missed:

        t    +2.56%  10/10          t+1 at 0.20% cost  +0.72%  7/10
        t+1  +0.72%   7/10          t+1 at 0.40% cost  +0.52%  6/10, median -0.06%
        t+2  +1.14%   7/10          t+1 at 0.60% cost  +0.32%  6/10, median -0.26%

    "t" already means filling at the open of the bar AFTER the signal, which
    is arguably a print you cannot decide to take. One further bar and the
    median goes negative once realistic costs are charged. The pre-registered
    bar was "t+1 positive at 0.40-0.60% cost with 7+ sessions" and it came
    back 6/10 with a negative median. It is not dead; it is not proven.

    Two numbers decide which world this lives in, and NEITHER has ever been
    measured on this desk:

      latency_sec  seconds from bar close to an order being in. Puts the
                   desk at t (+2.56%) or at t+1 (+0.72% falling to marginal).
      real spread  at these names at these moments, which picks the column
                   in that cost table.

    No further backtesting produces either. That is what this log is for.

    A predecessor rule (EXH crossing 75 then RSI-2 >= 90) was logged here and
    FALSIFIED the same day -- point-in-time and warm-up bugs were 57% of it,
    and its jitter profile had no peak at the signal at all. This rule differs
    in that its jitter is a plateau, its exit neighbourhood is uniformly
    positive, and it survives a 1% round trip at the signal bar. It is still
    in-sample: ten sessions, and the ones it was found on.

EVALUATES CLOSED BARS ONLY
    The poll runs every ~5s and RSI-2 moves inside a forming minute, so
    firing on a forming bar would be a different signal wearing this name.
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
_STATE: dict[tuple[str, str], dict] = {}          # (sym, day) -> last_bar/fired
_BURSTS: dict[str, tuple[float, set[str]]] = {}   # day -> (loaded_at, symbols)

RTH_OPEN_MIN = 9 * 60 + 30
PREMARKET_MIN = 4 * 60
BURST_REFRESH_SEC = 300.0

# Honest fill for this rule: decide on a CLOSED bar, so the earliest
# executable print is the NEXT bar's open. signal_close is the optimistic
# counterfactual only — never the pass/fail model.
FILL_MODEL_DEFAULT = "next_open"
FILL_MODELS = ("signal_close", "next_open", "next_bar_close")

DEFAULTS = {
    "ai_strength_signal_enabled": True,
    "ai_strength_rsi_min": 70.0,
    "ai_strength_rsi_period": 2,
    "ai_strength_require_burst": True,
    "ai_strength_one_per_day": True,
    # Declared fill model stamped on every row. Pass/fail uses next_open.
    "ai_strength_fill_model": FILL_MODEL_DEFAULT,
}


def _cfg(cfg: dict, key: str):
    v = (cfg or {}).get(key)
    return DEFAULTS[key] if v is None else v


def _f(cfg: dict, key: str) -> float:
    try:
        return float(_cfg(cfg, key))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def report_dir() -> str:
    return os.environ.get("AI_REPORT_DIR") or os.path.join(ROOT, "ai_reports")


def log_path() -> str:
    return os.path.join(report_dir(), "strength_signals.jsonl")


def _et_minutes(ts: float) -> int:
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def premarket_bursts(day: str, now: float) -> set[str]:
    """Symbols with an 04:00-09:30 mention burst today, from signal_shadow.

    Cached on a timer: the set can still grow until 09:30, and re-reading a
    large jsonl on every 5-second poll would be its own problem.
    """
    hit = _BURSTS.get(day)
    if hit and (now - hit[0]) < BURST_REFRESH_SEC:
        return hit[1]
    syms: set[str] = set()
    path = os.path.join(report_dir(), "signal_shadow.jsonl")
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    ts = float(r["ts"])
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                if str(r.get("signal") or "") != "mention_burst":
                    continue
                d = datetime.fromtimestamp(ts, ET)
                if d.strftime("%Y-%m-%d") != day:
                    continue
                if not (PREMARKET_MIN <= _et_minutes(ts) < RTH_OPEN_MIN):
                    continue
                sym = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
                if sym:
                    syms.add(sym)
    except OSError:
        # A missing burst file is not "no bursts", it is no information. With
        # require_burst on, nothing fires — the safe way for a logger to be
        # wrong is to record less, never to record something it did not see.
        pass
    _BURSTS[day] = (now, syms)
    return syms


def evaluate(symbols, cfg: dict, now: float, *, ew=None) -> list[dict]:
    """Evaluate every watched name on its newest CLOSED bar. Never raises."""
    out: list[dict] = []
    if not bool(_cfg(cfg, "ai_strength_signal_enabled")):
        return out
    if ew is None:
        import ai_entry_watch as ew  # noqa: PLC0415
    if _et_minutes(now) < RTH_OPEN_MIN:
        return out                                   # the rule starts at the open
    day = datetime.fromtimestamp(now, ET).strftime("%Y-%m-%d")
    need_burst = bool(_cfg(cfg, "ai_strength_require_burst"))
    bursts = premarket_bursts(day, now) if need_burst else None
    rsi_min = _f(cfg, "ai_strength_rsi_min")
    try:
        period = max(2, int(_cfg(cfg, "ai_strength_rsi_period")))
    except (TypeError, ValueError):
        period = 2
    one_per_day = bool(_cfg(cfg, "ai_strength_one_per_day"))

    for raw in symbols or ():
        sym = str(raw or "").upper().strip()
        if not sym:
            continue
        if bursts is not None and sym not in bursts:
            continue
        try:
            row = _one(sym, day, cfg, now, ew, rsi_min, period, one_per_day,
                       len(bursts) if bursts is not None else None)
        except Exception:
            continue
        if row:
            out.append(row)
    if out:
        _append(out)
    return out


def _one(sym, day, cfg, now, ew, rsi_min, period, one_per_day,
         burst_count) -> dict | None:
    rows = ew.symbol_ohlc(sym, cfg, now)
    if not rows or len(rows) < period + 5:
        return None
    stamps = ew._cached_ohlc_stamps(sym, cfg, now)
    if not stamps or len(stamps) != len(rows):
        return None

    # The newest CLOSED bar is the one before the (possibly forming) last.
    i = len(rows) - 2
    if i < period + 2:
        return None
    bar_ts = float(stamps[i])
    if _et_minutes(bar_ts) < RTH_OPEN_MIN:
        return None

    key = (sym, day)
    with _LOCK:
        st = _STATE.setdefault(key, {"last_bar": 0.0, "fired": False})
        if bar_ts <= st["last_bar"]:
            return None                              # already scored this bar
        st["last_bar"] = bar_ts
        if st["fired"] and one_per_day:
            return None

    closes = [float(r[2]) for r in rows[:i + 1]]
    try:
        series = ew.cm_rsi_series(closes, period)
    except Exception:
        return None
    if not series:
        return None
    rsi = float(series[-1])
    if rsi < rsi_min:
        return None

    with _LOCK:
        _STATE[key]["fired"] = True

    decision_ts = float(now)
    latency_sec = round(decision_ts - bar_ts, 1)
    fill_model = str(_cfg(cfg, "ai_strength_fill_model") or FILL_MODEL_DEFAULT)
    if fill_model not in FILL_MODELS:
        fill_model = FILL_MODEL_DEFAULT

    return {
        "kind": "strength_signal",
        "rule": "premarket_burst_rsi2",
        "symbol": sym,
        "day": day,
        # Latency honesty (non-negotiable). Pass/fail uses fill_model
        # next_open, not the optimistic signal_close counterfactual.
        # bar_ts / fired_at kept as aliases for older scorers.
        "signal_bar_ts": bar_ts,
        "bar_ts": bar_ts,
        "decision_ts": decision_ts,
        "fired_at": decision_ts,
        "latency_sec": latency_sec,
        "fill_model": fill_model,
        "cm_rsi": round(rsi, 2),
        "rsi": round(rsi, 2),
        "rsi_period": period,
        "price": round(float(rows[i][2]), 4),
        "bars": i + 1,
        "burst_universe": burst_count,
        "burst_required": bool(_cfg(cfg, "ai_strength_require_burst")),
        "params": {"rsi_min": rsi_min,
                   "require_burst": bool(_cfg(cfg, "ai_strength_require_burst")),
                   "fill_model": fill_model},
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
        _BURSTS.clear()
