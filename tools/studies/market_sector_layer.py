#!/usr/bin/env python3
"""market_sector_layer - the market + sector tailwind as a layer on arm_then_fire's wide fires.
Spec: docs/studies/market_sector_layer_prereg.json (amended_1).

    .venv/bin/python tools/studies/market_sector_layer.py fetch     # sector ETF SIP 1-minute RTH bars 2026-01-15..11-19 (after 16:05 ET)
"""
from __future__ import annotations

import json
import pickle
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
OUT = ROOT / "ai_reports" / "market_sector_layer"
ET = ZoneInfo("America/New_York")
ETFS = ("SPY", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC", "SMH", "XBI", "IBIT", "KRE")
START, END = "2026-01-15", "2026-11-19"


def fetch():
    """One request per ETF per ~month (RTH filtered after), resumable. -> OUT/etf_bars.pkl {sym: {day: (o,h,l,c,v) 390-arrays}}."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment
    now = datetime.now(ET)
    if now.weekday() < 5 and "0900" <= now.strftime("%H%M") < "1605":
        sys.exit("market hours - rerun after 16:05 ET (shared data key)")
    k = json.load(open(ROOT / "secrets_data.json"))
    cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    OUT.mkdir(parents=True, exist_ok=True)
    dst = OUT / "etf_bars.pkl"
    res = pickle.load(open(dst, "rb")) if dst.exists() else {}
    failed = []
    for sym in ETFS:
        if sym in res and res[sym].get("_done"):
            continue
        res[sym] = {}
        a = datetime.fromisoformat(START + "T09:30").replace(tzinfo=ET)
        end = datetime.fromisoformat(END + "T16:00").replace(tzinfo=ET)
        while a < end:
            b = min(a + timedelta(days=31), end)
            got = None
            for attempt in range(6):
                try:
                    got = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute, start=a, end=b,
                                                             feed=DataFeed.SIP, adjustment=Adjustment.RAW))
                    break
                except Exception as e:                  # noqa: BLE001
                    time.sleep(60 if "429" in str(e) else 5 * (attempt + 1))
            if got is None:
                failed.append((sym, a.date().isoformat()))
            else:
                for r in (got.data or {}).get(sym, []):
                    t = r.timestamp.astimezone(ET)
                    m = (t.hour - 9) * 60 + t.minute - 30
                    if not 0 <= m < 390:
                        continue
                    day = t.strftime("%Y-%m-%d")
                    arr = res[sym].setdefault(day, np.full((5, 390), np.nan))
                    arr[:, m] = (r.open, r.high, r.low, r.close, r.volume)
            a = b
        res[sym]["_done"] = True
        pickle.dump(res, open(dst, "wb"))
        print(sym, len(res[sym]) - 1, "days", flush=True)
    print("FETCH_DONE failed", failed, flush=True)




# ---------------------------------------------------------------- scoring (amended_1)

import math  # noqa: E402
from collections import defaultdict  # noqa: E402

SECTORS = ETFS[1:]
SEARCH, SPLIT, CONFIRM = ("2026-04-28", "2026-08-31"), "2026-07-15", ("2026-09-01", "2026-11-19")
PEER_MIN, MDE_MAX, EDGE_T = 15, 12.0, 2.87


def ret15(arr_c, t, lb=15, max_age=2):
    """Return from the last close at or before t-lb to the last close at or before t, each at most max_age minutes
    old (amended_1 #3). None if unknown. Pure."""
    def last(i):
        for j in range(i, max(i - max_age - 1, -1), -1):
            if 0 <= j < len(arr_c) and np.isfinite(arr_c[j]):
                return float(arr_c[j])
        return None
    a, b = last(t - lb), last(t)
    return None if a is None or b is None else b / a - 1


def daily_close(arr):
    v = arr[np.isfinite(arr)]
    return float(v[-1]) if len(v) else np.nan


def corr_map(name_closes, etf_closes, days, day):
    """Sector ETF with the highest correlation of daily returns over the 60 sessions before `day` (>= 50 overlapping,
    corr >= 0.3) or None (UNMAPPED). name_closes/etf_closes: {day: close}. Pure."""
    prior = [d for d in days if d < day][-61:]
    best, bc = None, 0.3
    nr = [(prior[i], name_closes.get(prior[i]), name_closes.get(prior[i - 1])) for i in range(1, len(prior))]
    for e in SECTORS:
        x, y = [], []
        for d, a, b in nr:
            ea, eb = etf_closes[e].get(d), etf_closes[e].get(prior[prior.index(d) - 1])
            if all(v is not None and np.isfinite(v) and v > 0 for v in (a, b, ea, eb)):
                x.append(a / b - 1)
                y.append(ea / eb - 1)
        if len(x) >= 50 and np.std(x) > 0 and np.std(y) > 0:
            c = float(np.corrcoef(x, y)[0, 1])
            if c >= bc:
                best, bc = e, c
    return best


def layer_rows(res, etf, fs, maps, shift=0):
    """Attach SPY/sector 15-minute states (read at t + shift for the placebo) to every fire. Pure."""
    out = []
    for f in fs:
        day, t = f["day"], f["t"] + shift
        if not (0 <= t < 390) or day not in etf["SPY"]:
            continue
        spy = ret15(etf["SPY"][day][3], t)
        sec = maps.get((day, f["sym"]))
        sr = ret15(etf[sec][day][3], t) if sec and day in etf[sec] else None
        if spy is None or (sec and sr is None):
            continue
        out.append(dict(f, spy=spy, sec=sec, sr=sr,
                        R1=bool(spy > 0 and (sr if sec else spy) > 0), SECT=(sr - spy > 0) if sec else None))
    return out


def peer_lift(rows, on, off, match_spy=True, mapped_only=True):
    """ON fires minus up to 5 OFF fires of the same trigger within +/-15 minutes on other names, same volatility tercile,
    gap bucket and (if match_spy) the same SPY 15-minute sign; weighted by peer count. -> (rows, dropped). Pure."""
    import bisect
    by = defaultdict(list)
    for f in rows:
        by[(f["day"], f["trig"])].append(f)
    out, dropped = [], 0
    for (day, trig), ff in by.items():
        grp = defaultdict(list)                     # OFF fires bucketed by (vt, gb, spy sign), sorted by t
        for u in ff:
            if off(u) and (not mapped_only or u["sec"]):
                grp[(u["vt"], u["gb"], (u["spy"] > 0) if match_spy else None)].append(u)
        idx = {k: ([u["t"] for u in sorted(v, key=lambda x: x["t"])], sorted(v, key=lambda x: x["t"])) for k, v in grp.items()}
        for f in ff:
            if not on(f) or (mapped_only and not f["sec"]):
                continue
            key = (f["vt"], f["gb"], (f["spy"] > 0) if match_spy else None)
            if key not in idx:
                dropped += 1
                continue
            ts, us = idx[key]
            lo, hi = bisect.bisect_left(ts, f["t"] - PEER_MIN), bisect.bisect_right(ts, f["t"] + PEER_MIN)
            c = [u for u in us[lo:hi] if u["sym"] != f["sym"]]
            c = sorted(c, key=lambda u: hkey("msl-peer", day, f["sym"], f["t"], u["sym"]))[:5]
            if not c:
                dropped += 1
                continue
            pm = float(np.mean([u["g"] for u in c]))
            out.append({"day": day, "trig": trig, "sym": f["sym"], "t": f["t"], "g": f["g"], "lift": f["g"] - pm,
                        "w": len(c) / (len(c) + 1)})
    return out, dropped


def hkey(*p):
    import hashlib
    return int(hashlib.sha256("|".join(str(x) for x in p).encode()).hexdigest(), 16)


def cell(rows, dropped):
    import arm_then_fire as A
    a = [r for r in rows if r["day"] < SPLIT]
    b = [r for r in rows if r["day"] >= SPLIT]
    m1, se1, _, d1 = A.day_stats(a)
    m2, se2, _, d2 = A.day_stats(b)
    m, se, t, dn = A.day_stats(rows)
    mde1, mde2 = (3.71 * se1 if se1 else None), (3.71 * se2 if se2 else None)
    n = len(rows) + dropped
    st = {"n": len(rows), "dropped": dropped, "dropped_share": round(dropped / n, 3) if n else None,
          "lift_h1": m1, "lift_h2": m2, "t": t, "mde_h1": mde1, "mde_h2": mde2, "days_h1": d1, "days_h2": d2,
          "winsor_lift": A.winsor([r["lift"] for r in rows])}
    if (mde1 or 99) > MDE_MAX or (mde2 or 99) > MDE_MAX:
        st["verdict"] = "UNPROVEN (MDE)"
    elif (st["dropped_share"] or 1) > 0.30:
        st["verdict"] = "UNPROVEN (> 30% without a peer)"
    elif m1 and m2 and m1 > 0 and m2 > 0 and (t or 0) >= EDGE_T and (st["winsor_lift"] or 0) > 0:
        st["verdict"] = "EDGE (pending controls)"
    else:
        st["verdict"] = "NO EDGE"
    return st


def score():
    import arm_then_fire as A
    AT = A.OUT
    res = pickle.load(open(AT / "fires.pkl", "rb"))
    nw = pickle.load(open(AT / "news.pkl", "rb"))
    etf = pickle.load(open(OUT / "etf_bars.pkl", "rb"))
    for e in etf:
        etf[e].pop("_done", None)
    sessions = A.all_sessions()
    failed_syms = frozenset(s for batch in nw["failed"] for s in batch)
    res["days"] = {d: v for d, v in res["days"].items() if SEARCH[0] <= d <= SEARCH[1]}
    fs = A.assign(res, nw["news"], sessions=sessions, skip_syms=failed_syms)
    # daily closes for the correlation map: names from the warehouse, ETFs from the fetch
    from relative_strength_scan import load_day
    from evening_family import WH_SIP
    cal = [d for d in sessions if "2026-01-15" <= d <= SEARCH[1]]
    names = {f["sym"] for f in fs}
    ncl = defaultdict(dict)
    for d in cal:
        z = load_day(WH_SIP / f"{d}.npz")
        for s in names:
            i = z["idx"].get(s)
            if i is not None:
                ncl[s][d] = daily_close(z["c"][i])
    ecl = {e: {d: daily_close(v[3]) for d, v in etf[e].items()} for e in SECTORS}
    maps, mcount = {}, defaultdict(int)
    for f in fs:
        key = (f["day"], f["sym"])
        if key not in maps:
            maps[key] = corr_map(ncl[f["sym"]], ecl, cal, f["day"])
            mcount[maps[key] or "UNMAPPED"] += 1
    rows = layer_rows(res, etf, fs, maps)
    out = {"prereg": "docs/studies/market_sector_layer_prereg.json (amended_1)",
           "counts": {"fires": len(fs), "with_states": len(rows), "name_days_by_etf": dict(mcount),
                      "unmapped_share": round(mcount.get("UNMAPPED", 0) / max(sum(mcount.values()), 1), 3)}, "cells": {}}
    armed = lambda f: f["arm"] in ("W1", "W2", "W3")
    defs = {
        "C1": (lambda f: f["R1"], lambda f: not f["R1"], True),
        "C2": (lambda f: f["SECT"] is True, lambda f: f["SECT"] is False, True),
    }
    for trig in ("F1", "F2", "F3", "F4"):
        rr = [f for f in rows if f["trig"] == trig]
        for c, (on, off, ms) in defs.items():
            lr, dr = peer_lift(rr, on, off, match_spy=ms)
            st = cell(lr, dr)
            # time-shift placebo (20 shifts of +/-60..120 minutes): EDGE rate <= 10%
            hits = 0
            for s in range(20):
                sh = (60 + s * 3) * (1 if s % 2 else -1)
                pr = layer_rows(res, etf, [f for f in fs if f["trig"] == trig], maps, shift=sh)
                pl, pd = peer_lift(pr, on, off, match_spy=ms)
                hits += cell(pl, pd)["verdict"].startswith("EDGE")
            st["time_shift_placebo_rate"] = hits / 20
            if st["verdict"].startswith("EDGE") and hits / 20 > 0.10:
                st["verdict"] = "BLOCKED (time-shift placebo)"
            out["cells"][f"{c}x{trig}"] = st
        # C3: armed+R1 must beat BOTH armed+R1-off AND unarmed+R1-on
        la, da = peer_lift(rr, lambda f: armed(f) and f["R1"], lambda f: armed(f) and not f["R1"], True)
        lb, db = peer_lift(rr, lambda f: armed(f) and f["R1"], lambda f: f["arm"] == "UNARMED" and f["R1"], True)
        sa, sb = cell(la, da), cell(lb, db)
        v = "EDGE (pending controls)" if sa["verdict"].startswith("EDGE") and sb["verdict"].startswith("EDGE") else \
            ("UNPROVEN" if "UNPROVEN" in sa["verdict"] or "UNPROVEN" in sb["verdict"] else "NO EDGE")
        out["cells"][f"C3x{trig}"] = {"vs_armed_R1_off": sa, "vs_unarmed_R1_on": sb, "verdict": v}
    out["VERDICT"] = {k: v["verdict"] for k, v in out["cells"].items()}
    (OUT / "score.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"fetch": fetch, "score": score}.get(cmd, lambda: sys.exit(__doc__))()
