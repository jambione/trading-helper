#!/usr/bin/env python3
"""bars_structure.py - shared, PURE bar-structure functions for the history studies (no network, no I/O).

Used by tools/studies/name_history.py (docs/studies/name_history_prereg.json, round_numbers_prereg.json) and
tools/studies/structure_pullback.py (docs/studies/structure_pullback_prereg.json). Every rule names its prereg clause.

Conventions
  * a 1-minute row is [start_ts, o, h, l, c, v] (Alpaca bar timestamps are bar STARTS); its close time is start + 60
  * Bar(start, end, o, h, l, c, v); a bar is COMPLETED at decision time t when end <= t (both preregs)
  * every function that takes a decision time t reads only bars with end <= t and swings with known_ts <= t
"""
from __future__ import annotations

import math
from collections import namedtuple
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

Bar = namedtuple("Bar", "start end o h l c v")
Swing = namedtuple("Swing", "kind i price start end known_ts")   # kind "H" | "L"

SWING_K = 2                    # swing: strict 2/2 rule
REJ_WICK, MOM_BODY = 0.50, 0.60  # rejection_bar lower wick >= 50% of range; momentum_bar body >= 60%
PIN_WICK = 0.60                # S5 PIN: lower wick >= 60% of the range, close in the upper third
EMA_N, EMA_SLOPE_LAG = 20, 3   # HTF_UP: close > EMA20 of 60-min closes and EMA20 > EMA20 three bars earlier
EMA_WARM = 20 * 7              # HTF_UP: >= 20 sessions x 7 bars of warm-up
NEW_HIGH_X, PRIOR_CLOSE_X = 1.001, 1.005   # name_history event (a), (b)
FT_UP, FT_DN, FT_SEC = 1.001, 0.997, 30 * 60   # T1 race


# ------------------------------------------------------------------ time
def et_ts(day: str, hh: int, mm: int = 0, ss: int = 0) -> float:
    d = datetime.strptime(day, "%Y-%m-%d")
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=ET).timestamp()


def et_hm(ts: float) -> int:
    """minutes after ET midnight"""
    d = datetime.fromtimestamp(float(ts), ET)
    return d.hour * 60 + d.minute


def et_hour(ts: float) -> int:
    return datetime.fromtimestamp(float(ts), ET).hour


def et_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), ET).strftime("%Y-%m-%d")


def rth_open(day: str) -> float:
    return et_ts(day, 9, 30)


# ------------------------------------------------------------------ bars
def minute_bars(rows, day: str, close_ts: float | None = None, rth=True) -> list:
    """1-minute rows -> Bars of `day` (RTH 09:30..close when rth, else 04:00..09:30 premarket), sorted, deduped."""
    o = rth_open(day)
    cl = close_ts or et_ts(day, 16)
    lo, hi = (o, cl) if rth else (et_ts(day, 4), o)
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r[0]):
        s = float(r[0])
        if lo <= s < hi and s not in seen:
            seen.add(s)
            out.append(Bar(s, s + 60.0, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0)))
    return out


def aggregate(bars1, day: str, size_min: int, close_ts: float | None = None) -> list:
    """RTH 1-min Bars -> size_min bars aligned to 09:30. The last bar ends at the session close (60-min: the
    15:30-16:00 half bar). A bucket with no 1-minute bar yields no bar."""
    o = rth_open(day)
    cl = close_ts or et_ts(day, 16)
    w = size_min * 60.0
    buckets = {}
    for b in bars1:
        if o <= b.start < cl:
            buckets.setdefault(int((b.start - o) // w), []).append(b)
    out = []
    for k in sorted(buckets):
        bs = sorted(buckets[k], key=lambda b: b.start)
        s = o + k * w
        out.append(Bar(s, min(s + w, cl), bs[0].o, max(b.h for b in bs), min(b.l for b in bs), bs[-1].c,
                       sum(b.v for b in bs)))
    return out


def completed(bars, t: float) -> list:
    """bars completed by t (end <= t). A forming bar is never returned."""
    return [b for b in bars if b.end <= t]


def scale(bars, f: float) -> list:
    """price-scale bars by a per-session adjustment factor (volume unchanged)."""
    return [Bar(b.start, b.end, b.o * f, b.h * f, b.l * f, b.c * f, b.v) for b in bars]


# ------------------------------------------------------------------ swings (strict 2/2, known at end of bar i+2)
def swings(bars, k: int = SWING_K) -> list:
    out = []
    for i in range(k, len(bars) - k):
        nb = [bars[j] for j in range(i - k, i + k + 1) if j != i]
        b = bars[i]
        known = bars[i + k].end
        if all(b.h > x.h for x in nb):
            out.append(Swing("H", i, b.h, b.start, b.end, known))
        if all(b.l < x.l for x in nb):
            out.append(Swing("L", i, b.l, b.start, b.end, known))
    return out


def swings_at(bars, t: float, k: int = SWING_K) -> list:
    """swings KNOWN by t: computed on completed bars only, and each needs known_ts <= t."""
    return [s for s in swings(completed(bars, t), k) if s.known_ts <= t]


def last_swing(sw, kind: str):
    xs = [s for s in sw if s.kind == kind]
    return xs[-1] if xs else None


def up60(sw) -> bool:
    """UP60: among swing points known by t, the last two swing highs ascending AND the last two swing lows ascending."""
    hs = [s.price for s in sw if s.kind == "H"]
    ls = [s.price for s in sw if s.kind == "L"]
    return len(hs) >= 2 and len(ls) >= 2 and hs[-1] > hs[-2] and ls[-1] > ls[-2]


def concat_sessions(per_day: list) -> list:
    """60-min swings run over the RTH 60-min bar sequence concatenated across sessions (overnight gap ignored)."""
    out = []
    for bars in per_day:
        out.extend(bars)
    return out


# ------------------------------------------------------------------ bar predicates (structure_pullback definitions)
def rejection_bar(b) -> bool:
    """range > 0, lower wick (min(open, close) - low) >= 50% of the range, close in the upper half (>= midpoint)."""
    rng = b.h - b.l
    return rng > 0 and (min(b.o, b.c) - b.l) >= REJ_WICK * rng and b.c >= b.l + 0.5 * rng


def momentum_bar(b, prev) -> bool:
    """close > open, body >= 60% of the range, close > the previous bar's high."""
    rng = b.h - b.l
    return prev is not None and rng > 0 and b.c > b.o and (b.c - b.o) >= MOM_BODY * rng and b.c > prev.h


def pin_bar(b) -> bool:
    """S5 PIN: lower wick >= 60% of the range and close in the upper third (>= low + 2/3 range)."""
    rng = b.h - b.l
    return rng > 0 and (min(b.o, b.c) - b.l) >= PIN_WICK * rng and b.c >= b.l + 2.0 * rng / 3.0


def bullish_fvg(bars, k: int):
    """S6: bullish FVG at displacement bar k when low(k+1) > high(k-1); FVG = [high(k-1), low(k+1)], KNOWN at the
    end of bar k+1. Returns (bottom, top, known_ts) or None."""
    if k < 1 or k + 1 >= len(bars):
        return None
    if bars[k + 1].l > bars[k - 1].h:
        return bars[k - 1].h, bars[k + 1].l, bars[k + 1].end
    return None


def is_green(b) -> bool:
    return b.c > b.o


def is_red(b) -> bool:
    return b.c < b.o


# ------------------------------------------------------------------ EMA / HTF_UP
def ema(xs, n: int = EMA_N) -> list:
    """EMA seeded at the first value (HTF_UP: seeded at the first look-back bar)."""
    out, a = [], 2.0 / (n + 1)
    for x in xs:
        out.append(x if not out else out[-1] + a * (x - out[-1]))
    return out


def htf15_up(bars15_today, t: float):
    """the 3 most recent COMPLETED 15-min bars B1<B2<B3: strictly ascending highs and lows (2 comparisons each).
    None if fewer than 3 completed bars."""
    c = completed(bars15_today, t)[-3:]
    if len(c) < 3:
        return None
    b1, b2, b3 = c
    return b2.h > b1.h and b3.h > b2.h and b2.l > b1.l and b3.l > b2.l


def htf60_up(bars60_series, t: float, warm: int = EMA_WARM):
    """last completed 60-min bar close > EMA20 of 60-min closes and EMA20 > EMA20 three bars earlier.
    None when fewer than `warm` completed bars precede the last one."""
    c = completed(bars60_series, t)
    if len(c) < warm + 1:
        return None
    e = ema([b.c for b in c])
    return c[-1].c > e[-1] and e[-1] > e[-1 - EMA_SLOPE_LAG]


def htf_up(bars15_today, bars60_series, t: float):
    a, b = htf15_up(bars15_today, t), htf60_up(bars60_series, t)
    if a is None or b is None:
        return None
    return bool(a and b)


# ------------------------------------------------------------------ name_history event and T1/T2/T3
def first_new_high(bars1, prior_close: float, lo_hm=10 * 60 + 30, hi_hm=15 * 60, x=NEW_HIGH_X,
                   px=PRIOR_CLOSE_X):
    """name_history event: the FIRST RTH 1-min bar t with lo <= close time <= hi such that close(t) >= x * max(high
    of RTH bars 09:30..t-1) and max(high 09:30..t) >= px * prior close. Returns a dict or None."""
    if not prior_close or prior_close <= 0:
        return None
    hmax = None
    for i, b in enumerate(bars1):
        if hmax is not None:
            hm = et_hm(b.end)
            if lo_hm <= hm <= hi_hm and b.c >= x * hmax and max(hmax, b.h) >= px * prior_close:
                return {"i": i, "t": b.end, "close": b.c, "h_prev": hmax, "hi_through": max(hmax, b.h)}
            if hm > hi_hm:
                return None
        hmax = b.h if hmax is None else max(hmax, b.h)
    return None


def first_new_low(bars1, prior_close: float, lo_hm=10 * 60 + 30, hi_hm=15 * 60):
    """short mirror (information): FIRST bar with close <= 0.999 x min(low 09:30..t-1) and min(low 09:30..t)
    <= 0.995 x prior close (a new session LOW on a down day)."""
    if not prior_close or prior_close <= 0:
        return None
    lmin = None
    for i, b in enumerate(bars1):
        if lmin is not None:
            hm = et_hm(b.end)
            if lo_hm <= hm <= hi_hm and b.c <= (2 - NEW_HIGH_X) * lmin and min(lmin, b.l) <= (2 - PRIOR_CLOSE_X) * prior_close:
                return {"i": i, "t": b.end, "close": b.c, "l_prev": lmin, "lo_through": min(lmin, b.l)}
            if hm > hi_hm:
                return None
        lmin = b.l if lmin is None else min(lmin, b.l)
    return None


def followthrough(bars1, ev, day: str) -> bool:
    """T1 race: SUCCESS if a later bar's high > close x 1.001 within 30 min BEFORE any bar's low <= close x 0.997;
    a bar doing both = FAILURE; neither within 30 min = FAILURE; the window ends by 15:30."""
    c, t = ev["close"], ev["t"]
    end = min(t + FT_SEC, et_ts(day, 15, 30))
    for b in bars1:
        if b.start < t:
            continue
        if b.end > end:
            break
        up, dn = b.h > c * FT_UP, b.l <= c * FT_DN
        if dn:
            return False
        if up:
            return True
    return False


def aligned_returns(bars_n) -> list:
    """[(bucket_start, ret)] for aligned bars of one session: close-to-close, the first bar from its own open."""
    out = []
    for j, b in enumerate(bars_n):
        base = bars_n[j - 1].c if j else b.o
        if base > 0:
            out.append((b.start, b.c / base - 1.0))
    return out


def lag1_pairs(bars_n, size_min: int) -> list:
    """consecutive non-overlapping bar returns within the session (adjacent buckets only)."""
    r = aligned_returns(bars_n)
    w = size_min * 60.0
    return [(a[1], b[1]) for a, b in zip(r, r[1:]) if abs(b[0] - a[0] - w) < 1e-6]


def pearson(pairs):
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    syy = sum((p[1] - my) ** 2 for p in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def ols_slope(pairs):
    """slope of y on x for (x, y) pairs."""
    n = len(pairs)
    if n < 2:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    if sxx <= 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pairs) / sxx


def trend_day(bars1) -> bool | None:
    """T3: |close - open| / (high - low) >= 0.6 over the session's RTH bars."""
    if not bars1:
        return None
    h, l = max(b.h for b in bars1), min(b.l for b in bars1)
    if h <= l:
        return None
    return abs(bars1[-1].c - bars1[0].o) / (h - l) >= 0.6


# ------------------------------------------------------------------ levels
def _q(x: float) -> int:
    """prices in integer 1/10000 dollars (exact grid comparisons)."""
    return int(round(float(x) * 10000))


def rn_step(close: float) -> float:
    """round_numbers primary grid by the event close: $0.50 under $50; $1 at $50-100; $5 at $100 and above."""
    return 0.5 if close < 50 else (1.0 if close < 100 else 5.0)


def grid_above(price: float, step: float, half_only=False) -> float:
    """nearest grid level STRICTLY above price (half_only: x.50 levels of a $1 grid)."""
    p, s = _q(price), _q(step)
    if half_only:
        off = s // 2
        k = (p - off) // s + 1
        return (k * s + off) / 10000.0
    return ((p // s) + 1) * s / 10000.0


def grid_crossed(lo: float, hi: float, step: float, half_only=False) -> bool:
    """a grid level L with lo < L <= hi exists."""
    a, b, s = _q(lo), _q(hi), _q(step)
    if b <= a:
        return False
    off = s // 2 if half_only else 0
    top = ((b - off) // s) * s + off     # largest level <= hi
    return top > a


def rn_group(close: float, h_prev: float, band: float = 0.0015, grid: str = "primary") -> str:
    """round_numbers groups (exclusive, exhaustive): BELOW if the nearest grid level strictly ABOVE the close is within
    `band` of the close (takes precedence); ABOVE if a level L with H_prev < L <= close; else NEUTRAL.
    grid: primary (price-scaled) | whole ($1 levels only) | half (x.50 levels only)."""
    if grid == "primary":
        step, half = rn_step(close), False
    elif grid == "whole":
        step, half = 1.0, False
    elif grid == "half":
        step, half = 1.0, True
    else:
        raise ValueError(grid)
    up = grid_above(close, step, half)
    if _q(up) - _q(close) <= band * _q(close):
        return "BELOW"
    if grid_crossed(h_prev, close, step, half):
        return "ABOVE"
    return "NEUTRAL"


def nh_round_levels_above(close: float, upto: float) -> list:
    """name_history LEVELS round numbers ($1 steps under $50, $5 above): levels in (close, upto]."""
    step = 1.0 if close < 50 else 5.0
    out, L = [], grid_above(close, step)
    while L <= upto + 1e-9 and len(out) < 1000:
        out.append(L)
        L = round(L + step, 4)
    return out


def nh_levels(prior_rth: list, prior_close: float, premarket_high, high20) -> list:
    """name_history LEVELS_info price levels (raw): prior 5 sessions' RTH highs and lows, prior close, today's
    premarket high, 20-session high. prior_rth = [(high, low)] of prior sessions, most recent LAST."""
    lv = []
    for h, l in prior_rth[-5:]:
        lv += [h, l]
    for x in (prior_close, premarket_high, high20):
        if x:
            lv.append(x)
    return [float(x) for x in lv if x]


def level_info(close: float, levels: list, conf_band: float = 0.002) -> dict:
    """distance (fraction) from the close to the nearest level above (round numbers included) and the count of
    levels within 0.20% above (confluence)."""
    rnd = nh_round_levels_above(close, close * (1 + conf_band))
    nxt_round = grid_above(close, 1.0 if close < 50 else 5.0)
    above = [x for x in levels if x > close] + [nxt_round]
    near = min(above) if above else None
    # one price is one level: the 20-session high usually equals one of the prior 5 highs, and a level can sit on
    # a round number - dedupe to the cent before counting (review round 1 note, 2026-10-08)
    conf = len({round(x * 100) for x in [x for x in levels if close < x <= close * (1 + conf_band)] + rnd})
    return {"dist": (near / close - 1.0) if near else None, "confluence": conf}


def s5_levels(bars60_series, t: float, today: str, sessions5: list, prior_rth_low) -> list:
    """S5 LEVELS known at t: 60-min swing lows (swing rule) whose bar lies in the prior 5 sessions or today, KNOWN by
    t, plus the prior session's RTH low."""
    keep = set(sessions5) | {today}
    lv = [s.price for s in swings_at(bars60_series, t) if s.kind == "L" and et_day(s.start) in keep]
    if prior_rth_low:
        lv.append(float(prior_rth_low))
    return lv


# ------------------------------------------------------------------ daily information cells
def atr14(daily_prior: list):
    """ATR14 from the prior 14 daily bars [(o, h, l, c)] (most recent last): mean true range (simple mean)."""
    if len(daily_prior) < 15:
        return None
    trs = []
    for j in range(len(daily_prior) - 14, len(daily_prior)):
        _, h, l, _c = daily_prior[j]
        pc = daily_prior[j - 1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / 14.0


def sma(xs: list, n: int = 50):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def realized_vol(closes: list, n: int = 20):
    """20-day realized vol: stdev of the last n daily log returns."""
    if len(closes) < n + 1:
        return None
    r = [math.log(closes[j] / closes[j - 1]) for j in range(len(closes) - n, len(closes)) if closes[j - 1] > 0]
    if len(r) < 2:
        return None
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
