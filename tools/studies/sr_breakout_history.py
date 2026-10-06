#!/usr/bin/env python3
"""Breakout distance above a just-broken LuxAlgo sell zone, on UNTOUCHED history (2026-05-01..09-10).
Pre-registration: docs/studies/sr_breakout_history_prereg.json (committed before this runs).

Universe: per session, the 40 largest opening gappers with open >= $10, prior-day dollar volume >= $20M, gap >= +2%
(sip_breakout_study.universe, the same daily panel). Bars: SIP 1-minute, split-adjusted, prior trading day + the day,
04:00-16:00 ET (fetched here, cached in ai_reports/sr_breakout_hist/).
Blocks: tools/order_blocks.py (swing 10, wicks), point-in-time, charted last 3 per side.
EVENTS (desk-like square moments): a 1-min close 09:40-15:00 ET with fast %R (21, EMA 7) >= -20 AND slow %R
  (112, EMA 3) >= -20; at most one per name per clock 15-min block (the first).
FEATURE: brk = % the close sits above the top of the most recently broken charted bearish OB (it became a breaker
  within the last 15 min and the close is still above its top); None = no breakout.
  bands: none | poke [0, 0.10) | clean [0.10, 0.30) | chase [0.30, inf)
OUTCOME net15: buy at the next bar's open, sell at the close 15 min later (15:55 cap), minus 0.20%.
Run on the mini after the close: .venv/bin/python tools/studies/sr_breakout_history.py
"""
from __future__ import annotations

import collections
import json
import math
import os
import pickle
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import bars as B  # noqa: E402
import order_blocks as OB  # noqa: E402
import sip_breakout_study as SBS  # noqa: E402
from order_block_gate import fe_diff  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = os.path.join(ROOT, "ai_reports", "sr_breakout_hist")
LO, HI = "2026-05-01", "2026-09-10"
COST = 0.0020
BANDS = (("poke", 0.0, 0.10), ("clean", 0.10, 0.30), ("chase", 0.30, 1e9))


def ema(xs, n):
    out, k = [], 2 / (n + 1)
    for v in xs:
        out.append(v if not out else out[-1] + k * (v - out[-1]))
    return out


def wr(h, l, c, n, sm):
    raw = []
    for i in range(len(c)):
        a = max(0, i - n + 1)
        hh, ll = max(h[a:i + 1]), min(l[a:i + 1])
        raw.append(-100.0 * (hh - c[i]) / (hh - ll) if hh > ll else -50.0)
    return ema(raw, sm)


def fetch(day, prev, syms, cl):
    p = os.path.join(OUT, f"min_{day}.pkl")
    if os.path.exists(p):
        return pickle.load(open(p, "rb"))
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    d0 = datetime.strptime(prev, "%Y-%m-%d").replace(hour=4, tzinfo=ET)
    d1 = datetime.strptime(day, "%Y-%m-%d").replace(hour=16, tzinfo=ET)
    out = None
    for att in range(5):
        try:
            r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                                                   start=d0.astimezone(timezone.utc), end=d1.astimezone(timezone.utc),
                                                   feed=DataFeed.SIP, adjustment=Adjustment.SPLIT))
            out = {}
            for s, rows in (r.data or {}).items():
                keep = []
                for b in rows:
                    tl = b.timestamp.astimezone(ET)
                    if 4 * 60 <= tl.hour * 60 + tl.minute < 16 * 60:
                        keep.append((b.timestamp.timestamp(), float(b.open), float(b.high), float(b.low), float(b.close)))
                out[s] = keep
            break
        except Exception as e:  # noqa: BLE001
            print(f"  fetch {day} try {att}: {str(e)[:90]}", flush=True)
            time.sleep(10 * (att + 1))
    if out is None:
        return None                       # not cached: a rerun retries; counted as a failed day
    pickle.dump(out, open(p, "wb"))
    time.sleep(0.5)
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    daily = pickle.load(open(os.path.join(ROOT, "ai_reports/allsym/daily_2025-09-01_2026-10-02.pkl"), "rb"))
    SBS.LO, SBS.HI = LO, HI
    uni = SBS.universe(daily)
    days = sorted(uni)
    cal = sorted({r[0] for rows in daily.values() for r in rows})
    cl = B.client()
    ev, fails, nd = [], [], 0
    for day in days:
        prev = cal[cal.index(day) - 1]
        mb = fetch(day, prev, uni[day], cl)
        if mb is None:
            fails.append(day)
            continue
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        t_lo, t_hi, t_end = d.replace(hour=9, minute=40).timestamp(), d.replace(hour=15).timestamp(), d.replace(hour=15, minute=55).timestamp()
        for sym in uni[day]:
            rows = sorted(mb.get(sym) or [])
            if len(rows) < 150:
                continue
            nd += 1
            h = [r[2] for r in rows]
            l = [r[3] for r in rows]
            c = [r[4] for r in rows]
            fast, slow = wr(h, l, c, 21, 7), wr(h, l, c, 112, 3)
            states = list(OB.order_blocks(rows, bar_sec=60))
            used = set()
            for i in range(len(rows) - 1):
                tq = rows[i][0] + 60
                if not (t_lo <= tq <= t_hi) or fast[i] < -20 or slow[i] < -20:
                    continue
                blk = int((tq - d.timestamp()) // 900)
                if blk in used:
                    continue
                used.add(blk)
                ch = OB.charted([b for b in states[i][1] if b.known_ts <= tq])
                rec = [b for b in ch if b.kind == "bear" and b.breaker and b.break_ts is not None
                       and tq - 900 <= b.break_ts <= tq and c[i] > b.top]
                brk = (c[i] / max(rec, key=lambda b: b.break_ts).top - 1) * 100 if rec else None
                entry = rows[i + 1][1]
                tx = min(rows[i + 1][0] + 900, t_end)
                j = max(k for k in range(i + 1, len(rows)) if rows[k][0] <= tx) if rows[i + 1][0] <= tx else None
                if j is None or j <= i + 1 or abs(rows[j][0] - tx) > 180:
                    continue
                ev.append({"day": day, "sym": sym, "brk": brk, "net15": (c[j] / entry - 1 - COST) * 1e4})
    json.dump({"events": ev, "fails": fails}, open(os.path.join(OUT, "events.json"), "w"))
    dd = sorted({e["day"] for e in ev})
    half = {x: (1 if k < len(dd) // 2 else 2) for k, x in enumerate(dd)}
    L = ["# Breakout distance on untouched history (prereg docs/studies/sr_breakout_history_prereg.json)", "",
         f"sessions {len(dd)} ({dd[0]}..{dd[-1]}), name-days {nd}, events {len(ev)}, failed days {fails}",
         f"halves: H1 {dd[0]}..{dd[len(dd) // 2 - 1]}, H2 {dd[len(dd) // 2]}..{dd[-1]}", ""]

    def band(b):
        if b is None:
            return "none"
        return next(lab for lab, lo, hi in BANDS if lo <= b < hi)
    for e in ev:
        e["band"] = band(e["brk"])
    res = {}
    for lab in ("poke", "clean", "chase"):
        for h in (1, 2, "all"):
            xs = [e for e in ev if e["band"] in (lab, "none") and (h == "all" or half[e["day"]] == h)]
            g = [1 if e["band"] == lab else 0 for e in xs]
            n1 = sum(g)
            if n1 < 5:
                L.append(f"- {lab} H{h}: n {n1} (too few)")
                res[(lab, h)] = None
                continue
            y = [e["net15"] for e in xs]
            b, se = fe_diff(y, g, [e["day"] for e in xs])
            m1 = statistics.fmean([v for v, x in zip(y, g) if x])
            m0 = statistics.fmean([v for v, x in zip(y, g) if not x])
            L.append(f"- {lab} H{h}: n {n1} mean {m1:+.1f} vs none {m0:+.1f} (n {len(g) - n1}) | DAY-FE diff {b:+.1f} bp, t {b / se:+.2f}")
            res[(lab, h)] = (n1, b, b / se)
        L.append("")

    def verdict(lab, sign):
        r1, r2, ra = res[(lab, 1)], res[(lab, 2)], res[(lab, "all")]
        if any(r is None or r[0] < 100 for r in (r1, r2)):
            return "UNDERPOWERED (fewer than 100 events in a half)"
        ok = all(sign * r[1] >= 5 and sign * r[2] >= 2 for r in (r1, r2)) and sign * ra[2] >= 2.24
        return "PASS" if ok else "FAIL"
    L.append(f"**H2 chase (>= 0.30%) worse than no breakout: {verdict('chase', -1)}**")
    L.append(f"**H3 clean (0.10-0.30%) better than no breakout: {verdict('clean', +1)}**")
    L.append("(each: in BOTH halves the difference is >= 5 bp in the predicted direction with |t| >= 2, pooled |t| >= 2.24, >= 100 events per half)")
    open(os.path.join(OUT, "report.md"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
