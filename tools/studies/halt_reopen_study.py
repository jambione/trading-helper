#!/usr/bin/env python3
"""halt_reopen_study.py — pre-registered in docs/studies/halt_reopen_prereg.json (5eb0044).

Candidates: common stock name-days with open >= $1 and (high - low) / prior close >= 15% (all-symbol daily panel).
Halts are inferred from SIP 1-minute RTH bars: >= 5 consecutive missing minutes between 09:35 and 15:45 in a name
that printed in each of the 5 minutes before the gap. UP halt: last close before the gap >= +5% vs 10 minutes
earlier; DOWN: <= -5%. Entry = open of the first bar after the gap (proxy for the reopening cross; no entry cost).
Exit = close of the last bar at or before +5 / +15 / +30 min (cap 15:55), selling at the SIP bid: cost = SIP
half-spread at the exit time + 10 bp. t clustered by day. Halves by date.

PASS: long UP halts at +15 min, net >= +50 bp with day-clustered t >= 2 in BOTH halves, n >= 100 per half.

USAGE (mini):  .venv/bin/python tools/studies/halt_reopen_study.py ai_reports/allsym/daily_2025-09-01_2026-10-02.pkl
"""
from __future__ import annotations

import json
import math
import os
import pickle
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import exec_report as er  # noqa: E402

OUT = os.path.join(ROOT, "ai_reports", "allsym")
LO, HI = "2025-10-01", "2026-10-02"
HORIZONS = (5, 15, 30)
SLIP_BP = 10.0
QCACHE = os.path.join(OUT, "halt_quotes.json")


def candidates(daily):
    by_day = defaultdict(list)
    for sym, rows in daily.items():
        for (d0, o0, h0, l0, c0, v0), (d, o, h, l, c, v) in zip(rows, rows[1:]):
            if LO <= d <= HI and o >= 1 and c0 > 0 and (h - l) / c0 >= 0.15:
                by_day[d].append(sym)
    return by_day


def minute_bars(day, syms, cl):
    p = os.path.join(OUT, f"min_{day}.pkl")
    if os.path.exists(p):
        return pickle.load(open(p, "rb"))
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET)
    out = {}
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        for attempt in range(4):
            try:
                r = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=chunk, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=d.replace(hour=9, minute=30), end=d.replace(hour=16, minute=0),
                    feed=DataFeed.SIP, adjustment=Adjustment.SPLIT))
                for s, rows in (r.data or {}).items():
                    out[s] = [(b.timestamp.timestamp(), float(b.open), float(b.close)) for b in rows]
                break
            except Exception as e:  # noqa: BLE001
                print(f"  {day} chunk {i} try {attempt}: {str(e)[:70]}", flush=True)
                time.sleep(5 * (attempt + 1))
        time.sleep(0.5)
    pickle.dump(out, open(p, "wb"))
    return out


def halts(day, rows):
    """[(halt_start_idx, reopen_idx, direction)] from 1-minute bars."""
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET)
    t935 = d.replace(hour=9, minute=35).timestamp()
    t1545 = d.replace(hour=15, minute=45).timestamp()
    ts = [r[0] for r in rows]
    out = []
    for i in range(5, len(rows) - 1):
        gap = (ts[i + 1] - ts[i]) / 60.0
        if gap < 6 or not (t935 <= ts[i] <= t1545):
            continue
        if ts[i] - ts[i - 4] > 4 * 60 + 1:          # a bar in each of the 5 minutes before
            continue
        j = i
        while j > 0 and ts[i] - ts[j - 1] <= 600:
            j -= 1
        ref = rows[j][2]
        mv = rows[i][2] / ref - 1 if ref > 0 else 0.0
        if mv >= 0.05:
            out.append((i, i + 1, "UP"))
        elif mv <= -0.05:
            out.append((i, i + 1, "DOWN"))
    return out


def main():
    daily = pickle.load(open(sys.argv[1], "rb"))
    cands = candidates(daily)
    cl = bars.client()
    qcache = json.load(open(QCACHE)) if os.path.exists(QCACHE) else {}
    ev = []
    days = sorted(cands)
    print(f"candidate name-days {sum(len(v) for v in cands.values())} over {len(days)} days", flush=True)
    for n, day in enumerate(days):
        mb = minute_bars(day, sorted(cands[day]), cl)
        close_t = datetime.strptime(day, "%Y-%m-%d").replace(hour=15, minute=55, tzinfo=bars.ET).timestamp()
        for sym, rows in mb.items():
            seen_first = False
            for hi_, ro, dirn in halts(day, rows):
                t_re = rows[ro][0]
                entry = rows[ro][1]
                if entry <= 0:
                    continue
                e = {"day": day, "sym": sym, "dir": dirn, "first": not seen_first, "px": entry}
                seen_first = True
                for H in HORIZONS:
                    target = min(t_re + H * 60, close_t)
                    k = ro
                    while k + 1 < len(rows) and rows[k + 1][0] <= target and rows[k + 1][0] - rows[k][0] < 6 * 60:
                        k += 1
                    xpx, xts = rows[k][2], rows[k][0] + 60
                    key = f"{sym}|{xts:.0f}"
                    if key not in qcache:
                        q = er.nbbo_at(cl, sym, datetime.fromtimestamp(xts, timezone.utc))
                        qcache[key] = list(q) if q else None
                        time.sleep(0.35)   # the live engine shares these data keys
                    q = qcache[key]
                    half = ((q[1] - q[0]) / ((q[0] + q[1]) / 2) * 1e4 / 2) if q and q[1] > q[0] > 0 else None
                    gross = (xpx / entry - 1) * 1e4
                    e[f"g{H}"] = gross
                    e[f"n{H}"] = None if half is None else gross - half - SLIP_BP
                ev.append(e)
        if n % 10 == 0:
            json.dump(qcache, open(QCACHE, "w"))
            print(f"  {day}: {len(ev)} halt events so far", flush=True)
    json.dump(qcache, open(QCACHE, "w"))
    pickle.dump(ev, open(os.path.join(OUT, "halt_events.pkl"), "wb"))

    half_day = days[len(days) // 2]

    def dct(xs, k):
        by = defaultdict(list)
        for x in xs:
            if x.get(k) is not None:
                by[x["day"]].append(x[k])
        allv = [v for vs in by.values() for v in vs]
        dm = [statistics.mean(v) for v in by.values()]
        t = (statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm)))
             if len(dm) > 2 and statistics.stdev(dm) > 0 else float("nan"))
        return (statistics.mean(allv) if allv else float("nan")), t, len(allv)

    print(f"\nHALT REOPEN {days[0]}..{days[-1]}: {len(ev)} events (UP {sum(e['dir'] == 'UP' for e in ev)}, "
          f"DOWN {sum(e['dir'] == 'DOWN' for e in ev)}); halves split at {half_day}\n")
    verdict = True
    for dirn, lab in (("UP", "PRIMARY long UP halts"), ("DOWN", "SECONDARY long DOWN halts")):
        print(f"== {lab}")
        for hname, sel in (("first", lambda e: e["day"] < half_day), ("second", lambda e: e["day"] >= half_day)):
            xs = [e for e in ev if e["dir"] == dirn and sel(e)]
            parts = []
            for H in HORIZONS:
                g = dct(xs, f"g{H}")
                m, t, n = dct(xs, f"n{H}")
                parts.append(f"+{H}m net {m:+6.0f} (t {t:+.2f}, n {n}) gross {g[0]:+6.0f}")
                if dirn == "UP" and H == 15:
                    verdict &= (m >= 50 and t >= 2 and n >= 100)
            print(f"  {hname:<7}" + "  |  ".join(parts))
        for lo, hi in ((1, 5), (5, 10), (10, 1e9)):
            xs = [e for e in ev if e["dir"] == dirn and lo <= e["px"] < hi]
            m, t, n = dct(xs, "n15")
            print(f"    price ${lo}-{hi if hi < 1e9 else '+'}: +15m net {m:+.0f} (t {t:+.2f}, n {n})")
        xs = [e for e in ev if e["dir"] == dirn and e["first"]]
        m, t, n = dct(xs, "n15")
        print(f"    first halt of the day only: +15m net {m:+.0f} (t {t:+.2f}, n {n})\n")
    print(f"PRE-REGISTERED VERDICT (long UP halts, +15 min): {'PASS' if verdict else 'FAIL'}")


if __name__ == "__main__":
    main()
