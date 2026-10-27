#!/usr/bin/env python3
"""Pitch entry with the live trail as the exit. Spec: docs/studies/pitch_trail_prereg.json.

The entry is the pitch_open grade. The exit is the minute trail in that write-up.
The live arm stays out of this file. Entry lines come from pitch_open. The hold
readings are pasted below.

  (default)  count step. Trades, days, MDE, skips, exit reasons. No means.
  --canary   add 10 bp to a copy of the trade gross. Print the change only.
  --score    the ladder. Needs the round-2 review id, the floors, and canary_ok.
  --lines sip  information run. It has no pass of its own.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pitch_open as P  # noqa: E402

REVIEW_ID = "01a1796a-336d-7c71-8e25-7ab83afa0463"
FLATTEN = 949
DEAD_MIN = 22
LOOKBACK = 10
EPS = 0.05
FAST_LEN, FAST_SPAN = 21, 7.0
SLOW_LEN, SLOW_SPAN = 112, 3.0
DEC_LO, DEC_HI = P.DEC_LO, P.DEC_HI
REFRACT = P.REFRACT
HARD_STOP = 0.95
GREEN_BAND = 0.0005
EXIT_REASONS = ("stop", "leash", "dead", "flatten")
SKIP_KEYS = P.SKIP_KEYS
POP_TAGS = {
    "primary": ("ptrail-d", "ptrail-d2", "ptrail-x", "ptrail-x2"),
    "confirmed": ("ptrail-k-d", "ptrail-k-d2", "ptrail-k-x", "ptrail-k-x2"),
    "complement": ("ptrail-c-d", "ptrail-c-d2", "ptrail-c-x", "ptrail-c-x2"),
    "empty": ("ptrail-e-d", "ptrail-e-d2", "ptrail-e-x", "ptrail-e-x2"),
}


def cent_ceil(x: float) -> float:
    return math.ceil(round(x * 100.0, 4)) / 100.0


def cent_floor(x: float) -> float:
    return math.floor(round(x * 100.0, 4)) / 100.0


def initial_stop(entry: float) -> float:
    """Working stop at the fill. 0.12R, and never wider than 1% of price."""
    risk = 0.05 * entry
    give = min(0.12 * risk, entry * 0.01)
    stop = cent_ceil(entry - give)
    if stop >= entry:
        stop = entry - 0.01
    return stop


def held_leash_stop(stop: float, close: float) -> float:
    """A blocked leash parks the stop. Raise only."""
    return max(stop, round(close, 2))


def _raw_percent_r(hh: float, ll: float, close: float) -> float | None:
    span = hh - ll
    if span <= 0:
        return None
    return -100.0 * (hh - close) / span


def _live_percent_r_line(
    rows: list[tuple[float, float, float]],
    price: float,
    length: int,
    ewm_span: float,
    eps: float,
    *,
    min_range: int,
) -> tuple[float, bool, bool, str] | None:
    """(smoothed %R, rising, falling, src) against *price* as the live close."""
    if len(rows) < min_range:
        return None
    px = float(price)
    if len(rows) < length:
        hh = max([r[0] for r in rows] + [px])
        ll = min([r[1] for r in rows] + [px])
        live_raw = _raw_percent_r(hh, ll, px)
        if live_raw is None:
            return None
        prev_raw = _raw_percent_r(hh, ll, rows[-1][2])
        if prev_raw is None:
            prev_raw = live_raw
        return (
            live_raw,
            live_raw > prev_raw + eps,
            live_raw < prev_raw - eps,
            "clock_range",
        )
    series: list[float] = []
    for i in range(length - 1, len(rows)):
        win = rows[i - length + 1:i + 1]
        hh = max(r[0] for r in win)
        ll = min(r[1] for r in win)
        v = _raw_percent_r(hh, ll, win[-1][2])
        if v is not None:
            series.append(v)
    if not series:
        return None
    alpha = 2.0 / (max(1.0, float(ewm_span)) + 1.0)
    sm = series[0]
    for v in series[1:]:
        sm = alpha * v + (1.0 - alpha) * sm
    prev_sm = sm
    win = rows[-(length - 1):] if length > 1 else []
    hh = max([r[0] for r in win] + [px]) if win else px
    ll = min([r[1] for r in win] + [px]) if win else px
    live_raw = _raw_percent_r(hh, ll, px)
    if live_raw is None:
        return None
    live_sm = alpha * live_raw + (1.0 - alpha) * prev_sm
    return (
        live_sm,
        live_sm > prev_sm + eps,
        live_sm < prev_sm - eps,
        "live",
    )


def smoothed_at_closes(
    rows: list[tuple[float, float, float]],
    length: int,
    span: float,
) -> list[float | None]:
    """Smoothed %R at each closed bar. None until the window is full.

    Same raw window and EWM as the live reading: undefined bars
    (zero range) are skipped, and the smoother carries forward.
    """
    length = max(2, int(length))
    alpha = 2.0 / (max(1.0, float(span)) + 1.0)
    out: list[float | None] = [None] * len(rows)
    sm: float | None = None
    for i in range(len(rows)):
        if i + 1 < length:
            continue
        win = rows[i - length + 1:i + 1]
        hh = max(r[0] for r in win)
        ll = min(r[1] for r in win)
        raw = _raw_percent_r(hh, ll, win[-1][2])
        if raw is None:
            continue
        sm = raw if sm is None else alpha * raw + (1.0 - alpha) * sm
        out[i] = sm
    return out


def live_value(
    rows: list[tuple[float, float, float]],
    price: float,
    length: int,
    span: float,
) -> float | None:
    """Current smoothed %R with price as the forming close.

    clock_range (a short window that is not a real %R of length) is not a reading.
    """
    got = _live_percent_r_line(
        rows, float(price), int(length), float(span), 0.0, min_range=int(length))
    if got is None or got[3] != "live":
        return None
    return float(got[0])


def line_window(
    rows: list[tuple[float, float, float]],
    price: float,
    length: int,
    span: float,
    lookback: int,
) -> list[float] | None:
    """Last lookback - 1 closed readings plus the live reading.

    None when either piece is missing. A hole in the closed tail is a
    missing lookback, not a value we invent.
    """
    lookback = int(lookback)
    if lookback < 2 or price is None or price <= 0 or not rows:
        return None
    live = live_value(rows, price, length, span)
    if live is None:
        return None
    need = lookback - 1
    hist = smoothed_at_closes(rows, length, span)
    if len(hist) < need:
        return None
    tail = hist[-need:]
    if any(v is None for v in tail):
        return None
    return [float(v) for v in tail] + [live]


def line_trend(values: list[float] | None, eps: float = EPS) -> str:
    """up, down, sideways, or no_lookback.

    Down means the live reading (the last point) has come off the high of
    the rest of the window by more than eps. Up means the live reading is a
    new high. Sitting on the high is sideways.
    """
    if not values or any(v is None for v in values) or len(values) < 2:
        return "no_lookback"
    live = float(values[-1])
    prior_high = max(float(v) for v in values[:-1])
    if live < prior_high - abs(eps):
        return "down"
    if live > prior_high + abs(eps):
        return "up"
    return "sideways"


def _row(*, sell, trend, fast_trend, slow_trend, fast, slow, lookback, reason) -> dict:
    def end(vals, i):
        if not vals or len(vals) <= i or vals[i] is None:
            return None
        return round(float(vals[i]), 2)

    return {
        "sell": sell,
        "trend": trend,
        "fast_trend": fast_trend,
        "slow_trend": slow_trend,
        "fast_now": end(fast, -1),
        "slow_now": end(slow, -1),
        "fast_then": end(fast, 0),
        "slow_then": end(slow, 0),
        "lookback": lookback,
        "reason": reason,
    }


def decide(
    fast: list[float] | None,
    slow: list[float] | None,
    *,
    lookback: int,
    eps: float = EPS,
) -> dict:
    """Sell call from the two lookback windows. Each window ends at the live reading."""
    lookback = int(lookback)
    fast_w = list(fast[-lookback:]) if fast else None
    slow_w = list(slow[-lookback:]) if slow else None
    if (
        not fast_w or not slow_w
        or len(fast_w) < lookback or len(slow_w) < lookback
    ):
        fast_trend = line_trend(fast_w, eps) if fast_w and len(fast_w) >= lookback else "no_lookback"
        slow_trend = line_trend(slow_w, eps) if slow_w and len(slow_w) >= lookback else "no_lookback"
        return _row(
            sell="yes", trend="no_lookback",
            fast_trend=fast_trend, slow_trend=slow_trend,
            fast=fast_w, slow=slow_w, lookback=lookback,
            reason="no %R lookback",
        )
    fast_trend = line_trend(fast_w, eps)
    slow_trend = line_trend(slow_w, eps)
    if fast_trend == "no_lookback" or slow_trend == "no_lookback":
        trend, sell, reason = "no_lookback", "yes", "no %R lookback"
    elif fast_trend == "down" and slow_trend == "down":
        trend, sell, reason = "down", "yes", "both %R lines trending down"
    elif fast_trend == "up" and slow_trend == "up":
        trend, sell, reason = "up", "no", "both %R lines trending up"
    else:
        trend, sell, reason = "sideways", "no", "up or sideways"
    return _row(
        sell=sell, trend=trend,
        fast_trend=fast_trend, slow_trend=slow_trend,
        fast=fast_w, slow=slow_w, lookback=lookback, reason=reason,
    )


def _carry(sm: list[float | None]) -> list[float | None]:
    out: list[float | None] = []
    last = None
    for v in sm:
        if v is not None:
            last = v
        out.append(last)
    return out


def live_from_closed(rows, price, length, span, prev_sm):
    """Live smoothed value from a closed-bar EWM. Matches live_value on a full prefix."""
    if price is None or price <= 0 or len(rows) < length or prev_sm is None:
        return None
    px = float(price)
    win = rows[-(length - 1):] if length > 1 else []
    hh = max([r[0] for r in win] + [px]) if win else px
    ll = min([r[1] for r in win] + [px]) if win else px
    live_raw = _raw_percent_r(hh, ll, px)
    if live_raw is None:
        return None
    alpha = 2.0 / (max(1.0, float(span)) + 1.0)
    return alpha * live_raw + (1.0 - alpha) * float(prev_sm)


def build_hold_cache(bars: dict) -> dict:
    """SIP (high, low, close) through minute 949, smoothed once."""
    mins = sorted(m for m in bars if m <= FLATTEN)
    hlc = [(float(bars[m][1]), float(bars[m][2]), float(bars[m][3])) for m in mins]
    fast_sm = smoothed_at_closes(hlc, FAST_LEN, FAST_SPAN)
    slow_sm = smoothed_at_closes(hlc, SLOW_LEN, SLOW_SPAN)
    return {
        "mins": mins,
        "hlc": hlc,
        "fast_sm": fast_sm,
        "slow_sm": slow_sm,
        "fast_carry": _carry(fast_sm),
        "slow_carry": _carry(slow_sm),
    }


def _window_at(sm, carry, rows, k, price, length, span, need):
    if price is None or price <= 0 or k < need:
        return None
    tail = sm[k - need:k]
    if len(tail) < need or any(v is None for v in tail):
        return None
    prev = carry[k - 1] if k else None
    live = live_from_closed(rows, price, length, span, prev)
    if live is None:
        return None
    return [float(v) for v in tail] + [float(live)]


def windows_before(cache: dict, minute: int, price: float):
    """Closed bars strictly before minute, plus price as the live close."""
    k = bisect.bisect_left(cache["mins"], minute)
    rows = cache["hlc"][:k]
    need = LOOKBACK - 1
    fast = _window_at(
        cache["fast_sm"], cache["fast_carry"], rows, k, price, FAST_LEN, FAST_SPAN, need)
    slow = _window_at(
        cache["slow_sm"], cache["slow_carry"], rows, k, price, SLOW_LEN, SLOW_SPAN, need)
    return fast, slow


def hold_blocks(exit_price: float, entry: float, minute: int, cache: dict) -> bool:
    """True only when the sale is inside the band and decide says sell no.

    An exit at or under 5% below the fill always sells. An exit more than 5 bp
    above the fill always sells. A missing lookback sells.
    """
    if exit_price <= entry * HARD_STOP:
        return False
    if exit_price > entry * (1.0 + GREEN_BAND):
        return False
    fast, slow = windows_before(cache, minute, exit_price)
    return decide(fast, slow, lookback=LOOKBACK, eps=EPS)["sell"] == "no"


def flatten_price(bars: dict, entry_minute: int):
    """Close of 949 when that close is > 0, else the latest close > 0 in [entry, 949]."""
    bar = bars.get(FLATTEN)
    if bar is not None and bar[3] > 0:
        return bar[3]
    best_m = None
    best = None
    for m, b in bars.items():
        if m < entry_minute or m > FLATTEN or b[3] <= 0:
            continue
        if best_m is None or m > best_m:
            best_m = m
            best = b[3]
    return best


def _shelf(high: float, risk: float) -> float:
    give = min(0.12 * risk, high * 0.01)
    shelf = cent_ceil(high - give)
    if shelf > high - 0.01:
        shelf = cent_floor(high - 0.01)
    return shelf


def trail_exit(bars: dict, entry_minute: int, cache: dict | None = None):
    """(price, minute, reason) or None.

    The stop in force at the start of the minute is tested before this high
    can raise it. A giveback inside the peak minute waits for the next minute.
    """
    if cache is None:
        cache = build_hold_cache(bars)
    first = bars.get(entry_minute)
    if first is None or first[0] <= 0:
        return None
    entry = float(first[0])
    risk = 0.05 * entry
    if risk <= 0:
        return None
    stop = initial_stop(entry)
    peak = entry
    idle = 0
    m = entry_minute
    while m <= FLATTEN:
        bar = bars.get(m)
        open_ok = bar is not None and bar[0] > 0
        if not open_ok:
            idle += 60
            if m == FLATTEN:
                px = flatten_price(bars, entry_minute)
                if px is None:
                    return None
                return (px, FLATTEN, "flatten")
            m += 1
            continue
        o, h, l, c = (float(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]))
        # Step 1. Prior stop, before this high.
        hit = None
        if o <= stop:
            hit = o
        elif l <= stop:
            hit = stop
        if hit is not None and not hold_blocks(hit, entry, m, cache):
            return (hit, m, "stop")
        # Step 2. Peak and idle. A strict new high clears the leash clock.
        if h > peak:
            peak = h
            idle = 0
        else:
            idle += 60
        mfe_r = (peak - entry) / risk
        # Step 3. Breakeven runs whether or not the trail is armed.
        if mfe_r >= 0.15 or (peak - entry) / entry >= 0.0015:
            be = entry + 0.01
            if be > stop:
                stop = be
        # Step 4. Armed shelf. Never lowered.
        if mfe_r >= 0.06:
            raised = max(stop, cent_floor(peak * (1.0 - 0.35 / 100.0)), _shelf(h, risk))
            if raised > stop:
                stop = raised
        # Step 5. A close at or under the fill clears the green idle.
        if c <= entry:
            idle = 0
        elif c > entry:
            # Step 6. Leash. Remainder stays on the clock.
            steps = idle // 15
            idle = idle % 15
            if steps:
                candidate = cent_ceil(stop + steps * 0.05 * risk)
                if candidate >= c - 0.01:
                    if not hold_blocks(c, entry, m, cache):
                        return (c, m, "leash")
                    stop = held_leash_stop(stop, c)
                else:
                    cap = c - 0.01
                    raised = candidate if candidate <= cap else cap
                    if raised > stop:
                        stop = raised
        # Step 7. Dead trade at the close. The hold does not apply.
        if (m - entry_minute) >= DEAD_MIN and mfe_r < 0.10 and stop <= entry and c > 0:
            return (c, m, "dead")
        # Step 8. Only minute 949, and only while still open.
        if m == FLATTEN:
            px = flatten_price(bars, entry_minute)
            if px is None:
                return None
            return (px, FLATTEN, "flatten")
        m += 1
    return None


def close_minutes(bars: dict) -> list:
    return sorted(m for m, b in bars.items() if m <= FLATTEN and b[3] > 0)


def minute_can_exit(bars: dict, minute: int, closes: list) -> bool:
    """A positive close in [minute, 949] means the walk can flatten. Otherwise walk it."""
    i = bisect.bisect_left(closes, minute)
    if i < len(closes) and closes[i] <= FLATTEN:
        return True
    return trail_exit(bars, minute) is not None


def same_name_minutes(bars: dict, entry: int, closes: list) -> list:
    """Later minutes, ascending, each able to fill and to exit."""
    hi = min(entry + P.CTRL_SPAN, FLATTEN)
    out = []
    for c in range(entry + 1, hi + 1):
        b = bars.get(c)
        if b is None or b[0] <= 0:
            continue
        if minute_can_exit(bars, c, closes):
            out.append(c)
    out.sort()
    return out


def other_names(sips: dict, pool: list, sym: str, entry: int, closes: dict) -> list:
    out = []
    for n in pool:
        if n == sym:
            continue
        b = sips[n].get(entry)
        if b is None or b[0] <= 0:
            continue
        if minute_can_exit(sips[n], entry, closes[n]):
            out.append(n)
    out.sort()
    return out


def onset_minutes(pred) -> list:
    """First minute of a run, inside 09:45..15:20. The clock is not applied here."""
    out = []
    prev = bool(pred(DEC_LO - 1))
    for t in range(DEC_LO, DEC_HI + 1):
        cur = bool(pred(t))
        if cur and not prev:
            out.append(t)
        prev = cur
    return out


def run_clock(onsets, classify):
    """Yield misses and fills.

    A minute before the window does not move the clock. A fill locks until the
    exit minute, so the next onset is strictly after that minute. A miss locks
    15 minutes from the signal and does not open a position.
    classify(t) -> ("miss", reason) or ("fill", price, exit_minute, reason).
    """
    lock = None
    for t in onsets:
        if t < DEC_LO or t > DEC_HI:
            continue
        if lock is not None and t <= lock:
            continue
        kind = classify(t)
        if kind[0] == "miss":
            lock = t + REFRACT - 1
            yield ("miss", t, kind[1])
            continue
        _, px, exit_m, reason = kind
        lock = exit_m
        yield ("fill", t, px, exit_m, reason)


def pick_gross(cands, sym, day, entry_min, tag, gross_fn):
    if not cands:
        return None
    pick = P.seeded(sym, day, entry_min, tag).choice(cands)
    return gross_fn(pick)


def _check_partition(rows) -> None:
    for r in rows:
        f, s, pop = r.get("f"), r.get("s"), r.get("pop")
        if pop == "primary":
            if not (f is not None and s is not None and P.state_fs((f, s))):
                raise SystemExit("predicate partition broken")
            if P.is_same(f, s) == P.is_steep(f, s):
                raise SystemExit("predicate partition broken")
        elif pop == "complement":
            if not (f is not None and s is not None and P.complement_fs((f, s))):
                raise SystemExit("predicate partition broken")


def prereg_path() -> str:
    return os.path.join(P.ROOT, "docs", "studies", "pitch_trail_prereg.json")


def prereg_sha() -> str:
    return hashlib.sha256(open(prereg_path(), "rb").read()).hexdigest()


def module_shas() -> dict:
    d = os.path.dirname(os.path.abspath(__file__))
    return {
        m: hashlib.sha256(open(os.path.join(d, f"{m}.py"), "rb").read()).hexdigest()
        for m in ("pitch_trail", "pitch_open", "eod_reversal", "indicator_confirm_hist")
    }


def out_dir_for(lines: str) -> str:
    name = "pitch_trail" if lines == "iex" else "pitch_trail_sip_lines"
    return os.path.join(P.ROOT, "ai_reports", name)


def write_json(path: str, obj, pretty: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        if pretty:
            json.dump(obj, f, indent=1, default=str)
        else:
            json.dump(obj, f, separators=(",", ":"), default=str)
    os.replace(tmp, path)


def day_task(args) -> dict:
    day, prev_day, line_feed = args
    import indicator_confirm_hist as H
    sip_raw = H.load_raw("sip", day)
    lf_today = H.load_raw(line_feed, day)
    lf_prev = H.load_raw(line_feed, prev_day)
    sip_prev = H.load_raw("sip", prev_day)
    sips = {
        s: {int(r[0]): (float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows}
        for s, rows in sip_raw.items()
    }
    pool = sorted(sips)
    closes = {s: close_minutes(sips[s]) for s in pool}
    half = P.half_of(day)
    st, out = Counter(), []
    hold_caches: dict = {}
    walks: dict = {}
    x_lists: dict = {}

    def hold_of(sym):
        c = hold_caches.get(sym)
        if c is None:
            c = build_hold_cache(sips[sym])
            hold_caches[sym] = c
        return c

    def walk(sym, minute):
        key = (sym, minute)
        if key not in walks:
            walks[key] = trail_exit(sips[sym], minute, hold_of(sym))
        return walks[key]

    def gross_of(sym, minute):
        b = sips[sym].get(minute)
        got = walk(sym, minute)
        if got is None or b is None or b[0] <= 0:
            return None
        return got[0] / b[0] - 1.0

    def x_at(entry):
        names = x_lists.get(entry)
        if names is None:
            names = other_names(sips, pool, "", entry, closes)
            x_lists[entry] = names
        return names

    for sym in pool:
        if sym not in lf_today:
            st[f"{half}|no_line_feed"] += 1
            continue
        if sym not in lf_prev:
            st[f"{half}|no_previous_session"] += 1
            continue
        if not P.series_ok(lf_today[sym]) or not P.series_ok(lf_prev[sym]):
            st[f"{half}|bad_line_series"] += 1
            continue
        if sym in sip_prev and len(sip_prev[sym]) and len(sip_raw[sym]):
            pc, op = float(sip_prev[sym][-1][4]), float(sip_raw[sym][0][1])
            if pc > 0 and abs(op / pc - 1) > P.SPLIT_JUMP:
                st[f"{half}|split_jump"] += 1
                continue
        fast, slow = P.lines(lf_prev[sym], lf_today[sym])
        if not any(DEC_LO <= m <= DEC_HI for m in fast):
            st[f"{half}|no_warmup"] += 1
            continue
        bars = sips[sym]

        def pred_state(t, _fast=fast, _slow=slow):
            return P.state_fs(P.pair(_fast, _slow, t))

        def pred_confirmed(t, _fast=fast, _slow=slow):
            return P.state_fs(P.pair(_fast, _slow, t)) and P.state_fs(P.pair(_fast, _slow, t - 1))

        def pred_comp(t, _fast=fast, _slow=slow):
            return P.complement_fs(P.pair(_fast, _slow, t))

        def pred_empty(t, _fast=fast, _slow=slow):
            return P.empty_square(_fast, _slow, t)

        pops = (
            ("primary", pred_state),
            ("confirmed", pred_confirmed),
            ("complement", pred_comp),
            ("empty", pred_empty),
        )
        for pop, pred in pops:
            tags = POP_TAGS[pop]

            def classify(t, _sym=sym, _bars=bars):
                e = t + 1
                b = _bars.get(e)
                if b is None or b[0] <= 0:
                    return ("miss", "missing_entry")
                got = walk(_sym, e)
                if got is None:
                    return ("miss", "missing_exit")
                px, exit_m, reason = got
                return ("fill", px, exit_m, reason)

            for event in run_clock(onset_minutes(pred), classify):
                if event[0] == "miss":
                    st[f"{half}|{event[2]}"] += 1
                    continue
                _, t, px, _exit_m, reason = event
                e = t + 1
                if pop == "primary":
                    st[f"{half}|exit|{reason}"] += 1
                entry_open = bars[e][0]
                d = pick_gross(
                    same_name_minutes(bars, e, closes[sym]), sym, day, e, tags[0],
                    lambda c, _s=sym: gross_of(_s, c))
                d2 = pick_gross(
                    same_name_minutes(bars, e, closes[sym]), sym, day, e, tags[1],
                    lambda c, _s=sym: gross_of(_s, c))
                base = [n for n in x_at(e) if n != sym]
                x = pick_gross(base, sym, day, e, tags[2], lambda n, _e=e: gross_of(n, _e))
                x2 = pick_gross(base, sym, day, e, tags[3], lambda n, _e=e: gross_of(n, _e))
                if d is None or d2 is None or x is None or x2 is None:
                    st[f"{half}|missing_control"] += 1
                    continue
                fs = P.pair(fast, slow, t)
                f = s = None
                if fs is not None:
                    f, s = fs
                out.append({
                    "day": day, "sym": sym, "pop": pop, "dec_min": t, "entry_min": e,
                    "f": f, "s": s, "gross": px / entry_open - 1.0,
                    "d": d, "d2": d2, "x": x, "x2": x2,
                    "fast_under": bool(t in fast and t in slow and fast[t] < slow[t]),
                })
    return {"rows": out, "stats": dict(st)}


def _primary(rows):
    return [r for r in rows if r.get("pop") == "primary"]


def count_body(rows, st: Counter) -> dict:
    _check_partition(rows)
    prim = _primary(rows)
    cnt = {
        "stats_skips": {h: {k: int(st.get(f"{h}|{k}", 0)) for k in SKIP_KEYS} for h in ("A", "B")},
        "exit_reasons": {
            h: {k: int(st.get(f"{h}|exit|{k}", 0)) for k in EXIT_REASONS} for h in ("A", "B")
        },
        "module_sha256": module_shas(),
        "prereg_sha256": prereg_sha(),
    }
    for h in ("A", "B"):
        hr = [r for r in prim if P.half_of(r["day"]) == h]
        md, mx = P.mde(hr, "d", "d2"), P.mde(hr, "x", "x2")
        days_n = len({r["day"] for r in hr})
        same = sum(1 for r in hr if P.is_same(r["f"], r["s"]))
        steep = sum(1 for r in hr if P.is_steep(r["f"], r["s"]))
        if same + steep != len(hr):
            raise SystemExit("predicate partition broken")
        cnt[h] = {
            "trades": len(hr),
            "days": days_n,
            "mde_d": md,
            "mde_x": mx,
            "floors_met": bool(
                len(hr) >= P.FLOOR_TRADES and days_n >= P.FLOOR_DAYS
                and md is not None and md <= P.LIFT_BAR and mx is not None and mx <= P.LIFT_BAR
            ),
            "cells": {
                "primary": len(hr),
                "same": same,
                "fast_steeper": steep,
                "fast_under": sum(1 for r in hr if r.get("fast_under")),
                "confirmed": sum(1 for r in rows if r["pop"] == "confirmed" and P.half_of(r["day"]) == h),
                "complement": sum(1 for r in rows if r["pop"] == "complement" and P.half_of(r["day"]) == h),
                "empty_square": sum(1 for r in rows if r["pop"] == "empty" and P.half_of(r["day"]) == h),
            },
        }
    return cnt


def load_or_build(lines: str, workers: int, prereg_commit: str):
    import indicator_confirm_hist as H
    from concurrent.futures import ProcessPoolExecutor
    dest = out_dir_for(lines)
    cache = os.path.join(dest, "rows.json")
    sha, psha = module_shas(), prereg_sha()
    if os.path.exists(cache):
        with open(cache) as f:
            c = json.load(f)
        if c.get("module_sha256") == sha and c.get("prereg_sha256") == psha:
            return c["rows"], Counter(c.get("stats") or {}), dest
    days = H.warehouse_days()
    run = [(d, days[i - 1], lines) for i, d in enumerate(days) if P.FIRST <= d <= P.LAST and i >= 1]
    rows, st = [], Counter()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, res in enumerate(ex.map(day_task, run, chunksize=2), 1):
            rows.extend(res["rows"])
            st.update(res["stats"])
            print(f"session {i}/{len(run)}", file=sys.stderr, flush=True)
    write_json(cache, {
        "rows": rows, "stats": dict(st), "module_sha256": sha, "prereg_sha256": psha,
        "prereg_commit": prereg_commit, "lines": lines,
    })
    return rows, st, dest


def info_for(rows, h) -> dict:
    hr = [r for r in rows if r["pop"] == "primary" and P.half_of(r["day"]) == h]

    def sub(pred):
        return P.cell([r for r in hr if pred(r)])

    return {
        "same_only": sub(lambda r: P.is_same(r["f"], r["s"])),
        "fast_steeper_only": sub(lambda r: P.is_steep(r["f"], r["s"])),
        "fast_still_under_slow": sub(lambda r: bool(r.get("fast_under"))),
        "confirmed": P.cell([r for r in rows if r["pop"] == "confirmed" and P.half_of(r["day"]) == h]),
        "complement": P.cell([r for r in rows if r["pop"] == "complement" and P.half_of(r["day"]) == h]),
        "empty_square_shape": P.cell([r for r in rows if r["pop"] == "empty" and P.half_of(r["day"]) == h]),
    }


def run_canary(lines: str) -> None:
    dest = out_dir_for(lines)
    cache = os.path.join(dest, "rows.json")
    if not os.path.exists(cache):
        raise SystemExit("canary needs rows.json from this count")
    with open(cache) as f:
        c = json.load(f)
    if c.get("module_sha256") != module_shas() or c.get("prereg_sha256") != prereg_sha():
        raise SystemExit("rows.json is from different code or a different prereg")
    deltas = P.canary_deltas(c["rows"])
    ok = all(v is not None and abs(v - 0.0010) <= 1e-9 for v in deltas.values())
    obj = {"deltas": deltas, "canary_ok": ok, "module_sha256": module_shas(), "prereg_sha256": prereg_sha()}
    write_json(os.path.join(dest, "canary.json"), obj, pretty=True)
    print(json.dumps({"deltas": deltas, "canary_ok": ok}))


def run_score(lines: str, skeptic_review: str) -> None:
    dest = out_dir_for(lines)
    if skeptic_review != REVIEW_ID:
        raise SystemExit("score needs the round-2 review id")
    cnt_path = os.path.join(dest, "count.json")
    if not os.path.exists(cnt_path):
        raise SystemExit("score needs count.json")
    with open(cnt_path) as f:
        cnt = json.load(f)
    if cnt.get("module_sha256") != module_shas() or cnt.get("prereg_sha256") != prereg_sha():
        raise SystemExit("count.json from different code or a different prereg")
    if not cnt.get("prereg_commit"):
        raise SystemExit("count.json without --prereg-commit")
    if not all(cnt[h]["floors_met"] for h in ("A", "B")):
        print("UNDERPOWERED")
        return
    canary_path = os.path.join(dest, "canary.json")
    if not os.path.exists(canary_path):
        raise SystemExit("score needs canary.json")
    with open(canary_path) as f:
        canary = json.load(f)
    if (not canary.get("canary_ok") or canary.get("module_sha256") != module_shas()
            or canary.get("prereg_sha256") != prereg_sha()):
        raise SystemExit("canary_ok is missing on this code and prereg")
    with open(os.path.join(dest, "rows.json")) as f:
        rows = json.load(f)["rows"]
    _check_partition(rows)
    halves = {}
    for h in ("A", "B"):
        hr = [r for r in _primary(rows) if P.half_of(r["day"]) == h]
        halves[h] = P.ladder(hr)
    verdict = P.overall(halves)
    res = {
        "verdict": verdict,
        "halves": halves,
        "skeptic_review": skeptic_review,
        "mode1": "two-round rule, round 2 was FIX FIRST, not a READY",
        "lines": lines,
        "prereg_sha256": prereg_sha(),
        "module_sha256": module_shas(),
    }
    if verdict in ("PASS", "FAIL"):
        res["info"] = {h: info_for(rows, h) for h in ("A", "B")}
    write_json(os.path.join(dest, "result.json"), res, pretty=True)
    print(json.dumps(res, indent=1, default=str))


def run_count(lines: str, workers: int, prereg_commit: str) -> None:
    if not prereg_commit:
        raise SystemExit("count needs --prereg-commit")
    rows, st, dest = load_or_build(lines, workers, prereg_commit)
    body = count_body(rows, st)
    body["prereg_commit"] = prereg_commit
    body["lines"] = lines
    write_json(os.path.join(dest, "count.json"), body, pretty=True)
    shown = {k: body[k] for k in ("A", "B", "stats_skips", "exit_reasons", "lines") if k in body}
    print(json.dumps(shown, indent=1, default=str))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--canary", action="store_true")
    ap.add_argument("--skeptic-review", default="")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--prereg-commit", default="")
    ap.add_argument("--lines", default="iex", choices=("iex", "sip"))
    a = ap.parse_args()
    if REVIEW_ID not in open(prereg_path()).read():
        raise SystemExit("prereg is missing the round-2 review id")
    if a.canary:
        run_canary(a.lines)
        return
    if a.score:
        run_score(a.lines, a.skeptic_review)
        return
    run_count(a.lines, a.workers, a.prereg_commit)


if __name__ == "__main__":
    main()
