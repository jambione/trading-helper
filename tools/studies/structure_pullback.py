#!/usr/bin/env python3
"""structure_pullback.py - six price-action entry setups from the operator's 10/8 videos (S2 last-higher-low pullback,
S3 break and retest, S4 first candle to make a new high, S5 double bottom / pin at a key level, S6 sweep ->
displacement -> FVG retrace, S7 ICC second break), structure stop and 3R target, vs the SAME geometry entered at a
random time in the same name and session.

Exactly per docs/studies/structure_pullback_prereg.json (amended through amended_6, all before any script or data).
Ambiguities are resolved in RESOLUTIONS (also written to result.json).

Commands (on the mini; fetch AFTER HOURS ONLY):
  fetch            bars (shared cache with name_history.py), entry/exit quotes for both arms, SPY
  count            OUTCOME-BLIND power step: event counts and distinct names per setup per half; projected
                   SE = sqrt(2) x the two-way SE of the CONTROL arm's net (no event outcome is read); extension
                   decision (any setup's projected MDE > 15 bp -> extend to 2025-11-03 BEFORE scoring)
  score            both arms, paired differences, verdicts                                -> WORK/result.json
  report           WORK/report.md
  --extend         primary sample from 2025-11-03 (after the count step says so)

USAGE (mini):  .venv/bin/python tools/studies/structure_pullback.py fetch|count|score|report [--extend]
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import random
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (ROOT, os.path.join(ROOT, "tools", "studies")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import bars_structure as BS  # noqa: E402
import bro_sr_wr as BRO  # noqa: E402
import name_history as NH  # noqa: E402

WORK = os.environ.get("STRUCTURE_WORK") or os.path.join(ROOT, "ai_reports", "structure_pullback")
PREREG = "docs/studies/structure_pullback_prereg.json"

DATA_LO, EXT_LO, TEST_HI = "2026-06-09", "2025-11-03", "2026-10-07"
STRUCT_LOOKBACK, BETA_LOOKBACK = 10, 20      # first 10 SESSIONS look-back; beta over the prior 20 sessions
SETUPS = ("S2", "S3", "S4", "S5", "S6", "S7")
MINUTE_SETUPS = ("S4", "S6")                 # minute-bar controls
WIN_LO, WIN_HI = 10 * 60 + 30, 14 * 60 + 30  # entries 10:30-14:30
R_LO, R_HI = 0.0015, 0.02                    # skip if R < 0.15% or > 2.0% of the entry
TARGET_R, INFO_R = 3.0, 2.0
MAX_HOLD = 80 * 60
LAST_EXIT = 15 * 60 + 50
ENTRY_LAG, QUOTE_MAX_AGE = 5.0, 5.0
CTRL_SEED, CTRL_GAP = 67, 60 * 60
Z_SP, UNDER_BP, PASS_BP, FAIL_T = 2.64, 15.0, 5.0, -2.0
TICK = 0.01

RESOLUTIONS = {
    "R1_bars": "as name_history R1: Alpaca bars are stamped at their start; 5/15/60-min RTH bars aligned to 09:30 "
               "from RAW 1-min bars; completed when end <= t; a bucket without a 1-min bar is no bar",
    "R2_window_entry": "the entry bar's close t must be 10:30-14:30 ET; when the FIRST qualifying entry bar of a "
                       "candidate lies outside the window, the candidate resolves without an entry (counted "
                       "<setup>_outside_window) and the scan continues after it",
    "R3_stale_entry": "a stale (> 5 s) or missing entry quote drops the pair (counted) AND uses the setup's one "
                      "trade that day (scanning on would pick a later signal conditional on quote coverage)",
    "R4_R_bounds": "R = entry mid - stop; R < 0.15% (incl. R <= 0) or > 2.0% of the entry mid is an R-bound skip",
    "R5_S3_break": "BREAK must be the bar immediately after the 4-bar consolidation (rolling: every bar end is a "
                   "consolidation check); body = |close - open|",
    "R6_S2_depth": "S2 touch depth: 'within_0.1pct' if |low / L - 1| <= 0.1%, else 'pierced'",
    "R7_S4_pullback_lows": "a red pullback bar with low <= the pole's first open or low < the pole midpoint ends the "
                           "candidate like a cancel; after a candidate ends on a non-red bar the pole scan restarts "
                           "AT that bar (it may begin the next pole)",
    "R8_S5_level_pick": "levels satisfying BOTH the +/-0.15% band and the approach-from-above rule; the nearest to "
                        "the touch low is used (tie: the lower level); levels known at the touch bar's end",
    "R9_S5_resume": "after a touch resolves (entry skip, cancel, 12-bar expiry, window) the next touch is scanned "
                    "from the bar after the resolving bar",
    "R10_S6_resume": "after a sweep candidate resolves, the next sweep is scanned from the first 5-min bar starting "
                     "at or after the resolving instant (no displacement: the bar after the 6-bar window)",
    "R11_S7_resume": "after an S7 candidate resolves, the next indication must start at or after the resolving "
                     "15-min bar's end; the pre-indication 60-min swing low = the most recent swing low known at "
                     "the indication bar's start (none -> no swing-low cancel)",
    "R12_time_exit": "time-exit instant = min(entry + 80 min, 15:50:00) with entry = t + 5 s; its fallback bar is "
                     "the 1-min bar ending at min(t + 80 min, 15:50) (or the last bar before it); stop/target bars "
                     "are those starting at >= t + 60 s and ending <= that fallback bar's end; a stale time-exit "
                     "quote -> the fallback close with the ENTRY spread as the exit cost (counted); no quote AND no "
                     "fallback bar -> no outcome: that arm is dropped (counted <arm>_time_exit_no_price)",
    "R13_spy": "a stale SPY quote at either hedge instant makes that arm's hedged value missing (counted); the pair "
               "then leaves the hedged series only",
    "R14_control_draw": "control candidates are CLOCK times (15-min closes 10:30, 10:45 ... 14:30; or every 1-min "
                        "close 10:30..14:30 for S4/S6) with |c - t| > 60 min; no bar data is read to choose; "
                        "random.Random(int(sha256('67|SETUP|SYM|DAY').hexdigest()[:16], 16)).choice(sorted)",
    "R15_control_geometry": "control stop = entry x (1 - R%/100), target = entry x (1 + 3 R%/100), R% from the "
                            "event's entry mid and stop",
    "R16_drop_rate": "event drop rate = (stale entries + event time exits with no price) / event signals; control "
                     "drop rate = (no legal bar + stale + no exit price) / event entries; FAILED-DATA when the pooled rates differ by > 2 pp (per setup also reported)",
    "R17_power": "count step uses the RAW control net; MDE = (t_crit(df, z 2.64) + 0.84) x sqrt(2) x SE_ctrl, "
                 "df = min(sessions, names) - 1 of the control sample; after scoring 'powered' = MDE <= 15 bp in "
                 "both halves on BOTH series",
    "R18_drop_share": "a unit's share = its summed paired difference / n; the 5 names / 3 sessions with the largest "
                      "share are dropped",
    "R19_not_implemented": "information cells NOT computed: 'setup 1 short mirror' (no coded rule for 'momentum "
                           "fade' / 'minor support' in the prereg) and 'the desk's own %R square ... same 3R bracket' "
                           "(the prereg gives no stop for the square's bracket); listed in the hand-back",
    "R20_early_close": "sessions with a calendar close before 16:00 are excluded (name_history R4)",
    "R21_r_multiple": "R-multiple = (exit price - entry mid) / R (before costs)",
    "R22_extension": "--extend applies the same session rule from 2025-11-03: the first 10 sessions are look-back "
                     "only; the count step is re-run on the extended sample (count_ext.json) before scoring",
    "R23_beta_missing": "a name-day without >= 100 beta pairs keeps its raw pair; its hedged values are missing "
                        "(counted beta_pairs_drop), so it leaves the hedged series only",
}


def P_(*a):
    print(*a, flush=True)


def in_window(t) -> bool:
    return WIN_LO <= BS.et_hm(t) <= WIN_HI


# ------------------------------------------------------------------ name-day context
class Ctx:
    """everything a scanner may read for one name-day. Scanners read bars only through completed()/swings_at()
    at their decision time; entry(t) returns (mid, spread) or None (stale)."""

    def __init__(self, sym, day, b1, b60_series, sessions5, prior_low, prior_close, entry):
        self.sym, self.day, self.b1 = sym, day, b1
        self.b5, self.b15 = BS.aggregate(b1, day, 5), BS.aggregate(b1, day, 15)
        self.b60, self.sessions5 = b60_series, sessions5
        self.prior_low, self.prior_close, self.entry = prior_low, prior_close, entry


def try_entry(ctx, setup, t, stop, counts, info=None, extra_skip=None):
    """('entry'|'stale'|'skip', signal|None)."""
    q = ctx.entry(t)
    if q is None:
        counts[f"{setup}_stale_entry"] += 1
        return "stale", {"setup": setup, "t": t, "stale": True, **(info or {})}
    mid, spr = q
    if extra_skip and extra_skip(mid):
        counts[f"{setup}_skip_guard"] += 1
        return "skip", None
    R = mid - stop
    if R < R_LO * mid or R > R_HI * mid:
        counts[f"{setup}_skip_R"] += 1
        return "skip", None
    counts[f"{setup}_entry"] += 1
    return "entry", {"setup": setup, "t": t, "entry": mid, "spread": spr, "stop": stop, "R": R,
                     "r_pct": R / mid, **(info or {})}


def _first_idx(bars, hm):
    for i, b in enumerate(bars):
        if BS.et_hm(b.start) >= hm:
            return i
    return len(bars)


# ------------------------------------------------------------------ setups
def scan_S2(ctx, counts):
    b = ctx.b15
    i = max(1, _first_idx(b, 10 * 60))
    while i < len(b):
        bar, prev = b[i], b[i - 1]
        sw = BS.swings_at(ctx.b60, bar.end)
        Ls = BS.last_swing(sw, "L")
        if not (Ls and BS.up60(sw) and bar.l <= Ls.price * 1.001 and prev.l > Ls.price * 1.001):
            i += 1
            continue
        L = Ls.price
        counts["S2_touch"] += 1
        rej = 1 if BS.rejection_bar(bar) else 0
        res = i + 8
        for j in range(i + 1, min(i + 9, len(b))):
            bj = b[j]
            if bj.c < L * 0.99:
                counts["S2_cancel"] += 1
                res = j
                break
            if rej >= 2 and BS.momentum_bar(bj, b[j - 1]):
                res = j
                if not in_window(bj.end):
                    counts["S2_outside_window"] += 1
                    break
                stop = min(x.l for x in b[i:j + 1]) - TICK
                depth = "within_0.1pct" if abs(bar.l / L - 1) <= 0.001 else "pierced"
                st, sig = try_entry(ctx, "S2", bj.end, stop, counts, {"L": L, "depth": depth, "n_rej": rej})
                if st != "skip":
                    return sig
                break
            if BS.rejection_bar(bj):
                rej += 1
        else:
            counts["S2_expire"] += 1
        i = res + 1
    return None


def scan_S3(ctx, counts):
    b = ctx.b15
    e = 3
    while e + 1 < len(b):
        four = b[e - 3:e + 1]
        M = max(x.h for x in four)
        brk = b[e + 1]
        rng = brk.h - brk.l
        if (M - min(x.l for x in four)) <= 0.01 * four[-1].c and brk.c > M * 1.001 and rng > 0 \
                and abs(brk.c - brk.o) >= 0.5 * rng:
            counts["S3_break"] += 1
            res = e + 7
            for r in range(e + 2, min(e + 8, len(b))):
                br = b[r]
                if br.l <= M * 1.001 and br.c >= M and BS.rejection_bar(br):
                    res = r
                    if not in_window(br.end):
                        counts["S3_outside_window"] += 1
                        break
                    up = BS.up60(BS.swings_at(ctx.b60, br.end))
                    st, sig = try_entry(ctx, "S3", br.end, br.l - TICK, counts, {"M": M, "up60": up})
                    if st != "skip":
                        return sig
                    break
                if br.c < M:
                    counts["S3_cancel"] += 1
                    res = r
                    break
            else:
                counts["S3_expire"] += 1
            e = res + 4
            continue
        e += 1
    return None


def scan_S4(ctx, counts):
    b = ctx.b1
    n, i = len(b), 0
    while i < n:
        if not BS.is_green(b[i]):
            i += 1
            continue
        a, j = i, i
        while j + 1 < n and BS.is_green(b[j + 1]):
            j += 1
        pole = b[a:j + 1]
        p_open, p_high = pole[0].o, max(x.h for x in pole)
        if not (len(pole) >= 3 and pole[-1].c >= 1.005 * p_open and p_high >= max(x.h for x in b[:j + 1])):
            i = j + 1
            continue
        counts["S4_pole"] += 1
        mid = (p_open + p_high) / 2.0
        k, lows, cancelled = j + 1, [], False
        while k < n and BS.is_red(b[k]):
            if len(lows) >= 5 or b[k].c < p_open or b[k].l <= p_open or b[k].l < mid:
                cancelled = True
                break
            lows.append(b[k].l)
            k += 1
        if cancelled:
            counts["S4_cancel"] += 1
            i = k + 1
            continue
        if k >= n:
            break
        if len(lows) >= 2:
            ent = b[k]
            if ent.c > b[k - 1].h:
                t = ent.end
                hi_t = max(x.h for x in b[:k + 1])
                if not in_window(t):
                    counts["S4_outside_window"] += 1
                elif hi_t < 1.03 * ctx.prior_close:
                    counts["S4_below_3pct"] += 1
                else:
                    st, sig = try_entry(ctx, "S4", t, min(lows) - TICK, counts,
                                        {"pole_pct": pole[-1].c / p_open - 1.0})
                    if st != "skip":
                        return sig
            else:
                counts["S4_no_break"] += 1
        i = k
    return None


def scan_S5(ctx, counts):
    b = ctx.b15
    i = max(1, _first_idx(b, 10 * 60))
    while i < len(b):
        bar, prev = b[i], b[i - 1]
        lv = BS.s5_levels(ctx.b60, bar.end, ctx.day, ctx.sessions5, ctx.prior_low)
        cand = [L for L in lv if abs(bar.l - L) <= 0.0015 * L and prev.l > L * 1.0015]
        if not cand:
            i += 1
            continue
        L = min(cand, key=lambda x: (abs(bar.l - x), x))
        counts["S5_touch"] += 1
        tl = bar.l
        if BS.pin_bar(bar):
            res = i
            if not in_window(bar.end):
                counts["S5_outside_window"] += 1
            else:
                st, sig = try_entry(ctx, "S5", bar.end, tl - TICK, counts, {"kind": "pin", "level": L})
                if st != "skip":
                    return sig
        elif bar.c < L * 0.9985:
            counts["S5_cancel"] += 1
            res = i
        else:
            res = i + 12
            for j in range(i + 1, min(i + 13, len(b))):
                bj = b[j]
                if j - i >= 3 and abs(bj.l - tl) <= 0.0015 * tl and bj.c > bj.o:
                    res = j
                    if not in_window(bj.end):
                        counts["S5_outside_window"] += 1
                        break
                    st, sig = try_entry(ctx, "S5", bj.end, min(tl, bj.l) - TICK, counts,
                                        {"kind": "double_bottom", "level": L})
                    if st != "skip":
                        return sig
                    break
                if bj.c < L * 0.9985:
                    counts["S5_cancel"] += 1
                    res = j
                    break
            else:
                counts["S5_expire"] += 1
        i = res + 1
    return None


def scan_S6(ctx, counts):
    b5, b1 = ctx.b5, ctx.b1
    s = 0
    while s < len(b5):
        bar = b5[s]
        SL = BS.last_swing(BS.swings_at(b5, bar.start), "L")
        if not SL or not (bar.l < SL.price and bar.c > SL.price):
            s += 1
            continue
        counts["S6_sweep"] += 1
        HH = max(x.h for x in b5[SL.i:s + 1])
        disp = None
        for k in range(s + 1, min(s + 7, len(b5))):
            if b5[k].c > HH:
                f = BS.bullish_fvg(b5, k)
                if f:
                    disp = (k, f)
                    break
        if not disp:
            s += 7
            continue
        k, (bot, top, known) = disp
        deadline = known + 12 * 300
        res_ts = deadline
        for m in b1:
            if m.start < known:
                continue
            if m.end > deadline:
                counts["S6_expire"] += 1
                break
            cx = [x for x in b5 if known < x.end <= m.start and x.c < bot]
            if cx:
                counts["S6_cancel"] += 1
                res_ts = cx[0].end
                break
            if m.l <= top:
                res_ts = m.end
                if not in_window(m.end):
                    counts["S6_outside_window"] += 1
                    break
                stop = bar.l - TICK
                st, sig = try_entry(ctx, "S6", m.end, stop, counts, {"fvg": [bot, top]},
                                    extra_skip=lambda mid, stop=stop, bot=bot: mid <= stop or mid < bot)
                if st != "skip":
                    return sig
                break
        else:
            counts["S6_expire"] += 1
        nxt = [ii for ii, x in enumerate(b5) if x.start >= res_ts]
        s = max(nxt[0], s + 1) if nxt else len(b5)
    return None


def scan_S7(ctx, counts):
    series, b15 = ctx.b60, ctx.b15
    today = [ii for ii, x in enumerate(series) if BS.et_day(x.start) == ctx.day]
    resume = None
    for ii in today:
        I = series[ii]
        if not (WIN_LO <= BS.et_hm(I.end) <= WIN_HI) or (resume is not None and I.start < resume) or ii == 0:
            continue
        sw = BS.swings_at(series, I.start)
        Hs = BS.last_swing(sw, "H")
        if not Hs:
            continue
        H = Hs.price
        if not (I.c > H and series[ii - 1].c <= H) or any(series[jj].c > H for jj in today if jj < ii):
            continue
        counts["S7_indication"] += 1
        SLs = BS.last_swing(sw, "L")
        pre_low = SLs.price if SLs else None
        corr, clow = False, math.inf
        resume = BS.et_ts(ctx.day, 16)
        for bj in b15:
            if bj.start < I.end:
                continue
            if BS.et_hm(bj.end) > WIN_HI:
                counts["S7_cancel_1430"] += 1
                resume = bj.end
                break
            if pre_low is not None and bj.c < pre_low:
                counts["S7_cancel_swing_low"] += 1
                resume = bj.end
                break
            if corr and bj.c > H * 1.001:
                resume = bj.end
                st, sig = try_entry(ctx, "S7", bj.end, clow - TICK, counts, {"H": H, "ind_high": I.h})
                if st != "skip":
                    return sig
                break
            clow = min(clow, bj.l)
            if bj.c < H:
                corr = True
    return None


SCANNERS = {"S2": scan_S2, "S3": scan_S3, "S4": scan_S4, "S5": scan_S5, "S6": scan_S6, "S7": scan_S7}


# ------------------------------------------------------------------ exits
def simulate(data, sym, b1, t, entry, spread, stop, target, beta, counts=None, tag=""):
    """common exit rules. Returns raw/hedged net (bp) for the primary fill and the stop-slippage cell, or None when
    a time exit has neither a quote nor a fallback bar (no exit price)."""
    counts = counts if counts is not None else collections.Counter()
    day = BS.et_day(t)
    te = t + ENTRY_LAG
    last = BS.et_ts(day, LAST_EXIT // 60, LAST_EXIT % 60)
    q_time = min(te + MAX_HOLD, last)
    bar_end = min(t + MAX_HOLD, last)
    R = entry - stop
    ex = None
    for b in b1:
        if b.start < t + 60:
            continue
        if b.end > bar_end:
            break
        if b.l <= stop:
            ex = {"type": "stop", "px": min(stop, b.o), "px_slip": min(b.o, (stop + b.l) / 2.0), "ts": b.end}
            break
        if b.h >= target:
            ex = {"type": "target", "px": target, "px_slip": target, "ts": b.end}
            break
    if ex:
        q = NH.mid_at(data, sym, ex["ts"], QUOTE_MAX_AGE)
        if q:
            sx = q[1]
        else:
            sx = spread
            counts[f"{tag}exit_quote_stale_entry_spread"] += 1
    else:
        q = NH.mid_at(data, sym, q_time, QUOTE_MAX_AGE)
        if q:
            px, sx = q[0], q[1]
        else:
            fb = [b for b in b1 if b.end <= bar_end]
            if not fb:
                # no quote AND no bar: there is no exit price - booking px = entry would score a made-up -cost
                # outcome (review round 1 FF1, 2026-10-08); the caller drops the pair
                counts[f"{tag}time_exit_no_price"] += 1
                return None
            px, sx = fb[-1].c, spread
            counts[f"{tag}time_exit_bar_close_fallback"] += 1
        ex = {"type": "time", "px": px, "px_slip": px, "ts": q_time}
    cost = 0.5 * spread + 0.5 * sx
    out = {"exit": ex["type"], "hold_min": (ex["ts"] - te) / 60.0, "R_mult": (ex["px"] - entry) / R if R else None,
           "net": 1e4 * (ex["px"] / entry - 1.0 - cost), "net_slip": 1e4 * (ex["px_slip"] / entry - 1.0 - cost)}
    s0, s1 = NH.mid_at(data, NH.SPY, te), NH.mid_at(data, NH.SPY, ex["ts"])
    if s0 and s1 and beta is not None:
        spy = 1e4 * beta * (s1[0] / s0[0] - 1.0)
        out["hedged"], out["hedged_slip"] = out["net"] - spy, out["net_slip"] - spy
    else:
        out["hedged"] = out["hedged_slip"] = None
        counts[f"{tag}spy_stale"] += 1
    return out


def control_time(setup, sym, day, t):
    """R14: a clock-time draw that reads no data."""
    rng = random.Random(NH.sha_seed(f"{CTRL_SEED}|{setup}|{sym}|{day}"))
    step = 1 if setup in MINUTE_SETUPS else 15
    base = BS.et_ts(day, 0)
    c = [base + m * 60 for m in range(WIN_LO, WIN_HI + 1, step)]
    c = sorted(x for x in c if abs(x - t) > CTRL_GAP)
    return rng.choice(c) if c else None


def realized_vol30(b1, t):
    xs = [b for b in b1 if t - 1800 < b.end <= t]
    r = [math.log(b.c / a.c) for a, b in zip(xs, xs[1:]) if a.c > 0 and b.c > 0]
    return statistics.pstdev(r) * 1e4 if len(r) >= 2 else None


def run_pair(data, ctx, sig, beta, counts, event_arm=True, control_arm=True):
    """the event's outcome and its same-geometry control (either arm can be skipped: count reads controls only)."""
    rp = sig["r_pct"]
    p = {"setup": sig["setup"], "sym": ctx.sym, "day": ctx.day, "t": sig["t"], "r_pct": rp,
         "info": {k: v for k, v in sig.items() if k not in ("setup", "t", "entry", "spread", "stop", "R", "r_pct")}}
    if event_arm:
        ev = simulate(data, ctx.sym, ctx.b1, sig["t"], sig["entry"], sig["spread"], sig["stop"],
                      sig["entry"] + TARGET_R * sig["R"], beta, counts, "ev_")
        if ev is None:
            p["ev_drop"] = "no_exit_price"          # an event-arm drop: enters the FAILED-DATA event rate (R16)
            return p
        p["ev"] = ev
        p["ev2R"] = simulate(data, ctx.sym, ctx.b1, sig["t"], sig["entry"], sig["spread"], sig["stop"],
                             sig["entry"] + INFO_R * sig["R"], beta)
        if sig["setup"] == "S7" and sig.get("ind_high", 0) > sig["entry"]:
            p["ev_ind"] = simulate(data, ctx.sym, ctx.b1, sig["t"], sig["entry"], sig["spread"], sig["stop"],
                                   sig["ind_high"], beta)
            p["ind_pct"] = sig["ind_high"] / sig["entry"] - 1.0
        p["ev_vol30"] = realized_vol30(ctx.b1, sig["t"])
    if control_arm:
        c = control_time(sig["setup"], ctx.sym, ctx.day, sig["t"])
        if c is None:
            counts["ctrl_no_legal_bar"] += 1
            p["ctrl_drop"] = "no_legal_bar"
            return p
        q = NH.mid_at(data, ctx.sym, c + ENTRY_LAG, QUOTE_MAX_AGE)
        if not q:
            counts["ctrl_stale_entry"] += 1
            p["ctrl_drop"] = "stale"
            return p
        m, s = q[0], q[1]
        st, tg = m * (1 - rp), m * (1 + TARGET_R * rp)
        p["ctrl_t"] = c
        co = simulate(data, ctx.sym, ctx.b1, c, m, s, st, tg, beta, counts, "ctrl_")
        if co is None:
            p["ctrl_drop"] = "no_exit_price"
            return p
        p["ctrl"] = co
        p["ctrl2R"] = simulate(data, ctx.sym, ctx.b1, c, m, s, st, m * (1 + INFO_R * rp), beta)
        if "ind_pct" in p:
            p["ctrl_ind"] = simulate(data, ctx.sym, ctx.b1, c, m, s, st, m * (1 + p["ind_pct"]), beta)
        p["ctrl_vol30"] = realized_vol30(ctx.b1, c)
    return p


# ------------------------------------------------------------------ name-day driver
def nameday_signals(data, summ, sym, day, counts):
    """(ctx, beta, [signals]) for one name-day, or None (reason counted). OUTCOME-BLIND (entry mids only)."""
    prior10 = summ.prior(day, STRUCT_LOOKBACK)
    prior20 = summ.prior(day, BETA_LOOKBACK)
    if len(prior10) < STRUCT_LOOKBACK:
        counts["short_lookback"] += 1
        return None
    rows = data.minutes(sym, day)
    if not rows:
        counts["no_bars"] += 1
        return None
    if NH.split_in(data, sym, summ.prior(day, STRUCT_LOOKBACK + 1) + [day]):
        counts["split_in_lookback"] += 1
        return None
    prev = prior10[-1]
    pr = data.daily(sym, prev)
    if not pr:
        counts["no_prior_close"] += 1
        return None
    cl = data.close_ts(day)
    b1 = BS.minute_bars(rows, day, cl)
    per = []
    prior_low = None
    for d in prior10:
        rr = data.minutes(sym, d)
        bb = BS.minute_bars(rr, d, data.close_ts(d)) if rr else []
        per.append(BS.aggregate(bb, d, 60, data.close_ts(d)))
        if d == prev and bb:
            prior_low = min(x.l for x in bb)
    series = BS.concat_sessions(per + [BS.aggregate(b1, day, 60, cl)])

    def entry(t):
        q = NH.mid_at(data, sym, t + ENTRY_LAG, QUOTE_MAX_AGE)
        return (q[0], q[1]) if q else None

    ctx = Ctx(sym, day, b1, series, prior10[-5:], prior_low, pr[3], entry)
    beta, _ = NH.beta_of(summ, sym, prior20)
    sigs = []
    for s in SETUPS:
        sig = SCANNERS[s](ctx, counts)
        if sig:
            sig.update(daily_info(data, summ, sym, day, ctx, sig))
            sigs.append(sig)
    return ctx, beta, sigs


def daily_info(data, summ, sym, day, ctx, sig):
    """transcript-5 information cells known at t (raw prices; ATR/SMA adjusted daily, ATR scaled by the D-1 factor)."""
    if sig.get("stale"):
        return {}
    prior = summ.prior(day, NH.DAILY_BACK)
    adj = [x for x in (data.daily(sym, d, True) for d in prior) if x]
    f_prev = data.factor(sym, prior[-1]) if prior else None
    atr = BS.atr14([(x[0], x[1], x[2], x[3]) for x in adj])
    atr_raw = atr / f_prev if (atr and f_prev) else None
    sma50 = BS.sma([x[3] for x in adj], 50)
    t = sig["t"]
    lows = [b.l for b in ctx.b1 if b.end <= t]
    used = ((sig["entry"] - min(lows)) / atr_raw) if (atr_raw and lows) else None
    pdh = None
    prev = prior[-1] if prior else None
    if prev:
        rr = data.minutes(sym, prev)
        if rr:
            bb = BS.minute_bars(rr, prev, data.close_ts(prev))
            pdh = max(b.h for b in bb) if bb else None
    older = [s for s in BS.swings_at(ctx.b60, t) if s.kind == "H" and prev and BS.et_day(s.start) < prev
             and pdh and s.price > pdh]
    return {"atr_used": used,
            "target_beyond_atr": ((TARGET_R * sig["R"]) > (atr_raw - (sig["entry"] - min(lows))))
            if (atr_raw and lows) else None,
            "bias_above_sma50": (adj[-1][3] > sma50) if (sma50 and adj) else None,
            "pdh_dist": ((pdh - sig["entry"]) / sig["entry"]) if pdh else None,
            "older_swing_dist": ((older[-1].price - sig["entry"]) / sig["entry"]) if older else None}


def build(data, tests, uni, counts, event_arm=True, control_arm=True):
    summ = NH.Summaries(data, counts)
    hv = NH.halves_of(tests)
    by_sym = collections.defaultdict(list)
    for d in tests:
        for s in uni.get(d, []):
            by_sym[s].append(d)
    pairs, signals, fails = [], [], 0
    for sym in sorted(by_sym):
        for d in by_sym[sym]:
            counts["namedays"] += 1
            try:
                got = nameday_signals(data, summ, sym, d, counts)
                if not got:
                    continue
                ctx, beta, sigs = got
                for sig in sigs:
                    signals.append({"setup": sig["setup"], "sym": sym, "day": d, "half": hv[d], "t": sig["t"],
                                    "stale": bool(sig.get("stale")), "r_pct": sig.get("r_pct")})
                    if sig.get("stale"):
                        continue
                    if beta is None:
                        counts["beta_pairs_drop"] += 1
                    p = run_pair(data, ctx, sig, beta, counts, event_arm, control_arm)
                    p["half"] = hv[d]
                    p["beta"] = beta
                    pairs.append(p)
            except BRO.FetchFail:
                fails += 1
                counts["nameday_fetch_fail"] += 1
        summ.drop_sym(sym)
    return pairs, signals, fails


# ------------------------------------------------------------------ statistics and verdicts
def series_rows(pairs, setup, key="net", ev="ev", ctrl="ctrl"):
    out = []
    for p in pairs:
        if p["setup"] != setup or not p.get(ev) or not p.get(ctrl):
            continue
        a, b = p[ev].get(key), p[ctrl].get(key)
        if a is None or b is None:
            continue
        out.append({"day": p["day"], "sym": p["sym"], "half": p["half"], "d": a - b, "e": a, "c": b,
                    "hour": BS.et_hour(p["t"]), "chour": BS.et_hour(p["ctrl_t"])})
    return out


def _series_verdict(rows, z):
    pooled = NH.tmean(rows, "d")
    pooled["t_crit"] = NH.t_crit(pooled["df"], z)
    halves = {h: NH.tmean([r for r in rows if r["half"] == h], "d") for h in ("A", "B")}
    for h in halves.values():
        h["mde"] = NH.mde(h, z)
    ev = NH.tmean(rows, "e")
    drops = {}
    for unit, k in (("sym", NH.DROP_NAMES), ("day", NH.DROP_SESSIONS)):
        c = collections.defaultdict(float)
        for r in rows:
            c[r[unit]] += r["d"] / len(rows)
        us = {u for u, _ in sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))[:k]}
        kept = [r for r in rows if r[unit] not in us]
        drops[unit] = {"dropped": sorted(map(str, us)), "diff": NH.tmean(kept, "d")["coef"],
                       "event_net": NH.tmean(kept, "e")["coef"]}
    why, ok = [], True
    c, t, tc = pooled["coef"], pooled["t"], pooled["t_crit"]
    if c is None or c < PASS_BP:
        ok = False
        why.append("difference < +5 bp")
    if t is None or tc is None or t < tc:
        ok = False
        why.append("t < t_crit")
    if any(h["coef"] is None or h["coef"] <= 0 for h in halves.values()):
        ok = False
        why.append("not positive in both halves")
    if ev["coef"] is None or ev["coef"] <= 0:
        ok = False
        why.append("event net <= 0")
    for unit, d in drops.items():
        if d["diff"] is None or d["diff"] < 0 or d["event_net"] is None or d["event_net"] <= 0:
            ok = False
            why.append(f"drop-top-{unit} test")
    return {"pooled": pooled, "halves": halves, "event": ev, "drops": drops, "ok": ok, "why": why}


def setup_verdict(pairs, setup, z=Z_SP):
    """pass clause on BOTH the raw and the hedged series, plus the required stop-slippage cell."""
    out = {}
    for name, key in (("raw", "net"), ("hedged", "hedged")):
        out[name] = _series_verdict(series_rows(pairs, setup, key), z)
        out[name + "_slip"] = NH.tmean(series_rows(pairs, setup, key + "_slip"), "d")
    why = []
    if any(out[s]["pooled"]["t"] is not None and out[s]["pooled"]["t"] <= FAIL_T for s in ("raw", "hedged")):
        v = "FAIL"
        why.append("two-way t <= -2 on a series")
    else:
        slip_ok = all(out[s + "_slip"]["coef"] is not None and out[s + "_slip"]["coef"] >= 0
                      for s in ("raw", "hedged"))
        ok = out["raw"]["ok"] and out["hedged"]["ok"] and slip_ok
        why = [f"raw: {w}" for w in out["raw"]["why"]] + [f"hedged: {w}" for w in out["hedged"]["why"]]
        if not slip_ok:
            why.append("stop-slippage cell < 0")
        powered = all(h["mde"] is not None and h["mde"] <= UNDER_BP
                      for s in ("raw", "hedged") for h in out[s]["halves"].values())
        out["powered"] = powered
        if ok:
            v = "PASS"
            why = ["historical PASS: reruns on >= 60 earlier sessions AND on IEX bars required"]
        elif powered:
            v = "FAIL"
            why.append("powered (MDE <= 15 bp in both halves) and not passed")
        else:
            v = "UNDERPOWERED"
    out["verdict"], out["why"] = v, why
    return out


def drop_rates(pairs, signals, setup=None):
    sg = [s for s in signals if setup is None or s["setup"] == setup]
    pp = [p for p in pairs if setup is None or p["setup"] == setup]
    ev = ((sum(1 for s in sg if s["stale"]) + sum(1 for p in pp if p.get("ev_drop"))) / len(sg)) if sg else None
    ct = (sum(1 for p in pp if p.get("ctrl_drop")) / len(pp)) if pp else None
    return {"event": ev, "control": ct,
            "failed_data": ev is not None and ct is not None and abs(ev - ct) > NH.DROP_RATE_PP}


def projection(pairs, signals):
    """OUTCOME-BLIND count step: event counts/names per setup per half; projected SE = sqrt(2) x SE_ctrl (raw)."""
    out, extend = {}, False
    for s in SETUPS:
        o = {}
        for h in ("A", "B"):
            sg = [x for x in signals if x["setup"] == s and not x["stale"] and x["half"] == h]
            ctrl = [{"day": p["day"], "sym": p["sym"], "y": p["ctrl"]["net"]} for p in pairs
                    if p["setup"] == s and p["half"] == h and p.get("ctrl")]
            cm = NH.tmean(ctrl, "y")
            se = math.sqrt(2) * cm["se"] if cm["se"] is not None else None
            c = NH.t_crit(cm["df"], Z_SP)
            m = (c + NH.POWER_K) * se if (se is not None and c is not None) else None
            o[h] = {"events": len(sg), "names": len({x["sym"] for x in sg}), "n_ctrl": cm["n"],
                    "se_ctrl": cm["se"], "projected_se": se, "projected_mde": m}
        mdes = [o[h]["projected_mde"] for h in ("A", "B")]
        o["projected_mde"] = None if any(x is None for x in mdes) else max(mdes)
        if o["projected_mde"] is None or o["projected_mde"] > UNDER_BP:
            extend = True
        out[s] = o
    out["extend_to_2025_11_03"] = extend
    return out


def info_cells(pairs):
    out = {}
    for s in SETUPS:
        ps = [p for p in pairs if p["setup"] == s and p.get("ev") and p.get("ctrl")]
        o = {"n_pairs": len(ps)}
        o["target_2R"] = NH.tmean(series_rows(pairs, s, "net", "ev2R", "ctrl2R"), "d")
        o["exit_shares"] = {arm: dict(collections.Counter(p[arm]["exit"] for p in ps)) for arm in ("ev", "ctrl")}
        rs = sorted(p["r_pct"] for p in ps)
        o["R_pct_quartiles"] = [rs[int(q * (len(rs) - 1))] for q in (0, .25, .5, .75, 1)] if rs else None
        o["median_hold_min"] = {arm: (statistics.median(p[arm]["hold_min"] for p in ps) if ps else None)
                                for arm in ("ev", "ctrl")}
        o["median_entry_hm"] = {"ev": statistics.median(BS.et_hm(p["t"]) for p in ps) if ps else None,
                                "ctrl": statistics.median(BS.et_hm(p["ctrl_t"]) for p in ps) if ps else None}
        vv = {arm: [p[f"{arm}_vol30"] for p in ps if p.get(f"{arm}_vol30") is not None] for arm in ("ev", "ctrl")}
        o["median_vol30_bp"] = {k: (statistics.median(v) if v else None) for k, v in vv.items()}
        rows = series_rows(pairs, s, "net")
        if rows:
            ph = collections.Counter(r["hour"] for r in rows)
            ch = collections.defaultdict(list)
            for r in rows:
                ch[r["chour"]].append(r["c"])
            w = {h: n for h, n in ph.items() if ch.get(h)}
            tot = sum(w.values())
            rw = (sum(n * statistics.mean(ch[h]) for h, n in w.items()) / tot) if tot else None
            ev_m = statistics.mean(r["e"] for r in rows)
            plain = statistics.mean(r["d"] for r in rows)
            o["entry_hour_reweighted_diff"] = {"diff": (ev_m - rw) if rw is not None else None, "plain": plain,
                                               "sign_flip": rw is not None and (ev_m - rw) * plain < 0}

        def split(fn):
            by = collections.defaultdict(list)
            for p in ps:
                k = fn(p)
                if k is not None:
                    by[str(k)].append(p)
            return {k: NH.tmean(series_rows(v, s, "net"), "d") for k, v in sorted(by.items())}
        inf = lambda p, k: p["info"].get(k)  # noqa: E731
        o["daily_bias_sma50"] = split(lambda p: None if inf(p, "bias_above_sma50") is None else
                                      ("above(with-trend)" if inf(p, "bias_above_sma50") else "below(contrarian)"))
        o["range_budget"] = split(lambda p: None if inf(p, "atr_used") is None else
                                  ("<50%" if inf(p, "atr_used") < 0.5 else
                                   ("50-100%" if inf(p, "atr_used") <= 1 else ">100%")))
        o["target_beyond_atr"] = split(lambda p: inf(p, "target_beyond_atr"))
        o["within_0.3pct_under_pdh"] = split(lambda p: None if inf(p, "pdh_dist") is None else
                                             (0 <= inf(p, "pdh_dist") <= 0.003))
        if s == "S2":
            o["touch_depth"] = split(lambda p: inf(p, "depth"))
            o["n_rejection"] = split(lambda p: None if inf(p, "n_rej") is None else min(inf(p, "n_rej"), 4))
        if s == "S3":
            o["up60"] = split(lambda p: inf(p, "up60"))
        if s == "S5":
            o["pin_vs_double_bottom"] = split(lambda p: inf(p, "kind"))
        if s == "S4":
            med = statistics.median(inf(p, "pole_pct") for p in ps) if ps else None
            o["pole_size_vs_median"] = split(lambda p: None if med is None else
                                             ("big" if inf(p, "pole_pct") > med else "small"))
        if s == "S7":
            o["indication_high_target"] = NH.tmean(series_rows(pairs, s, "net", "ev_ind", "ctrl_ind"), "d")
        out[s] = o
    return out


# ------------------------------------------------------------------ pipeline
def run(mode, extend=False, work=WORK, mkt=None):
    os.makedirs(work, exist_ok=True)
    counts = collections.Counter()
    if mode == "fetch":
        BRO.market_hours_guard()
    mkt = mkt or NH.StudyMarket(offline=(mode != "fetch"))
    lo = EXT_LO if extend else DATA_LO
    # beta needs 20 prior sessions: load from 10 sessions earlier than the structure look-back start
    data, tests, uni = NH.load_inputs(mkt, lo, TEST_HI, STRUCT_LOOKBACK, counts)
    if mode == "fetch":
        NH.fetch_minutes(mkt, data, tests, uni, BETA_LOOKBACK)
        pairs, signals, fails = build(data, tests, uni, counts)
        mkt.flush()
        P_(f"fetch done: {mkt.requests} requests, {len(mkt.fails)} failures; counts {dict(counts)}")
        return None
    if mode == "count":
        pairs, signals, fails = build(data, tests, uni, counts, event_arm=False, control_arm=True)
        _abort(counts, fails)
        sha = hashlib.sha256(json.dumps(signals, sort_keys=True).encode()).hexdigest()
        res = {"extend": extend, "signals_sha256": sha, "counts": dict(counts), "projection":
               projection(pairs, signals)}
        BRO.jsave(os.path.join(work, f"count{'_ext' if extend else ''}.json"), res)
        P_(json.dumps(res, indent=1, default=str))
        return res
    if mode == "score":
        cnt = BRO.jload(os.path.join(work, f"count{'_ext' if extend else ''}.json"))
        if not cnt:
            raise SystemExit("run count first (outcome-blind power step precedes scoring)")
        if cnt["projection"]["extend_to_2025_11_03"] and not extend:
            raise SystemExit("count step projected MDE > 15 bp: extend the primary sample (--extend) before scoring")
        pairs, signals, fails = build(data, tests, uni, counts)
        _abort(counts, fails)
        sha = hashlib.sha256(json.dumps(signals, sort_keys=True).encode()).hexdigest()
        if sha != cnt["signals_sha256"]:
            raise SystemExit("signals differ from the count step: re-run count")
        res = summarize(pairs, signals, counts, cnt)
        BRO.jsave(os.path.join(work, "result.json"), res)
        P_(f"result.json written ({len(pairs)} pairs)")
        return res
    raise SystemExit(__doc__)


def _abort(counts, fails):
    nd = counts["namedays"]
    if nd and fails / nd > NH.FAIL_SHARE:
        raise SystemExit(f"ABORT: {fails}/{nd} name-days failed (> 2%)")


def summarize(pairs, signals, counts, cnt):
    dr = drop_rates(pairs, signals)
    return {"prereg": PREREG, "script_rev": BRO.script_rev(), "resolutions": RESOLUTIONS, "count": cnt,
            "counts": dict(counts), "drop_rates": {"pooled": dr, **{s: drop_rates(pairs, signals, s) for s in SETUPS}},
            "failed_data": dr["failed_data"],
            "setups": {s: setup_verdict(pairs, s) for s in SETUPS}, "info": info_cells(pairs)}


def _f(x, nd=1):
    return "n/a" if x is None else (f"{x:.{nd}f}" if isinstance(x, (int, float)) else str(x))


def render_report(res) -> str:
    L = ["# Video setups vs random entries - result", "", f"prereg {res['prereg']}; script {res['script_rev']}", ""]
    if res["failed_data"]:
        L += ["**FAILED-DATA: the arms' drop rates differ by > 2 pp - do not read.**", ""]
    L += ["## Power (count step, outcome-blind)", "", "| setup | half | events | names | projected MDE bp |",
          "|---|---|---|---|---|"]
    pj = res["count"]["projection"]
    for s in SETUPS:
        for h in ("A", "B"):
            x = pj[s][h]
            L.append(f"| {s} | {h} | {x['events']} | {x['names']} | {_f(x['projected_mde'])} |")
    L += ["", "## Verdicts", ""]
    for s in SETUPS:
        v = res["setups"][s]
        L.append(f"- **{s}: {v['verdict']}** - raw diff {_f(v['raw']['pooled']['coef'])} bp (t "
                 f"{_f(v['raw']['pooled']['t'], 2)}), hedged {_f(v['hedged']['pooled']['coef'])} bp (t "
                 f"{_f(v['hedged']['pooled']['t'], 2)}); {'; '.join(v['why'])}")
    L += ["", "Information cells are in result.json; none can be promoted.", ""]
    return "\n".join(L)


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    extend = "--extend" in a
    a = [x for x in a if x != "--extend"]
    if not a or a[0] not in ("fetch", "count", "score", "report"):
        raise SystemExit(__doc__)
    if a[0] == "report":
        res = BRO.jload(os.path.join(WORK, "result.json"))
        if res is None:
            raise SystemExit("no result.json")
        open(os.path.join(WORK, "report.md"), "w").write(render_report(res))
        P_(f"report.md written to {WORK}")
        return
    run(a[0], extend)


if __name__ == "__main__":
    main()
