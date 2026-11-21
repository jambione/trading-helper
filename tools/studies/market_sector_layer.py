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
    nr = [(prior[i], prior[i - 1], name_closes.get(prior[i]), name_closes.get(prior[i - 1])) for i in range(1, len(prior))]
    for e in SECTORS:
        x, y = [], []
        for d, dp, a, b in nr:
            ea, eb = etf_closes[e].get(d), etf_closes[e].get(dp)
            if all(v is not None and np.isfinite(v) and v > 0 for v in (a, b, ea, eb)):
                x.append(a / b - 1)
                y.append(ea / eb - 1)
        if len(x) >= 50 and np.std(x) > 0 and np.std(y) > 0:
            c = float(np.corrcoef(x, y)[0, 1])
            if c >= bc:
                best, bc = e, c
    return best


def layer_rows(res, etf, fs, maps, shift=0, remap=None):
    """Attach SPY/sector 15-minute states (read at t + shift for the time placebo; from another day's ETF bars when
    remap {day: day'} is given - the ETF-shuffle canary) to every fire. Pure."""
    out = []
    for f in fs:
        day0, t = f["day"], f["t"] + shift
        day = remap.get(day0, day0) if remap else day0
        if not (0 <= t < 390) or day not in etf["SPY"]:
            continue
        spy = ret15(etf["SPY"][day][3], t)
        sec = maps.get((day, f["sym"]))
        sr = ret15(etf[sec][day][3], t) if sec and day in etf[sec] else None
        if spy is None or (sec and sr is None):
            continue
        out.append(dict(f, spy=spy, sec=sec, sr=sr, spy_up=spy > 0, sec_up=(sr > 0) if sec else None,
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
          "winsor_h1": A.winsor([r["lift"] for r in a]), "winsor_h2": A.winsor([r["lift"] for r in b])}
    if (mde1 or 99) > MDE_MAX or (mde2 or 99) > MDE_MAX:
        st["verdict"] = "UNPROVEN (MDE)"
    elif (st["dropped_share"] or 1) > 0.30:
        st["verdict"] = "UNPROVEN (> 30% without a peer)"
    elif m1 and m2 and m1 > 0 and m2 > 0 and (t or 0) >= EDGE_T and (st["winsor_h1"] or 0) > 0 and (st["winsor_h2"] or 0) > 0:
        st["verdict"] = "EDGE (pending controls)"
    else:
        st["verdict"] = "NO EDGE"
    return st


def controls(res, lr):
    """P2 controls for a cell's ON rows: unconditional (a random price-band minute on a random name that day, hash
    seed) and same-name later (the name's G at a minute in t+32 .. t+92). Means and the day-clustered gap. Pure."""
    import arm_then_fire as A
    unc, later = [], []
    for r in lr:
        D = res["days"][r["day"]]
        G, px = D["G"], D["px"]
        rng = np.random.default_rng(hkey("msl-unc", r["day"], r["sym"], r["t"]) % 2 ** 32)
        for _ in range(20):
            k, t = int(rng.integers(0, G.shape[0])), int(rng.integers(15, 360))
            if px[k, t] and np.isfinite(G[k, t]):
                unc.append({"day": r["day"], "v": float(G[k, t])})
                break
        k = D["syms"].index(r["sym"])
        t2 = r["t"] + 32 + hkey("msl-later", r["day"], r["sym"], r["t"]) % 61
        if t2 < 360 and np.isfinite(G[k, t2]):
            later.append({"day": r["day"], "v": float(G[k, t2]), "lift": r["g"] - float(G[k, t2])})
    ctl = [{"day": r["day"], "v": r["g"] - r["lift"]} for r in lr]
    def dm(rows):
        per = defaultdict(list)
        for x in rows:
            per[x["day"]].append(x["v"])
        return {k: float(np.mean(v)) for k, v in per.items()}
    pc, uc = dm(ctl), dm(unc)
    diffs = [pc[d] - uc[d] for d in pc if d in uc]
    t = float(np.mean(diffs) / (np.std(diffs, ddof=1) / math.sqrt(len(diffs)))) if len(diffs) > 2 and np.std(diffs) > 0 else None
    lm, _, lt, _ = A.day_stats(later, weighted=False)
    later_v = dm([{"day": x["day"], "v": x["v"]} for x in later])
    ld = [later_v[d] - uc[d] for d in later_v if d in uc]
    lt2 = float(np.mean(ld) / (np.std(ld, ddof=1) / math.sqrt(len(ld)))) if len(ld) > 2 and np.std(ld) > 0 else None
    on = dm([{"day": r["day"], "v": r["g"]} for r in lr])
    od = [on[d] - uc[d] for d in on if d in uc]
    om = float(np.mean(od)) if od else None
    return {"peer_mean": float(np.mean([x["v"] for x in ctl])) if ctl else None,
            "unconditional_mean": float(np.mean([x["v"] for x in unc])) if unc else None,
            "peer_vs_unconditional_day": {"diff": float(np.mean(diffs)) if diffs else None, "t": t},
            "later_vs_unconditional_day": {"diff": float(np.mean(ld)) if ld else None, "t": lt2},
            "on_minus_unconditional": om,
            "same_name_later_lift": lm, "same_name_later_t": lt}


def apply_controls(st, ct, placebo_rate, shuffle_rate):
    """Final verdict for an EDGE candidate (amended_1 #7 + arm_then_fire rules): time-shift placebo and ETF-shuffle
    <= 10%, same-name later lift > 0, ON minus unconditional > 0, peer and later controls within 2 SE of the
    unconditional mean. Pure."""
    if not st["verdict"].startswith("EDGE"):
        return st["verdict"]
    pu, lu = ct["peer_vs_unconditional_day"], ct["later_vs_unconditional_day"]
    if placebo_rate > 0.10:
        return "BLOCKED (time-shift placebo)"
    if shuffle_rate is not None and shuffle_rate > 0.10:
        return "BLOCKED (ETF-shuffle canary)"
    if not (ct["same_name_later_lift"] or -1) > 0:
        return "BLOCKED (same-name later lift not > 0)"
    if not (ct["on_minus_unconditional"] or -1) > 0:
        return "BLOCKED (ON fires not above the unconditional mean)"
    if (pu["t"] is not None and abs(pu["t"]) > 2) or (lu["t"] is not None and abs(lu["t"]) > 2):
        return "BLOCKED (control vs unconditional gap > 2 SE)"
    return "EDGE"


def fetch_daily():
    """Daily SIP closes 2026-01-15..11-19 for every name that ever fired (the sector map needs 60 sessions before each
    fire day; the warehouse starts 2026-04-27). -> OUT/name_daily.pkl {sym: {day: close}}. After 16:05 ET."""
    import arm_then_fire as A
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment
    now = datetime.now(ET)
    if now.weekday() < 5 and "0900" <= now.strftime("%H%M") < "1605":
        sys.exit("market hours - rerun after 16:05 ET")
    res = pickle.load(open(A.OUT / "fires.pkl", "rb"))
    names = sorted({f["sym"] for D in res["days"].values() for f in D["fires"]})
    k = json.load(open(ROOT / "secrets_data.json"))
    cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    out, failed = {}, []
    for i in range(0, len(names), 100):
        got = None
        for attempt in range(6):
            try:
                got = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=names[i:i + 100], timeframe=TimeFrame.Day,
                                                         start=datetime.fromisoformat(START).replace(tzinfo=ET),
                                                         end=datetime.fromisoformat(END + "T23:59").replace(tzinfo=ET),
                                                         feed=DataFeed.SIP, adjustment=Adjustment.RAW))
                break
            except Exception as e:                      # noqa: BLE001
                time.sleep(60 if "429" in str(e) else 5 * (attempt + 1))
        if got is None:
            failed.append(i)
            continue
        for s_, rows in (got.data or {}).items():
            out[s_] = {r.timestamp.astimezone(ET).strftime("%Y-%m-%d"): float(r.close) for r in rows}
    pickle.dump(out, open(OUT / "name_daily.pkl", "wb"))
    print("DAILY_DONE names", len(out), "failed batches", failed, flush=True)


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
    ncl = defaultdict(dict, pickle.load(open(OUT / "name_daily.pkl", "rb")))     # daily SIP closes from 2026-01-15
    ecl = {e: {d: daily_close(v[3]) for d, v in etf[e].items()} for e in SECTORS}
    cal = sorted(etf["SPY"])                                                      # trading days with SPY bars
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
        "C1": (lambda f: f["R1"], lambda f: not f["R1"]),
        "C2": (lambda f: f["SECT"] is True, lambda f: f["SECT"] is False),
    }
    sdays = sorted(res["days"])
    shuffles = []
    for seed in range(20):
        perm = list(np.random.default_rng(1000 + seed).permutation(sdays))
        shuffles.append(dict(zip(sdays, perm)))
    out["counts"]["etf_day_coverage_missing"] = [d for d in sdays if d not in etf["SPY"]]
    cover_ok = len(out["counts"]["etf_day_coverage_missing"]) <= 0.02 * len(sdays)
    canary_ok = None
    for trig in ("F1", "F2", "F3", "F4"):
        fsT = [f for f in fs if f["trig"] == trig]
        rr = [f for f in rows if f["trig"] == trig]
        for c, (on, off) in defs.items():
            lr, dr = peer_lift(rr, on, off)
            st = cell(lr, dr)
            ab = [x for x in rr if x["sec"]]
            st["on_share_h1"] = round(float(np.mean([on(x) for x in ab if x["day"] < SPLIT])), 3) if ab else None
            st["on_share_h2"] = round(float(np.mean([on(x) for x in ab if x["day"] >= SPLIT])), 3) if ab else None
            hits = 0
            for k_ in range(20):
                sh = (60 + k_ * 3) * (1 if k_ % 2 else -1)
                pl, pd = peer_lift(layer_rows(res, etf, fsT, maps, shift=sh), on, off)
                hits += cell(pl, pd)["verdict"].startswith("EDGE")
            sh_hits = 0
            for rm in shuffles:
                pl, pd = peer_lift(layer_rows(res, etf, fsT, maps, remap=rm), on, off)
                sh_hits += cell(pl, pd)["verdict"].startswith("EDGE")
            st["time_shift_placebo_rate"], st["etf_shuffle_rate"] = hits / 20, sh_hits / 20
            ct = controls(res, lr)
            st["controls"] = ct
            st["verdict"] = apply_controls(st, ct, hits / 20, sh_hits / 20)
            if c == "C1" and trig == "F2":
                pl = [dict(x, lift=x["lift"] + 15.0) for x in lr]
                canary_ok = cell(pl, dr)["verdict"].startswith("EDGE")
                out["canary_planted_C1xF2"] = canary_ok
            out["cells"][f"{c}x{trig}"] = st
        # C3 (all names, not only mapped): armed+R1 must beat BOTH armed+R1-off AND unarmed+R1-on; full 2x2 printed
        on3 = lambda f: armed(f) and f["R1"]
        comps = {"vs_armed_R1_off": lambda f: armed(f) and not f["R1"],
                 "vs_unarmed_R1_on": lambda f: f["arm"] == "UNARMED" and f["R1"],
                 "vs_unarmed_R1_off (original C3)": lambda f: f["arm"] == "UNARMED" and not f["R1"]}
        c3 = {}
        for nm, offf in comps.items():
            lr, dr = peer_lift(rr, on3, offf, mapped_only=False)
            st = cell(lr, dr)
            hits = 0
            for k_ in range(20):
                sh = (60 + k_ * 3) * (1 if k_ % 2 else -1)
                pl, pd = peer_lift(layer_rows(res, etf, fsT, maps, shift=sh), on3, offf, mapped_only=False)
                hits += cell(pl, pd)["verdict"].startswith("EDGE")
            st["time_shift_placebo_rate"] = hits / 20
            ct = controls(res, lr)
            st["controls"] = ct
            st["verdict"] = apply_controls(st, ct, hits / 20, None)
            c3[nm] = st
        # arm-shift placebo: each name's headlines moved by 1-5 sessions (20 seeds); must not read EDGE > 10%
        ashift = 0
        names = sorted({f["sym"] for f in fsT})
        for seed in range(20):
            rng = np.random.default_rng(2000 + seed)
            shift = {s_: int(rng.integers(1, 6)) for s_ in names}
            fs2 = A.assign({"days": {d: res["days"][d] for d in res["days"]}}, nw["news"], shift=shift,
                           sessions=sessions, skip_syms=failed_syms)
            r2_ = layer_rows(res, etf, [f for f in fs2 if f["trig"] == trig], maps)
            ok_ = all(cell(*peer_lift(r2_, on3, comps[k_], mapped_only=False))["verdict"].startswith("EDGE")
                      for k_ in ("vs_armed_R1_off", "vs_unarmed_R1_on"))
            ashift += ok_
        ex = [f for f in rr if not f.get("earn")]
        exc = {k_: cell(*peer_lift(ex, on3, comps[k_], mapped_only=False))["verdict"] for k_ in ("vs_armed_R1_off", "vs_unarmed_R1_on")}
        both = c3["vs_armed_R1_off"]["verdict"] == "EDGE" and c3["vs_unarmed_R1_on"]["verdict"] == "EDGE"
        if both and ashift / 20 > 0.10:
            v = "BLOCKED (arm-shift placebo)"
        elif both and not all(x.startswith("EDGE") for x in exc.values()):
            v = "earnings-day effect (not the stack)"
        elif both:
            v = "PROMISING (same-name placebo #3a not implemented - cannot be EDGE)"
        else:
            v = "UNPROVEN" if any("UNPROVEN" in c3[k_]["verdict"] for k_ in ("vs_armed_R1_off", "vs_unarmed_R1_on")) else "NO EDGE"
        out["cells"][f"C3x{trig}"] = {**c3, "arm_shift_placebo_rate": ashift / 20, "ex_earnings": exc, "verdict": v,
                                      "two_by_two_mean_g": {f"{a_}|R1={r_}": round(float(np.mean([f["g"] for f in rr if (armed(f) if a_ == "armed" else f["arm"] == "UNARMED") and f["R1"] == r_])), 2)
                                                            if any((armed(f) if a_ == "armed" else f["arm"] == "UNARMED") and f["R1"] == r_ for f in rr) else None
                                                            for a_ in ("armed", "unarmed") for r_ in (True, False)}}
        # information: R1 decomposition and the unmapped group
        out.setdefault("information", {})[trig] = {
            "spy_only_up": cell(*peer_lift(rr, lambda f: f["spy_up"] and f["sec"] and not f["sec_up"], lambda f: not f["spy_up"] and f["sec"] and not f["sec_up"]))["lift_h1"],
            "unmapped_spy_up_vs_down": cell(*peer_lift([dict(f, sec=None) for f in rr if not f["sec"]], lambda f: f["spy_up"], lambda f: not f["spy_up"], match_spy=False, mapped_only=False)),
            "opening_0945_1030_C1": cell(*peer_lift([f for f in rr if f["t"] <= 60], defs["C1"][0], defs["C1"][1]))}
    if canary_ok is False or not cover_ok:
        out["VERDICT_OVERRIDE"] = "NO VERDICT (" + ("planted canary failed" if canary_ok is False else "ETF coverage > 2% missing") + ")"
    out["VERDICT"] = out.get("VERDICT_OVERRIDE") or {k: v["verdict"] for k, v in out["cells"].items()}
    (OUT / "score.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"fetch": fetch, "fetch_daily": fetch_daily, "score": score}.get(cmd, lambda: sys.exit(__doc__))()
