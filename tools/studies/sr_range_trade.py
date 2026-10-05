#!/usr/bin/env python3
"""Support -> resistance range trade on LuxAlgo order blocks (operator's "optimal capture", RXO 2026-10-05).
Pre-registration: docs/studies/sr_range_trade_prereg.json (committed before this script is run).

Sample: the admitted name-days of the 2026-10-02 indicator-levels study ($10+, 9/04-10/02; the control rows of
ai_reports/indicator_levels/rows.json), with its 1-minute SIP bars (ext.pkl: premarket + RTH, day and prior day).
Blocks: tools/order_blocks.py, point-in-time, charted last 3 per side, swing 10, wicks, on 1-minute bars.

SIGNAL at the close of 1-min bar i (09:40-15:00 ET), all known at that close:
  support S = a charted support block (unbroken bullish OB, or a bearish breaker) with close[i-1] <= S.top < close[i]
              (the first close up out of the zone); one signal per support block
  room      = nearest charted resistance above close[i] (unbroken bearish OB or bullish breaker): R.btm / close[i] - 1
              >= 0.40%; no resistance above -> no trade (no target)
  %R        = fast Williams %R (21 bars, EMA 7, as the desk): below -20 and rising (wr[i] > wr[i-1])
TRADE: buy at the open of bar i+1. Target = R.btm (filled when a later bar's HIGH is strictly above it, at R.btm).
       Stop = a 1-min CLOSE below S.btm (exit at that close). Time stop: the close 30 min after entry (15:55 cap).
       Target and stop in the same bar -> the stop. Cost 0.20% round trip (the 10/02 $10+ tier).
CONTROL: for each signal, one random minute (seed 29) in the same name-day 09:40-15:00 with a bracket of the SAME %
       distances (target +room%, stop at the same % below entry as S.btm was, close-based) and the same time stop.
Run on the mini after the close: .venv/bin/python tools/studies/sr_range_trade.py
"""
from __future__ import annotations

import collections
import json
import math
import os
import pickle
import random
import statistics
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import order_blocks as OB  # noqa: E402
from order_block_gate import series, fe_diff  # noqa: E402

ET = ZoneInfo("America/New_York")
IND = os.path.join(ROOT, "ai_reports", "indicator_levels")
OUT = os.path.join(ROOT, "ai_reports", "sr_range_trade")
COST = 0.0020
ROOM_MIN = 0.40
HOLD = 30 * 60


def et_min(ts):
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def wr_fast(h, l, c, n=21, ema=7):
    raw = []
    for i in range(len(c)):
        a = max(0, i - n + 1)
        hh, ll = max(h[a:i + 1]), min(l[a:i + 1])
        raw.append(-100.0 * (hh - c[i]) / (hh - ll) if hh > ll else -50.0)
    out, k = [], 2 / (ema + 1)
    for v in raw:
        out.append(v if not out else out[-1] + k * (v - out[-1]))
    return out


def run_bracket(rows, i_entry, entry, target, stop_px, day_end):
    """Long from the open of bar i_entry. Returns gross return or None."""
    t0 = rows[i_entry][0]
    for j in range(i_entry, len(rows)):
        t, o, h, l, c = rows[j]
        if t >= day_end or t - t0 >= HOLD:
            return rows[j - 1][4] / entry - 1 if j > i_entry else None
        hit_stop = c < stop_px
        hit_tgt = h > target
        if hit_stop:                      # stop wins ties (conservative)
            return c / entry - 1
        if hit_tgt:
            return target / entry - 1
    return None


def main():
    os.makedirs(OUT, exist_ok=True)
    R = json.load(open(os.path.join(IND, "rows.json")))
    days = R["days"]
    half = {d: "AB"[k % 2] for k, d in enumerate(days)}
    namedays = sorted({(r["sym"], r["day"]) for r in R["rows"] if r["pop"] == "C"})
    cache = pickle.load(open(os.path.join(IND, "ext.pkl"), "rb"))
    cal = sorted({d for (_, d) in cache})
    prev_of = {d: cal[cal.index(d) - 1] if cal.index(d) else None for d in cal}
    rng = random.Random(29)
    trades, ctrls, drops = [], [], collections.Counter()
    for sym, day in namedays:
        rows = series(cache, sym, day, prev_of.get(day))
        if len(rows) < 100:
            drops["short_series"] += 1
            continue
        h = [r[2] for r in rows]
        l = [r[3] for r in rows]
        c = [r[4] for r in rows]
        wr = wr_fast(h, l, c)
        states = list(OB.order_blocks(rows, bar_sec=60))
        day_idx = [i for i, r in enumerate(rows) if datetime.fromtimestamp(r[0], ET).strftime("%Y-%m-%d") == day]
        if not day_idx:
            continue
        d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        day_end = d0.replace(hour=15, minute=55).timestamp()
        window = [i for i in day_idx if 9 * 60 + 40 <= et_min(rows[i][0] + 60) <= 15 * 60 and i + 1 < len(rows)]
        used = set()
        for i in window:
            tq = rows[i][0] + 60
            ch = [b for b in OB.charted([b for b in states[i][1] if b.known_ts <= tq])]
            sup = [b for b in ch if ((b.kind == "bull" and not b.breaker) or (b.kind == "bear" and b.breaker))
                   and c[i - 1] <= b.top < c[i]]
            if not sup:
                continue
            S = max(sup, key=lambda b: b.top)
            key = (S.kind, round(S.top, 4), round(S.btm, 4), S.origin_ts)
            if key in used:
                continue
            res = [b.btm for b in ch if ((b.kind == "bear" and not b.breaker) or (b.kind == "bull" and b.breaker))
                   and b.btm > c[i]]
            if not res:
                drops["no_resistance_above"] += 1
                continue
            room = (min(res) / c[i] - 1) * 100
            if room < ROOM_MIN:
                drops["room_lt_0.40"] += 1
                continue
            if not (wr[i] < -20 and wr[i] > wr[i - 1]):
                drops["wr_not_rising_below_-20"] += 1
                continue
            used.add(key)
            entry = rows[i + 1][1]
            target = min(res)
            stop_pct = S.btm / entry - 1
            g = run_bracket(rows, i + 1, entry, target, S.btm, day_end)
            if g is None:
                drops["no_outcome"] += 1
                continue
            trades.append({"sym": sym, "day": day, "t": tq, "room": room, "stop_pct": stop_pct * 100, "gross": g,
                           "net": g - COST})
            # control: random minute, same name-day, same % bracket
            k = rng.choice(window)
            ce = rows[k + 1][1]
            cg = run_bracket(rows, k + 1, ce, ce * (1 + room / 100), ce * (1 + stop_pct), day_end)
            if cg is not None:
                ctrls.append({"sym": sym, "day": day, "gross": cg, "net": cg - COST, "pair": len(trades) - 1})
    json.dump({"trades": trades, "controls": ctrls, "drops": dict(drops)}, open(os.path.join(OUT, "rows.json"), "w"))
    L = ["# Support -> resistance range trade (prereg docs/studies/sr_range_trade_prereg.json)", "",
         f"name-days {len(namedays)}; trades {len(trades)}; controls {len(ctrls)}; drops {dict(drops)}", ""]

    def tstat(xs, dd):
        m = statistics.fmean(xs)
        by = collections.defaultdict(float)
        for v, d in zip(xs, dd):
            by[d] += v - m
        G, n = len(by), len(xs)
        se = math.sqrt(sum(s * s for s in by.values()) * G / (G - 1)) / n if G > 1 else float("nan")
        return m, (m / se if se and se == se else float("nan"))
    res = {}
    for hh in ("A", "B", "all"):
        tr = [t for t in trades if hh == "all" or half[t["day"]] == hh]
        if len(tr) < 5:
            L.append(f"- half {hh}: {len(tr)} trades (too few)")
            res[hh] = None
            continue
        m, t = tstat([x["net"] * 1e4 for x in tr], [x["day"] for x in tr])
        gm = statistics.fmean([x["gross"] * 1e4 for x in tr])
        pairs = [(trades[cx["pair"]]["net"] - cx["net"]) * 1e4 for cx in ctrls if hh == "all" or half[cx["day"]] == hh]
        pd_ = [cx["day"] for cx in ctrls if hh == "all" or half[cx["day"]] == hh]
        pm, pt = tstat(pairs, pd_) if len(pairs) > 4 else (float("nan"), float("nan"))
        win = sum(x["net"] > 0 for x in tr) / len(tr)
        L.append(f"- half {hh}: trades {len(tr)} on {len({x['day'] for x in tr})} days | net {m:+.1f} bp (day-clustered t {t:+.2f}), "
                 f"gross {gm:+.1f}, win {win:.0%}, median room {statistics.median(x['room'] for x in tr):.2f}% | "
                 f"minus same-bracket random control {pm:+.1f} bp (t {pt:+.2f}, n {len(pairs)})")
        res[hh] = (len(tr), m, t, pm, pt)
    if any(res[h] is None or res[h][0] < 50 for h in ("A", "B")):
        v = "UNDERPOWERED (fewer than 50 trades in a half): no verdict"
    elif all(res[h][1] > 0 and res[h][2] >= 2 and res[h][3] >= 5 and res[h][4] >= 2 for h in ("A", "B")):
        v = "PASS (earns a >= 10-session held-out confirmation; not a strategy yet)"
    else:
        v = "FAIL"
    L.append("")
    L.append(f"**PRIMARY: {v}** (bar, both halves: net > 0 with t >= 2, AND beats the same-bracket random control by >= 5 bp with t >= 2, n >= 50)")
    open(os.path.join(OUT, "report.md"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
