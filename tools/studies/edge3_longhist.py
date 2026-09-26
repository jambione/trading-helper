#!/usr/bin/env python3
"""edge3_longhist.py — Round 3 (2026-09-26): long-history re-test, NO retuning, of the
single rule promoted in round 2 (edge2_study.py cell "G IDX4 r1&r12 hiVol ->15:50"):

  SPY QQQ IWM DIA; first-30-min realized vol (std of 1m close returns 09:30-10:00) above the
  66.7th percentile of that ETF's previous 20 qualifying days; at 15:30, if sign(prior close
  -> 10:00) == sign(15:00 -> 15:30), trade that direction 15:30 -> 15:50 (info: -> 16:00).
  Net = gross - SIP quoted spread at 15:30 (round trip). Same code path/guards as round 2.

Data: free IEX 1m bars, 09:25-16:05, as far back as Alpaca serves (from 2016), cached per
month in ai_reports/edge_iex_bars/idx4/YYYY-MM.pkl. Spreads: SIP quotes (free historical)
at 15:31 on the 1st and 15th-ish trading day of every month -> per sym-month median.
Verification: the 250-day round-2 window must reproduce round 2's numbers.

USAGE  edge3_longhist.py fetch | spreads | test
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import edge_common as ec  # noqa: E402
import edge_fetch as ef  # noqa: E402

ET = ZoneInfo("America/New_York")
SYMS = ["SPY", "QQQ", "IWM", "DIA"]
OUT = os.path.join(ROOT, "ai_reports", "edge_iex_bars", "idx4")
SPR = os.path.join(ROOT, "ai_reports", "edge3_spreads.json")
START, END = "2016-01", "2026-09"
USED_FROM = "2025-09-29"  # round-2 window (tune+validate+holdout) starts here


def months():
    y, m = map(int, START.split("-"))
    while f"{y:04d}-{m:02d}" <= END:
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def fetch():
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    cl = bars.client()
    os.makedirs(OUT, exist_ok=True)
    for y, m in months():
        p = os.path.join(OUT, f"{y:04d}-{m:02d}.pkl")
        if os.path.exists(p):
            continue
        s = datetime(y, m, 1, tzinfo=ET)
        e = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=ET)
        ef.throttle()
        try:
            bs = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                                                    start=s.astimezone(timezone.utc), end=e.astimezone(timezone.utc),
                                                    feed=DataFeed.IEX))
        except Exception as ex:  # noqa: BLE001
            print(f"{y}-{m:02d}: {ex}"[:160], flush=True)
            continue
        out = defaultdict(dict)
        for sym, rs in (bs.data or {}).items():
            byday = defaultdict(list)
            for r in rs:
                dt = r.timestamp.astimezone(ET)
                if 9 * 60 + 25 <= dt.hour * 60 + dt.minute < 16 * 60 + 5:
                    byday[dt.strftime("%Y-%m-%d")].append(r)
            for day, rr in byday.items():
                out[day][sym] = (np.array([int(r.timestamp.timestamp()) for r in rr], np.int64),
                                 np.array([r.open for r in rr]), np.array([r.high for r in rr]),
                                 np.array([r.low for r in rr]), np.array([r.close for r in rr]),
                                 np.array([r.volume for r in rr]))
        pickle.dump(dict(out), open(p, "wb"))
        print(f"{y}-{m:02d}: {len(out)} days", flush=True)


def all_days():
    D = {}
    for y, m in months():
        p = os.path.join(OUT, f"{y:04d}-{m:02d}.pkl")
        if os.path.exists(p):
            D.update(pickle.load(open(p, "rb")))
    return D


def spreads():
    cl = bars.client()
    store = json.load(open(SPR)) if os.path.exists(SPR) else {}
    days = sorted(d for d, v in all_days().items() if len(v) == 4)
    bym = defaultdict(list)
    for d in days:
        bym[d[:7]].append(d)
    for mo, ds in sorted(bym.items()):
        for d in (ds[0], ds[len(ds) // 2]):
            t = datetime.strptime(d, "%Y-%m-%d").replace(hour=15, minute=31, tzinfo=ET).timestamp()
            ef.fetch_spreads(cl, t, SYMS, 5, store)
        json.dump(store, open(SPR, "w"))
        print(f"spreads {mo}", flush=True)


def test():
    store = json.load(open(SPR)) if os.path.exists(SPR) else {}
    sm_mo = defaultdict(list)
    for k, v in store.items():
        if v:
            s, ts = k.split("|")
            sm_mo[(s, datetime.fromtimestamp(int(ts), ET).strftime("%Y-%m"))].append(v)
    sm_mo = {k: float(np.median(v)) for k, v in sm_mo.items()}
    r2 = ec.SpreadModel(set(SYMS))  # round-2 model (used for the round-2 window, as in round 2)
    sym_all = defaultdict(list)
    for (s, mo), v in sm_mo.items():
        sym_all[s].append(v)

    def spread(s, day):
        if day >= USED_FROM:
            return r2.get(s, day, 360)
        v = sm_mo.get((s, day[:7]))
        return v if v is not None else float(np.median(sym_all[s]))

    Dd = all_days()
    days = sorted(d for d in Dd if d <= "2026-09-25")
    rows = []  # (day, sym, net1550, gross1550, net1600, gross1600, spread)
    hist = defaultdict(list)
    prev = {}
    for day in days:
        D = {s: ec.Dense(B, day) for s, B in Dd[day].items() if len(B[0]) > 100}
        for s in SYMS:
            if s not in D or s not in prev:
                continue
            d, pc = D[s], prev[s].Cf[389]
            if np.isnan(pc):
                continue
            first = np.where(~np.isnan(d.O[:3]))[0]
            if not len(first) or d.age[29] > 3 or d.age[359] > 3 or d.age[329] > 3:
                continue
            r1, r12 = d.Cf[29] / pc - 1, d.Cf[359] / d.Cf[329] - 1
            c30 = d.Cf[:30]
            rv30 = float(np.nanstd(np.diff(c30) / c30[:-1]))
            hv = len(hist[s]) >= 20 and rv30 > np.percentile(hist[s][-20:], 66.7)
            hist[s].append(rv30)
            sig = np.sign(r1) if np.sign(r1) == np.sign(r12) else 0
            if not hv or sig == 0:
                continue
            sp = spread(s, day)
            g50 = sig * (d.Cf[379] / d.Cf[359] - 1) * 1e4
            g60 = sig * (d.Cf[389] / d.Cf[359] - 1) * 1e4
            rows.append((day, s, g50 - sp, g50, g60 - sp, g60, sp))
        prev = D
    di = {d: i for i, d in enumerate(days)}
    print(f"IEX history: {days[0]}..{days[-1]} ({len(days)} days with bars); qualifying trades {len(rows)}")

    def line(label, rr, ndays):
        if len(rr) < 5:
            print(f"  {label:26} n {len(rr)}"); return
        A = np.array([(di[r[0]], r[2], r[3], r[4], r[5], r[6]) for r in rr])
        s50 = ec.stats(A[:, 1], A[:, 0], gross=A[:, 2], n_days_total=ndays)
        s60 = ec.stats(A[:, 3], A[:, 0], gross=A[:, 4], n_days_total=ndays)
        _u, _i = np.unique(A[:, 0], return_inverse=True)
        dmean = float((np.bincount(_i, weights=A[:, 1]) / np.bincount(_i)).mean())
        print(f"  {label:26} n {s50['n']:4d} ({s50['per_day']:.2f}/d)  spr {A[:, 5].mean():4.2f}  ->15:50 gross {s50['gross']:+6.1f} "
              f"net {s50['net']:+6.1f} med {s50['median']:+6.1f} win {s50['win']:4.0%} t {s50['t']:+5.2f} daymean {dmean:+5.1f} "
              f"CI [{s50['lo']:+6.1f},{s50['hi']:+6.1f}]  | ->16:00 net {s60['net']:+6.1f} t {s60['t']:+5.2f}")

    print("\nPer year (net bp/trade, day-clustered t; CI = day bootstrap):")
    for y in sorted({d[:4] for d in days}):
        yd = [d for d in days if d[:4] == y and d < USED_FROM]
        line(f"{y}" + (" (to 09-26)" if y == "2025" else ""), [r for r in rows if r[0][:4] == y and r[0] < USED_FROM], len(yd))
    new = [d for d in days if d < USED_FROM]
    print("\nPooled:")
    line("NEW history (excl. round 2)", [r for r in rows if r[0] < USED_FROM], len(new))
    for s in SYMS:
        line(f"  new, {s}", [r for r in rows if r[0] < USED_FROM and r[1] == s], len(new))
    for lab, a, b in (("2016-2019", "2016", "2020"), ("2020-2022", "2020", "2023"), ("2023-2025-09", "2023", USED_FROM)):
        line(f"  new, {lab}", [r for r in rows if a <= r[0] < b], len([d for d in days if a <= d < b]))
    used = [d for d in days if d >= USED_FROM]
    print("\nReproduction of the round-2 window (should match round 2: tune +4.3, validate +5.9, holdout +9.0):")
    reg = json.load(open(os.path.join(ROOT, "ai_reports", "edge2_split.json")))
    for part in ("tune", "validate", "holdout"):
        ps = set(reg[part])
        line(f"  round-2 {part}", [r for r in rows if r[0] in ps], len(ps))
    line("ALL (new + round 2)", rows, len(days))


if __name__ == "__main__":
    {"fetch": fetch, "spreads": spreads, "test": test}[sys.argv[1]]()
