#!/usr/bin/env python3
"""Where is the name in its own day range at admission? Log it; optionally refuse.

WHY THIS EXISTS
    Measured 2026-09-05 over 195 momentum admissions (2026-08-24..09-04),
    anchored at the admission instant and held to the close:

        range position at admission     n     mean     median   win%
          bottom third                 15   +0.55%    +0.92%    53%
          middle third                 33   +0.25%    -0.63%    39%
          top third                    81   -2.04%    -1.25%    38%
          at the highs (90+)           66   -3.08%    -0.37%    44%

        refusing the top third:  book -1.81% -> +0.34% (48 of 195 kept)

    The same gradient shows in move-since-open, where the +30-60% bucket
    loses 15.36% to the close on a -9.67% median with 12% winners. The desk
    currently ranks candidates by abs(pct) and takes the top 12 — it sorts
    for that bucket and buys it.

WHY IT IS OFF BY DEFAULT
    In-sample. Four results on 2026-09-05 looked at least this strong and
    died under review, so this one gets out-of-sample days before it gets
    to change behaviour. What it does today is write down what it WOULD
    have refused, once per name-day, so those days accumulate.

    That said, the objections that killed the others do not reach this one:
    it is an admission filter, so no fill can flatter it; it was scored on
    plain buy-and-hold, so no state-dependent exit biases it; and it is
    anchored at first watch, so no pre-admission bars leak in.

FAIL-OPEN
    A name whose range cannot be computed is admitted, and logged with
    range_pos=None. Absence is not a verdict — refusing on blindness is how
    the MACD availability gates starved the book for weeks.

WINDOW CAVEAT, read before trusting a live row against the backtest
    The backtest measured the range over the session so far, from the 09:25
    fetch. Live, this reads whatever ai_entry_watch has cached, which is a
    rolling window (ai_watch_db_lookback_bars, 220 by default) and can be
    shorter than the elapsed session. Bars are filtered to today's RTH, and
    `bars_used` rides on every row so the analysis can tell a full-session
    reading from a partial one instead of averaging them together.
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
_LOGGED: set[tuple[str, str]] = set()

RTH_OPEN_MIN = 9 * 60 + 30
MIN_BARS = 10          # below this the "range" is a handful of minutes
DEFAULTS = {
    # 0 = log only, refuse nothing. 67 = refuse the top third (the measured
    # cut). Live behaviour is unchanged until this is set.
    "ai_watch_admit_max_range_pos": 0.0,
    "ai_watch_admit_range_log": True,
}


def _cfg(cfg: dict, key: str):
    v = (cfg or {}).get(key)
    return DEFAULTS[key] if v is None else v


def log_path() -> str:
    base = os.environ.get("AI_REPORT_DIR") or os.path.join(ROOT, "ai_reports")
    return os.path.join(base, "admit_range.jsonl")


def range_pos(symbol: str, price: float, cfg: dict, now: float, *, ew):
    """(0-100 position in today's range, bars used) or (None, 0).

    100 = at the session high so far, 0 = at the low. Reads only the cache
    the poll already warmed; never fetches, because this runs on every
    candidate on every poll.
    """
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None, 0
    if px <= 0:
        return None, 0
    rows = ew.symbol_ohlc(symbol, cfg, now)
    if not rows:
        return None, 0
    stamps = ew._cached_ohlc_stamps(symbol, cfg, now)
    if stamps and len(stamps) == len(rows):
        today = datetime.fromtimestamp(now, ET).date()
        keep = []
        for s, r in zip(stamps, rows):
            d = datetime.fromtimestamp(s, ET)
            if d.date() == today and (d.hour * 60 + d.minute) >= RTH_OPEN_MIN:
                keep.append(r)
        rows = keep
    if len(rows) < MIN_BARS:
        return None, len(rows)
    try:
        hh = max(float(r[0]) for r in rows)
        ll = min(float(r[1]) for r in rows)
    except (TypeError, ValueError, IndexError):
        return None, len(rows)
    # The live print can sit outside the closed-bar range; extend rather than
    # clamp, so a new high reads 100 instead of an impossible 140.
    hh, ll = max(hh, px), min(ll, px)
    if hh <= ll:
        return None, len(rows)
    return 100.0 * (px - ll) / (hh - ll), len(rows)


def check(row: dict, cfg: dict, now: float, *, ew) -> str | None:
    """Reject reason, or None to admit. Logs once per name-day either way.

    Never raises: an admission experiment must not be able to empty the book.
    """
    try:
        return _check(row, cfg, now, ew)
    except Exception:
        return None


def _check(row, cfg, now, ew) -> str | None:
    sym = str(row.get("symbol") or "").upper().strip()
    if not sym:
        return None
    try:
        cap = float(_cfg(cfg, "ai_watch_admit_max_range_pos"))
    except (TypeError, ValueError):
        cap = 0.0

    pos, nbars = range_pos(sym, row.get("price"), cfg, now, ew=ew)
    row["admit_range_pos"] = None if pos is None else round(pos, 1)
    row["admit_range_bars"] = nbars

    would = pos is not None and cap > 0 and pos > cap
    if bool(_cfg(cfg, "ai_watch_admit_range_log")):
        _log_once(sym, row, now, pos, nbars, cap, would)
    return "admit_range_pos" if would else None


def _log_once(sym, row, now, pos, nbars, cap, would) -> None:
    day = datetime.fromtimestamp(now, ET).strftime("%Y-%m-%d")
    key = (sym, day)
    with _LOCK:
        if key in _LOGGED:
            return
        _LOGGED.add(key)
    rec = {
        "kind": "admit_range",
        "symbol": sym,
        "day": day,
        "ts": float(now),
        "range_pos": None if pos is None else round(pos, 1),
        "bars_used": nbars,
        "price": row.get("price"),
        "pct_change": row.get("pct_change"),
        "rvol": row.get("rvol"),
        "source": row.get("source"),
        # cap 0 means the filter is inert; `would_refuse` is then what it
        # WOULD have done, which is the whole point of logging while off.
        "cap": cap,
        "filter_active": cap > 0,
        "would_refuse": bool(pos is not None and pos > 67.0),
    }
    path = log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def reset_state() -> None:
    """Tests only."""
    with _LOCK:
        _LOGGED.clear()
