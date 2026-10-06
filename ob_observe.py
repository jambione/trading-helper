"""Order blocks at the arm decision. OBSERVE ONLY: it logs, it never gates.

Operator-approved 2026-10-05 (docs/HANDOFF_2026-10-05_GROK.md, "APPROVED ...
OBSERVE-ONLY"). The night-batch skip rule FAILED its pre-registered test
(docs/studies/ORDER_BLOCKS_2026-10-05.md), so this exists only to build the
held-out record on the desk's own IEX feed. This module itself never gates.
OPERATOR OVERRIDE 2026-10-05: the operator chose a hard skip anyway, after the
tradeoffs; it lives in ai_entry_watch (_ob_resist_refusal), behind its own
knob ai_watch_ob_resist_skip (code default off, fails open on no reading).

What it computes, per arm decision, from ``tools/order_blocks.py``
(LuxAlgo Order Blocks & Breaker Blocks, swing 10, wicks, 1-minute bars):

  ob_resist_0.3  price is inside, or within 0.3% under, a CHARTED resistance
                 block (last 3 per side) known at that moment: an unbroken
                 bearish OB or a bullish breaker. Same test as the study.
  ob_room_pct    % from price up to the bottom of the nearest charted
                 resistance block above; 0.0 when price is inside one; None
                 when there is none above. Same as tools/studies/ob_fills_daily.py.
  ob_brk_dist_pct  % the price sits above the top of the most recently broken charted bearish OB (it became a
                 breaker within the last 15 min and price is still above its top); None when there is no such
                 break. Same definition as the 10/5 study's BREAKOUT DISTANCE cells (order_block_gate.py).
                 Forward test: docs/studies/sr_breakout_forward_prereg.json.
  ob_bars        how many 1-minute bars the blocks were computed on.
  ob_prior_day   whether those bars reach back into the prior trading day.

Bars: NO new data requests. The desk's structure scan
(ai_entry_watch._fetch_symbol_lows) already pulls the newest 1-minute IEX
bars with extended hours on its own throttle; ``absorb`` folds each of those
frames into a per-symbol store here (prior trading day + today, 04:00-16:00
ET). Only bars CLOSED at the decision are used and only blocks whose
known_ts is at or before it (no look-ahead). Prior-day bars are present only
as far back as that fetch reached; ``ob_bars`` / ``ob_prior_day`` say how much
history each reading had.

Cost: the block pass runs once per symbol per minute (cached); the
price-dependent part is a handful of comparisons per call.

Safety: every public function swallows its own errors and returns an empty
result, and nothing in the arm/place path reads these fields.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BAR_SEC = 60.0
SWING = 10
SHOW = 3
WITHIN_PCT = 0.3
CFG_KEY = "ai_watch_ob_observe"
# How many newest ET trading dates the LIVE store keeps. The study this
# mirrors (ob_fills_daily) used 2 (prior day + today). OPERATOR OVERRIDE
# 2026-10-06 10:14 ET (Jonathan): 3 = two prior sessions + today, so a zone
# like CRWV's 10/02 resistance is seen on 10/06. UNTESTED: no study measured
# the skip with a 3-session window. Code constant on purpose (not bot_config).
LIVE_SESSIONS = 3

_LOCK = threading.RLock()
# symbol -> {bar_ts: (ts, open, high, low, close)}
_BARS: dict[str, dict[float, tuple]] = {}
# symbol -> (cache key, charted blocks, n_bars, prior_day)
_CACHE: dict[str, tuple] = {}
# symbol -> wall-clock time (time.time()) of the last absorb that brought in
# at least one session bar. Lets the caller treat a store that stopped
# refreshing as "no reading" (fail open) instead of trusting stale blocks.
_LAST_ABSORB: dict[str, float] = {}


def enabled(cfg: dict | None) -> bool:
    """The knob. Default OFF: absent, None or any falsy value is off."""
    try:
        return bool((cfg or {}).get("ai_watch_ob_observe", False))
    except Exception:  # noqa: BLE001
        return False


def _et(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), ET)


def _session_bar(ts: float) -> bool:
    """Premarket + RTH (04:00-16:00 ET), the same window the study used."""
    t = _et(ts)
    m = t.hour * 60 + t.minute
    return 4 * 60 <= m < 16 * 60


def _trim(store: dict[float, tuple], keep_days: int | None = None) -> None:
    """Keep the `keep_days` newest ET trading dates present (default
    LIVE_SESSIONS: two prior sessions + today)."""
    n = int(LIVE_SESSIONS if keep_days is None else keep_days)
    days = sorted({_et(t).date() for t in store})
    if len(days) <= n:
        return
    keep = set(days[-n:])
    for t in [t for t in store if _et(t).date() not in keep]:
        del store[t]


def absorb_rows(symbol: str, rows) -> int:
    """Fold (ts, open, high, low, close) rows into the store. Newer data for the
    same minute replaces older (a re-fetch completes a bar seen while forming).
    Returns the store size. Never raises."""
    try:
        sym = str(symbol or "").upper().strip()
        if not sym:
            return 0
        n_ok = 0
        with _LOCK:
            store = _BARS.setdefault(sym, {})
            for r in rows or ():
                try:
                    ts = float(r[0])
                    o, h, lo, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                except (TypeError, ValueError, IndexError):
                    continue
                if not (h > 0 and lo > 0 and c > 0 and o > 0) or not _session_bar(ts):
                    continue
                ts = float(int(ts // BAR_SEC) * BAR_SEC)
                store[ts] = (ts, o, h, lo, c)
                n_ok += 1
            _trim(store)
            if n_ok:
                _LAST_ABSORB[sym] = time.time()
            return len(store)
    except Exception:  # noqa: BLE001
        return 0


def absorb_df(symbol: str, df: Any) -> int:
    """Fold an alpaca_api.fetch_bars frame (DatetimeIndex; open/high/low/close)
    into the store. This is the ONLY way bars get in: the desk's existing fetch.
    Never raises."""
    try:
        if df is None or len(df) == 0:
            return 0
        rows = []
        for t, r in zip(df.index, df[["open", "high", "low", "close"]].itertuples(index=False)):
            try:
                ts = t.timestamp()
            except AttributeError:
                continue
            rows.append((ts, r[0], r[1], r[2], r[3]))
        return absorb_rows(symbol, rows)
    except Exception:  # noqa: BLE001
        return 0


def store_size(symbol: str) -> int:
    """How many 1-minute bars the store holds for `symbol` (0 = none). Never raises."""
    try:
        with _LOCK:
            return len(_BARS.get(str(symbol or "").upper().strip()) or {})
    except Exception:  # noqa: BLE001
        return 0


def absorb_age(symbol: str) -> float | None:
    """Seconds (wall clock) since the last absorb that added session bars for
    `symbol`; None if never. Never raises."""
    try:
        with _LOCK:
            t = _LAST_ABSORB.get(str(symbol or "").upper().strip())
        return None if t is None else max(0.0, time.time() - t)
    except Exception:  # noqa: BLE001
        return None


def session_count(symbol: str) -> int:
    """Distinct ET dates in the store for `symbol` (0 = none). Never raises."""
    try:
        with _LOCK:
            ts = list((_BARS.get(str(symbol or "").upper().strip()) or {}).keys())
        return len({_et(t).date() for t in ts})
    except Exception:  # noqa: BLE001
        return 0


def has_prior_day(symbol: str) -> bool:
    """True when the store holds bars from two ET dates (prior day + today)."""
    try:
        with _LOCK:
            ts = list((_BARS.get(str(symbol or "").upper().strip()) or {}).keys())
        if not ts:
            return False
        return _et(min(ts)).date() != _et(max(ts)).date()
    except Exception:  # noqa: BLE001
        return False


def bars(symbol: str) -> list[tuple]:
    """Stored bars, oldest first (a copy)."""
    with _LOCK:
        return sorted((_BARS.get(str(symbol or "").upper().strip()) or {}).values())


def _charted_at(sym: str, now: float):
    """(charted blocks known by `now`, n_bars, prior_day), cached per symbol per
    minute (and re-run if the store gained bars inside that minute)."""
    from tools import order_blocks as OB
    minute = float(int(float(now) // BAR_SEC) * BAR_SEC)
    with _LOCK:
        store = _BARS.get(sym) or {}
        # Closed bars only: a bar is closed once ts + 60 <= the current minute.
        rows = sorted(v for t, v in store.items() if t + BAR_SEC <= minute)
        key = (minute, len(rows), rows[-1][0] if rows else None)
        hit = _CACHE.get(sym)
        if hit is not None and hit[0] == key:
            return hit[1], hit[2], hit[3]
    if not rows:
        return [], 0, False
    last = None
    for _i, blocks in OB.order_blocks(rows, length=SWING, bar_sec=BAR_SEC):
        last = blocks
    known = [b for b in (last or []) if b.known_ts <= minute]
    ch = OB.charted(known, show=SHOW)
    # Two ET dates among the bars = the prior session is in the window.
    prior = _et(rows[0][0]).date() != _et(rows[-1][0]).date()
    with _LOCK:
        _CACHE[sym] = (key, ch, len(rows), prior)
    return ch, len(rows), prior


def _resistance(b) -> bool:
    return (b.kind == "bear" and not b.breaker) or (b.kind == "bull" and b.breaker)


def flags(charted_blocks, price: float) -> tuple[bool, float | None]:
    """(ob_resist_0.3, ob_room_pct) for a price against charted blocks.
    Mirrors tools/studies/ob_fills_daily.py exactly."""
    from tools import order_blocks as OB
    px = float(price)
    resist = bool(OB.overhead_resistance(charted_blocks, px, WITHIN_PCT))
    res = [b for b in charted_blocks if _resistance(b)]
    if any(b.btm <= px <= b.top for b in res):
        room = 0.0
    else:
        above = [b.btm for b in res if b.btm > px]
        room = (min(above) / px - 1.0) * 100.0 if above else None
    return resist, room


def breakout_dist(charted_blocks, price: float, now: float) -> float | None:
    """% above the top of the most recently broken charted bearish OB (break within 15 min, price above it)."""
    px = float(price)
    rec = [b for b in charted_blocks if b.kind == "bear" and b.breaker and b.break_ts is not None
           and float(now) - 900.0 <= b.break_ts <= float(now) and px > b.top]
    if not rec:
        return None
    return (px / max(rec, key=lambda b: b.break_ts).top - 1.0) * 100.0


def fields(symbol: str, price: Any, now: float) -> dict[str, Any]:
    """The logged fields for one decision, or {} when there is nothing to say
    (no price, no bars, any error). Never raises, never fetches."""
    try:
        sym = str(symbol or "").upper().strip()
        px = float(price)
        if not sym or not (px > 0):
            return {}
        ch, n, prior = _charted_at(sym, float(now))
        if n <= 0:
            return {}
        resist, room = flags(ch, px)
        brk = breakout_dist(ch, px, float(now))
        return {
            "ob_resist_0.3": resist,
            "ob_room_pct": None if room is None else round(room, 3),
            "ob_brk_dist_pct": None if brk is None else round(brk, 3),
            "ob_bars": int(n),
            "ob_prior_day": bool(prior),
        }
    except Exception:  # noqa: BLE001
        return {}


FIELD_KEYS = ("ob_resist_0.3", "ob_room_pct", "ob_brk_dist_pct", "ob_bars", "ob_prior_day")


def _support(b) -> bool:
    return (b.kind == "bull" and not b.breaker) or (b.kind == "bear" and b.breaker)


def levels(symbol: str, price: Any) -> dict[str, Any]:
    """DISPLAY ONLY (dashboard book SR tooltip). Nearest charted support at or
    below `price` and nearest charted resistance at or above it, from the LAST
    block pass the poll already ran (``_CACHE``). Cache-only: never computes
    blocks, never fetches, never raises; {} when nothing is cached."""
    try:
        sym = str(symbol or "").upper().strip()
        px = float(price)
        if not sym or not (px > 0):
            return {}
        with _LOCK:
            hit = _CACHE.get(sym)
        if not hit:
            return {}
        key, ch, _n, _prior = hit
        out: dict[str, Any] = {"ob_levels_ts": float(key[0]) if key and key[0] else None}
        sup = [b for b in ch if _support(b) and b.btm <= px]
        res = [b for b in ch if _resistance(b) and b.top >= px]
        if sup:
            s = max(sup, key=lambda b: b.top)
            out["ob_sup_btm"], out["ob_sup_top"] = float(s.btm), float(s.top)
        if res:
            r = min(res, key=lambda b: b.btm)
            out["ob_res_btm"], out["ob_res_top"] = float(r.btm), float(r.top)
        return out
    except Exception:  # noqa: BLE001
        return {}


def copy_fields(src: Any, dst: dict) -> dict:
    """Copy whichever OB fields `src` carries onto `dst` (no keys when off)."""
    try:
        if isinstance(src, dict) and isinstance(dst, dict):
            for k in FIELD_KEYS:
                if k in src:
                    dst[k] = src[k]
    except Exception:  # noqa: BLE001
        pass
    return dst


def reset() -> None:
    with _LOCK:
        _BARS.clear()
        _CACHE.clear()
        _LAST_ABSORB.clear()
