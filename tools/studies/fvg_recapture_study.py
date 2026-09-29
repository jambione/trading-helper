#!/usr/bin/env python3
"""Bearish FVG recapture: do down-displacement gaps get filled, and does trading
the reclaim pay after costs?

Question: on volatile SIP 1m (and 5m) paths, a three-candle bearish fair-value
gap (down FVG) is often said to grant a "right to recapture." This measures
touch / full-fill / fail rates and two long entries (immediate fade vs wait
for reclaim) after round-trip costs.

OFFLINE by default: reads ai_reports/runway_bars_cache.pkl
  key (sym, day) -> (t, o, h, l, c, v) 1m RTH SIP bars.
Does NOT call Alpaca unless --fetch is passed (forbidden while the live desk
is open).

Bearish FVG at bar i (candles i-2, i-1, i), ICT three-candle form:
  low[i-2] > high[i]
  gap_top    = low[i-2]
  gap_bottom = high[i]
  gap_bp     = (gap_top - gap_bottom) / mid * 1e4
Optional: middle-bar body >= min_disp_atr * ATR(14).

Outcomes from the bar AFTER the FVG print (no look-ahead into bar i):
  touch      any later high >= gap_bottom
  full_fill  any later high >= gap_top
  fail       any later low  <= impulse_low - fail_atr * ATR
             before a touch (impulse_low = min low of the 3 candles)
  time_to_*  minutes from FVG bar timestamp

Trades (long only; costs = short_fade price-tier RT %):
  immediate  enter at open of bar i+1; stop = impulse_low - stop_buf_atr*ATR;
             target = gap_top; time stop = max_hold_m
  reclaim    after FVG, first bar close >= gap_bottom; enter next open;
             same stop/target/time stop
  random     control: same hold length as the paired reclaim trade, entry at a
             random later open on the same symbol-day (seeded)

Train days <= TRAIN_END; test >= TEST_START (within available cache days).
Pass bar (desk-style): both halves same sign on mean net %, and beat random
on the same name-days.

Usage (repo root; MacBook OK if cache is present)::

    .venv/bin/python tools/studies/fvg_recapture_study.py
    .venv/bin/python tools/studies/fvg_recapture_study.py --symbols FSLY,ASST,TEM,HOOD,IREN
    .venv/bin/python tools/studies/fvg_recapture_study.py --tf 1,5 --min-gap-bp 10,25,50 --json /tmp/fvg.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

ET = ZoneInfo("America/New_York")
CACHE = os.path.join(ROOT, "ai_reports", "runway_bars_cache.pkl")
TRAIN_END = "2026-09-17"
TEST_START = "2026-09-18"
ATR_LEN = 14
DEFAULT_SYMBOLS = ("FSLY", "ASST", "TEM", "HOOD", "IREN", "LUNR", "PURR", "HUT")


# ---------------------------------------------------------------- helpers
def cost_rt_pct(px: float) -> float:
    """Round-trip cost % by price tier (same as short_fade_study)."""
    if px >= 10:
        return 0.20
    if px >= 5:
        return 0.40
    return 1.00


def rth_mins(ts: float) -> int:
    dt = datetime.fromtimestamp(float(ts), ET)
    return dt.hour * 60 + dt.minute


def in_session(ts: float, start_m: int = 9 * 60 + 35, end_m: int = 15 * 60 + 55) -> bool:
    m = rth_mins(ts)
    return start_m <= m <= end_m


def atr_wilder(h, l, c, length=ATR_LEN):
    """Causal Wilder ATR; None until warm."""
    n = len(c)
    out = [None] * n
    if n < length + 1:
        return out
    trs = [0.0] * n
    for i in range(1, n):
        trs[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    s = sum(trs[1:length + 1])
    out[length] = s / length
    for i in range(length + 1, n):
        prev = out[i - 1]
        out[i] = (prev * (length - 1) + trs[i]) / length
    return out


def aggregate_tf(B, tf_m: int):
    """Clock-aligned OHLC from 1m bars. tf_m=1 returns B unchanged (no vol)."""
    t, o, h, l, c, v = B
    if tf_m <= 1:
        return t, o, h, l, c, v
    sec = tf_m * 60
    out_t, out_o, out_h, out_l, out_c, out_v = [], [], [], [], [], []
    cur = None
    for i in range(len(t)):
        bucket = int(t[i] // sec) * sec
        if bucket != cur:
            out_t.append(t[i])
            out_o.append(o[i])
            out_h.append(h[i])
            out_l.append(l[i])
            out_c.append(c[i])
            out_v.append(v[i])
            cur = bucket
        else:
            out_h[-1] = max(out_h[-1], h[i])
            out_l[-1] = min(out_l[-1], l[i])
            out_c[-1] = c[i]
            out_v[-1] += v[i]
            out_t[-1] = t[i]  # stamp = last 1m in bucket (completed)
    return out_t, out_o, out_h, out_l, out_c, out_v


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100.0
    f = int(math.floor(k))
    c = int(math.ceil(k))
    if f == c:
        return xs[f]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


# ---------------------------------------------------------------- detect
def find_bearish_fvgs(B, min_gap_bp: float, min_disp_atr: float, atr_len: int = ATR_LEN):
    """Yield dicts for each bearish FVG at index i (>=2)."""
    t, o, h, l, c, v = B
    atr = atr_wilder(h, l, c, atr_len)
    out = []
    for i in range(2, len(c)):
        # Bearish FVG: candle-1 low above candle-3 high (down displacement left a pocket).
        if l[i - 2] <= h[i]:
            continue
        gap_top = l[i - 2]
        gap_bottom = h[i]
        if gap_top <= gap_bottom:
            continue
        mid = (gap_top + gap_bottom) / 2.0
        if mid <= 0:
            continue
        gap_bp = (gap_top - gap_bottom) / mid * 1e4
        if gap_bp < min_gap_bp:
            continue
        a = atr[i]
        if a is None or a <= 0:
            continue
        # middle candle displacement (body of i-1)
        body = abs(c[i - 1] - o[i - 1])
        if min_disp_atr > 0 and body < min_disp_atr * a:
            continue
        impulse_low = min(l[i - 2], l[i - 1], l[i])
        impulse_high = max(h[i - 2], h[i - 1], h[i])
        out.append({
            "i": i,
            "ts": t[i],
            "gap_top": gap_top,
            "gap_bottom": gap_bottom,
            "gap_bp": gap_bp,
            "atr": a,
            "impulse_low": impulse_low,
            "impulse_high": impulse_high,
            "mid_body_atr": body / a,
            "close": c[i],
            "open_next": o[i + 1] if i + 1 < len(o) else None,
        })
    return out


def path_outcomes(B, fvg, fail_atr: float, max_hold_m: int, tf_m: int):
    """Touch / fill / fail from bars after the FVG bar."""
    t, o, h, l, c, v = B
    i0 = fvg["i"]
    if i0 + 1 >= len(t):
        return None
    gap_top = fvg["gap_top"]
    gap_bottom = fvg["gap_bottom"]
    impulse_low = fvg["impulse_low"]
    atr = fvg["atr"]
    fail_px = impulse_low - fail_atr * atr
    t0 = t[i0]
    # max index by time
    t_end = t0 + max_hold_m * 60
    touch_m = fill_m = fail_m = None
    touch_i = fill_i = fail_i = None
    for j in range(i0 + 1, len(t)):
        if t[j] > t_end:
            break
        if fail_m is None and l[j] <= fail_px:
            fail_m = (t[j] - t0) / 60.0
            fail_i = j
        if touch_m is None and h[j] >= gap_bottom:
            touch_m = (t[j] - t0) / 60.0
            touch_i = j
        if fill_m is None and h[j] >= gap_top:
            fill_m = (t[j] - t0) / 60.0
            fill_i = j
        # once failed before touch, stop classifying further path for fail-first
        if fail_m is not None and touch_m is None:
            break
        if fill_m is not None:
            break
    failed_before_touch = fail_m is not None and (touch_m is None or fail_m < touch_m)
    return {
        "touch": touch_m is not None,
        "full_fill": fill_m is not None,
        "fail_before_touch": failed_before_touch,
        "time_to_touch_m": touch_m,
        "time_to_fill_m": fill_m,
        "time_to_fail_m": fail_m if failed_before_touch else None,
        "touch_i": touch_i,
        "fill_i": fill_i,
        "fail_i": fail_i if failed_before_touch else None,
        "max_hold_m": max_hold_m,
        "tf_m": tf_m,
    }


def simulate_trade(B, entry_i, entry_px, stop_px, target_px, max_hold_m, side="long"):
    """Walk bars after entry_i; entry assumed filled at entry_px at open of entry_i.
    Returns exit dict. Bar entry_i is the entry bar (use its H/L for stops/targets).
    """
    t, o, h, l, c, v = B
    if entry_i is None or entry_i >= len(t) or entry_px is None or entry_px <= 0:
        return None
    t0 = t[entry_i]
    t_end = t0 + max_hold_m * 60
    # same-bar: conservative — stop before target if both touch
    for j in range(entry_i, len(t)):
        if t[j] > t_end:
            # time stop at prior close if possible
            k = j - 1 if j > entry_i else entry_i
            exit_px = c[k]
            return {
                "exit_i": k, "exit_px": exit_px, "reason": "time",
                "hold_m": (t[k] - t0) / 60.0,
                "gross_pct": (exit_px / entry_px - 1.0) * 100.0,
            }
        if side == "long":
            hit_stop = l[j] <= stop_px
            hit_tgt = h[j] >= target_px
            if hit_stop and hit_tgt:
                # adverse first inside the bar
                exit_px = stop_px
                return {
                    "exit_i": j, "exit_px": exit_px, "reason": "stop_same_bar",
                    "hold_m": (t[j] - t0) / 60.0,
                    "gross_pct": (exit_px / entry_px - 1.0) * 100.0,
                }
            if hit_stop:
                return {
                    "exit_i": j, "exit_px": stop_px, "reason": "stop",
                    "hold_m": (t[j] - t0) / 60.0,
                    "gross_pct": (stop_px / entry_px - 1.0) * 100.0,
                }
            if hit_tgt:
                return {
                    "exit_i": j, "exit_px": target_px, "reason": "target",
                    "hold_m": (t[j] - t0) / 60.0,
                    "gross_pct": (target_px / entry_px - 1.0) * 100.0,
                }
    exit_px = c[-1]
    return {
        "exit_i": len(c) - 1, "exit_px": exit_px, "reason": "eod",
        "hold_m": (t[-1] - t0) / 60.0,
        "gross_pct": (exit_px / entry_px - 1.0) * 100.0,
    }


def find_reclaim_entry(B, fvg, max_hold_m: int):
    """First bar after FVG whose close is back at/above gap_bottom; enter next open."""
    t, o, h, l, c, v = B
    i0 = fvg["i"]
    gap_bottom = fvg["gap_bottom"]
    t_end = t[i0] + max_hold_m * 60
    for j in range(i0 + 1, len(c) - 1):
        if t[j] > t_end:
            break
        if c[j] >= gap_bottom:
            return j + 1, o[j + 1]
    return None, None


# ---------------------------------------------------------------- build
def build_events(cache, symbols, days_lo, days_hi, tfs, min_gaps, min_disp_atr,
                 fail_atr, stop_buf_atr, max_hold_m, session_start, session_end,
                 min_px, max_px, seed=7):
    rng = random.Random(seed)
    events = []
    skipped = Counter()
    want = {s.upper() for s in symbols} if symbols else None

    for (sym, day), B0 in cache.items():
        if B0 is None or not isinstance(B0, tuple) or len(B0) != 6:
            continue
        if want is not None and sym.upper() not in want:
            continue
        if day < days_lo or day > days_hi:
            continue
        for tf_m in tfs:
            B = aggregate_tf(B0, tf_m)
            t, o, h, l, c, v = B
            if len(c) < ATR_LEN + 5:
                skipped["short_series"] += 1
                continue
            for min_gap_bp in min_gaps:
                fvgs = find_bearish_fvgs(B, min_gap_bp, min_disp_atr)
                for fvg in fvgs:
                    if not in_session(fvg["ts"], session_start, session_end):
                        skipped["outside_session"] += 1
                        continue
                    px = fvg["close"]
                    if px is None or not (min_px <= px <= max_px):
                        skipped["price_band"] += 1
                        continue
                    if fvg["open_next"] is None:
                        skipped["no_next_bar"] += 1
                        continue
                    oc = path_outcomes(B, fvg, fail_atr, max_hold_m, tf_m)
                    if oc is None:
                        skipped["no_path"] += 1
                        continue

                    stop_px = fvg["impulse_low"] - stop_buf_atr * fvg["atr"]
                    # --- immediate
                    ei = fvg["i"] + 1
                    ep = o[ei]
                    imm = simulate_trade(B, ei, ep, stop_px, fvg["gap_top"], max_hold_m)
                    # --- reclaim
                    ri, rp = find_reclaim_entry(B, fvg, max_hold_m)
                    rec = None
                    if ri is not None and rp is not None and rp > 0:
                        # stop: min(impulse_low, lows from FVG to entry) - buf
                        swing = min(l[fvg["i"]:ri + 1])
                        stop_r = min(fvg["impulse_low"], swing) - stop_buf_atr * fvg["atr"]
                        rec = simulate_trade(B, ri, rp, stop_r, fvg["gap_top"], max_hold_m)

                    # --- random control matched to reclaim hold when possible
                    rnd = None
                    hold_for_rand = (rec["hold_m"] if rec else max_hold_m)
                    candidates = [k for k in range(fvg["i"] + 1, len(o) - 1)
                                  if in_session(t[k], session_start, session_end)]
                    if candidates:
                        rk = rng.choice(candidates)
                        rnd = simulate_trade(
                            B, rk, o[rk],
                            o[rk] * (1 - 0.01),  # loose 1% stop so random is mostly time/path
                            o[rk] * (1 + 0.02),
                            max(1, int(hold_for_rand)),
                        )

                    cst = cost_rt_pct(ep)

                    def pack(tr, style, entry_i, entry_px, stop):
                        if tr is None:
                            return None
                        gross = tr["gross_pct"]
                        net = gross - cst
                        return {
                            "style": style,
                            "entry_i": entry_i,
                            "entry_px": entry_px,
                            "stop_px": stop,
                            "exit_reason": tr["reason"],
                            "hold_m": tr["hold_m"],
                            "gross_pct": gross,
                            "cost_pct": cst,
                            "net_pct": net,
                        }

                    events.append({
                        "symbol": sym.upper(),
                        "day": day,
                        "tf_m": tf_m,
                        "min_gap_bp": min_gap_bp,
                        "ts": fvg["ts"],
                        "tod_mins": rth_mins(fvg["ts"]),
                        "gap_bp": fvg["gap_bp"],
                        "mid_body_atr": fvg["mid_body_atr"],
                        "price": px,
                        "day_chg_at_fvg": (px / o[0] - 1) * 100 if o[0] else None,
                        **{k: oc[k] for k in (
                            "touch", "full_fill", "fail_before_touch",
                            "time_to_touch_m", "time_to_fill_m", "time_to_fail_m",
                        )},
                        "trade_immediate": pack(imm, "immediate", ei, ep, stop_px),
                        "trade_reclaim": pack(
                            rec, "reclaim", ri, rp,
                            (min(fvg["impulse_low"], min(l[fvg["i"]:ri + 1])) - stop_buf_atr * fvg["atr"])
                            if ri is not None else None,
                        ),
                        "trade_random": pack(rnd, "random", None, None, None) if rnd else None,
                    })
    return events, skipped


# ---------------------------------------------------------------- report
def half_of(day, train_end=TRAIN_END, test_start=TEST_START):
    if day <= train_end:
        return "train"
    if day >= test_start:
        return "test"
    return "mid"


def summarize_outcomes(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0}
    touches = [r for r in rows if r["touch"]]
    fills = [r for r in rows if r["full_fill"]]
    fails = [r for r in rows if r["fail_before_touch"]]
    return {
        "n": n,
        "touch_rate": len(touches) / n,
        "fill_rate": len(fills) / n,
        "fail_rate": len(fails) / n,
        "med_time_to_touch_m": median([r["time_to_touch_m"] for r in touches]),
        "med_time_to_fill_m": median([r["time_to_fill_m"] for r in fills]),
        "p90_time_to_fill_m": pct([r["time_to_fill_m"] for r in fills], 90),
        "med_gap_bp": median([r["gap_bp"] for r in rows]),
    }


def summarize_trades(rows, key):
    trades = [r[key] for r in rows if r.get(key)]
    n = len(trades)
    if n == 0:
        return {"n": 0}
    nets = [t["net_pct"] for t in trades]
    gross = [t["gross_pct"] for t in trades]
    wins = sum(1 for x in nets if x > 0)
    reasons = Counter(t["exit_reason"] for t in trades)
    return {
        "n": n,
        "mean_net_pct": mean(nets),
        "med_net_pct": median(nets),
        "mean_gross_pct": mean(gross),
        "win_rate": wins / n,
        "med_hold_m": median([t["hold_m"] for t in trades]),
        "exit_reasons": dict(reasons),
        "p10_net": pct(nets, 10),
        "p90_net": pct(nets, 90),
    }


def cell_report(events, tf_m, min_gap_bp):
    rows = [e for e in events if e["tf_m"] == tf_m and e["min_gap_bp"] == min_gap_bp]
    by = defaultdict(list)
    for r in rows:
        by[half_of(r["day"])].append(r)
    out = {"tf_m": tf_m, "min_gap_bp": min_gap_bp, "all": {}, "train": {}, "test": {}}
    for half in ("all", "train", "test"):
        subset = rows if half == "all" else by.get(half, [])
        out[half] = {
            "outcomes": summarize_outcomes(subset),
            "immediate": summarize_trades(subset, "trade_immediate"),
            "reclaim": summarize_trades(subset, "trade_reclaim"),
            "random": summarize_trades(subset, "trade_random"),
        }
    # pass-style flags
    def signed(h, style):
        m = out[h][style].get("mean_net_pct")
        return m

    imm_ok = (
        out["train"]["immediate"].get("n", 0) >= 10
        and out["test"]["immediate"].get("n", 0) >= 10
        and signed("train", "immediate") is not None
        and signed("test", "immediate") is not None
        and signed("train", "immediate") > 0
        and signed("test", "immediate") > 0
    )
    rec_ok = (
        out["train"]["reclaim"].get("n", 0) >= 10
        and out["test"]["reclaim"].get("n", 0) >= 10
        and signed("train", "reclaim") is not None
        and signed("test", "reclaim") is not None
        and signed("train", "reclaim") > 0
        and signed("test", "reclaim") > 0
    )
    # beat random on test mean net
    def beats_random(style):
        a = out["test"][style].get("mean_net_pct")
        b = out["test"]["random"].get("mean_net_pct")
        return a is not None and b is not None and a > b

    out["pass_immediate"] = bool(imm_ok and beats_random("immediate"))
    out["pass_reclaim"] = bool(rec_ok and beats_random("reclaim"))
    return out


def _m(x):
    return f"{x:.1f}" if x is not None else "—"


def fmt_out(o):
    if not o or o.get("n", 0) == 0:
        return "n=0"
    return (f"n={o['n']}  touch={o['touch_rate']*100:5.1f}%  fill={o['fill_rate']*100:5.1f}%  "
            f"fail={o['fail_rate']*100:5.1f}%  med_touch={_m(o['med_time_to_touch_m'])}m  "
            f"med_fill={_m(o['med_time_to_fill_m'])}m  p90_fill={_m(o['p90_time_to_fill_m'])}m  "
            f"med_gap={o['med_gap_bp']:.0f}bp")


def fmt_tr(t):
    if not t or t.get("n", 0) == 0:
        return "n=0"
    return (f"n={t['n']}  mean_net={t['mean_net_pct']:+.3f}%  med_net={t['med_net_pct']:+.3f}%  "
            f"gross={t['mean_gross_pct']:+.3f}%  win={t['win_rate']*100:4.1f}%  "
            f"med_hold={t['med_hold_m']:.0f}m  exits={t['exit_reasons']}")


def print_report(events, skipped, tfs, min_gaps, args):
    days = sorted({e["day"] for e in events})
    syms = sorted({e["symbol"] for e in events})
    print("# FVG recapture study")
    print(f"symbols ({len(syms)}): {', '.join(syms)}")
    print(f"days ({len(days)}): {days[0] if days else '?'} .. {days[-1] if days else '?'}  "
          f"train<={TRAIN_END}  test>={TEST_START}")
    print(f"events={len(events)}  skipped={dict(skipped)}")
    print(f"params: fail_atr={args.fail_atr} stop_buf_atr={args.stop_buf_atr} "
          f"max_hold_m={args.max_hold_m} min_disp_atr={args.min_disp_atr} "
          f"session={args.session}")
    print()
    cells = []
    for tf_m in tfs:
        for gap in min_gaps:
            cell = cell_report(events, tf_m, gap)
            cells.append(cell)
            print(f"## tf={tf_m}m  min_gap>={gap}bp  "
                  f"pass_immediate={cell['pass_immediate']}  pass_reclaim={cell['pass_reclaim']}")
            for half in ("all", "train", "test"):
                print(f"  [{half}] outcomes  {fmt_out(cell[half]['outcomes'])}")
                print(f"  [{half}] immediate {fmt_tr(cell[half]['immediate'])}")
                print(f"  [{half}] reclaim   {fmt_tr(cell[half]['reclaim'])}")
                print(f"  [{half}] random    {fmt_tr(cell[half]['random'])}")
            print()

    # per-symbol fill rates on the primary cell (1m, smallest gap)
    if tfs and min_gaps:
        tf0, g0 = tfs[0], min_gaps[0]
        print(f"## per-symbol outcomes  tf={tf0}m min_gap>={g0}bp (all days)")
        by_sym = defaultdict(list)
        for e in events:
            if e["tf_m"] == tf0 and e["min_gap_bp"] == g0:
                by_sym[e["symbol"]].append(e)
        for sym in sorted(by_sym, key=lambda s: -len(by_sym[s])):
            print(f"  {sym:6s}  {fmt_out(summarize_outcomes(by_sym[sym]))}")
            print(f"         reclaim  {fmt_tr(summarize_trades(by_sym[sym], 'trade_reclaim'))}")
        print()

    # morning vs rest (primary cell)
    if tfs and min_gaps:
        tf0, g0 = tfs[0], min_gaps[0]
        print(f"## session slice  tf={tf0}m min_gap>={g0}bp")
        for label, pred in (
            ("09:35-11:00", lambda e: e["tod_mins"] < 11 * 60),
            ("11:00-15:55", lambda e: e["tod_mins"] >= 11 * 60),
            ("day_chg<=0 (down day so far)", lambda e: (e.get("day_chg_at_fvg") or 0) <= 0),
            ("day_chg>0 (up day so far)", lambda e: (e.get("day_chg_at_fvg") or 0) > 0),
        ):
            sub = [e for e in events if e["tf_m"] == tf0 and e["min_gap_bp"] == g0 and pred(e)]
            print(f"  {label}")
            print(f"    outcomes  {fmt_out(summarize_outcomes(sub))}")
            print(f"    reclaim   {fmt_tr(summarize_trades(sub, 'trade_reclaim'))}")
        print()

    return cells


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                    help="Comma list, or 'ALL' for every symbol in cache")
    ap.add_argument("--from", dest="days_lo", default="2026-09-01")
    ap.add_argument("--to", dest="days_hi", default="2026-09-24")
    ap.add_argument("--tf", default="1,5", help="Comma list of minute timeframes")
    ap.add_argument("--min-gap-bp", default="10,25,50")
    ap.add_argument("--min-disp-atr", type=float, default=0.5,
                    help="Min middle-candle body in ATRs (0=off)")
    ap.add_argument("--fail-atr", type=float, default=0.25)
    ap.add_argument("--stop-buf-atr", type=float, default=0.1)
    ap.add_argument("--max-hold-m", type=int, default=120)
    ap.add_argument("--session", default="0935-1555",
                    help="HHMM-HHMM ET window for FVG prints")
    ap.add_argument("--min-price", type=float, default=5.0)
    ap.add_argument("--max-price", type=float, default=500.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", default="")
    ap.add_argument("--fetch", action="store_true",
                    help="Unused placeholder; study is cache-only for now")
    args = ap.parse_args()

    if not os.path.exists(args.cache):
        print(f"missing cache: {args.cache}", file=sys.stderr)
        sys.exit(1)
    with open(args.cache, "rb") as f:
        cache = pickle.load(f)

    if args.symbols.strip().upper() == "ALL":
        symbols = sorted({s for (s, _) in cache.keys()})
    else:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    tfs = [int(x) for x in args.tf.split(",") if x.strip()]
    min_gaps = [float(x) for x in args.min_gap_bp.split(",") if x.strip()]

    ss, se = args.session.split("-")
    session_start = int(ss[:2]) * 60 + int(ss[2:])
    session_end = int(se[:2]) * 60 + int(se[2:])

    events, skipped = build_events(
        cache, symbols, args.days_lo, args.days_hi, tfs, min_gaps,
        args.min_disp_atr, args.fail_atr, args.stop_buf_atr, args.max_hold_m,
        session_start, session_end, args.min_price, args.max_price, args.seed,
    )
    cells = print_report(events, skipped, tfs, min_gaps, args)

    if args.json:
        payload = {
            "params": vars(args),
            "n_events": len(events),
            "skipped": dict(skipped),
            "cells": cells,
            # keep events light: drop nested None-heavy if huge
            "events": events,
        }
        with open(args.json, "w") as f:
            json.dump(payload, f, default=float, indent=0)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
