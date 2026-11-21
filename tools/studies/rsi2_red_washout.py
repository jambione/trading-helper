#!/usr/bin/env python3
"""rsi2_red_washout - RSI-2 red early off a fresh low (A), and confirmed by the %R oversold-exit triangle (B1).
Spec: docs/studies/rsi2_red_washout_prereg.json. Reuses rsi2_colour_timing's cache, trigger, ratchet and peers.

    .venv/bin/python tools/studies/rsi2_red_washout.py examples   # code check on 11/16-18 (signals only)
    .venv/bin/python tools/studies/rsi2_red_washout.py screen     # step 1, SEARCH only (CONFIRM counts only)
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import rsi2_colour_timing as R  # noqa: E402
from early_entry_honest import os_leave_flag  # noqa: E402

PREREG = "docs/studies/rsi2_red_washout_prereg.json"
WARM = 112
HOLDS = (1, 3, 5)
EX = ("m1", "m3", "m5", "ratchet")
EXCLUDED = ("2026-11-16", "2026-11-17", "2026-11-18")


# ---------------------------------------------------------------- pure pieces

def low_bar(l, t, look=15):
    """Bar index of the lowest LOW among completed bars t-look .. t-1 (NaN skipped), or None. Pure."""
    lo = max(0, t - look)
    seg = l[lo:t]
    if not np.isfinite(seg).any():
        return None
    return lo + int(np.nanargmin(seg))


def early(l, t):
    """Rule A's 'within 2 bars of the 15-minute low': the low is at t-1 or t-2. Pure."""
    b = low_bar(l, t)
    return b is not None and t - b in (1, 2)


ETH_OHLC = R.OUT / "eth_ohlc"


def fetch():
    """amended_1 #4: SIP extended-hours OHLC (prior 16:00 .. today 09:29) per universe name, SEARCH + CONFIRM days.
    -> ETH_OHLC/DAY.pkl {sym: [(epoch, o, h, l, c)]}. After 16:05 ET. Resumable."""
    import pickle
    import time
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment
    from evening_family import WH_SIP
    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    if now.weekday() < 5 and "0900" <= now.strftime("%H%M") < "1605":
        sys.exit("market hours - rerun after 16:05 ET")
    k = json.load(open(ROOT / "secrets_data.json"))
    cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    ETH_OHLC.mkdir(parents=True, exist_ok=True)
    cal = sorted(p.stem for p in WH_SIP.glob("20??-??-??.npz"))
    days = sorted(p.stem for p in R.CACHE.glob("*.npz"))
    failed = []
    for day in days:
        if (ETH_OHLC / f"{day}.pkl").exists() or day not in cal or cal.index(day) == 0:
            continue
        syms = [str(x) for x in np.load(R.CACHE / f"{day}.npz")["syms"]]
        a_ = datetime.fromisoformat(cal[cal.index(day) - 1] + "T16:00").replace(tzinfo=et)
        b_ = datetime.fromisoformat(day + "T09:29:59").replace(tzinfo=et)
        res, ok = {}, True
        for i in range(0, len(syms), 100):
            got = None
            for attempt in range(6):
                try:
                    got = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms[i:i + 100], timeframe=TimeFrame.Minute,
                                                             start=a_, end=b_, feed=DataFeed.SIP, adjustment=Adjustment.RAW))
                    break
                except Exception as e:                  # noqa: BLE001
                    time.sleep(60 if "429" in str(e) else 5 * (attempt + 1))
            if got is None:
                ok = False
                break
            for s_, rows in (got.data or {}).items():
                res[s_] = [(r.timestamp.timestamp(), float(r.open), float(r.high), float(r.low), float(r.close)) for r in rows]
        if not ok:
            failed.append(day)
            continue
        for s_ in syms:
            res.setdefault(s_, [])
        pickle.dump(res, open(ETH_OHLC / f"{day}.pkl", "wb"))
        print(day, len(res), flush=True)
    print("FETCH_DONE failed", failed, flush=True)


def wr_lines(ph, pl, pc, h, l, c, ext=None):
    """Fast %R(21, EWM 7) and slow %R(112, EWM 3) on: prior-session RTH bars + extended-hours bars (ext: [(ts,o,h,l,c)],
    amended_1 #4 PRIMARY; None = prior-RTH-only information variant) + today's 390 RTH minutes. Existing bars only for
    the history; today's minutes kept on the grid. -> (fast, slow, off) with today's minute t at off + t. Pure."""
    from bar_signal_family import wr_smoothed
    m = np.isfinite(pc) & np.isfinite(ph) & np.isfinite(pl)
    wh, wl, wc = list(ph[m][-WARM:]), list(pl[m][-WARM:]), list(pc[m][-WARM:])
    if ext:
        for _, _, eh, el, ec in ext:
            wh.append(eh); wl.append(el); wc.append(ec)
    hh, ll, cc = np.concatenate([wh, h]), np.concatenate([wl, l]), np.concatenate([wc, c])
    return wr_smoothed(hh, ll, cc, n=21, span=7), wr_smoothed(hh, ll, cc, n=112, span=3), len(wc)


def gap_to_low(l, t, look=15):
    """t minus the minute of the lowest low among minutes t-look .. t-1 (existing bars; ties -> latest). None if none."""
    lo = max(0, t - look)
    seg = np.asarray(l[lo:t], float)
    if not np.isfinite(seg).any():
        return None
    m = np.nanmin(seg)
    idx = [i for i in range(len(seg)) if np.isfinite(seg[i]) and seg[i] == m][-1]
    return t - (lo + idx)


def signals(d, k, fast, slow, off):
    """Mid-bar red fills (RS, with gap info; A = gap 1-2) and B1 on one name-day.
    -> list of dicts {rule, t, ent, entry, dist, at_open, gap, gap10, gap30}. Pure."""
    o, h, l, c = d["o"][k], d["h"][k], d["l"][k], d["c"][k]
    red, U, D, m4, m199 = d["red_pine"][k], d["U_pine"][k], d["D_pine"][k], d["m4_pine"][k], d["m199_pine"][k]
    pc = d["pc"][k]
    out = []
    for t in range(R.T_LO, R.T_HI + 1):
        if not bool(red[t - 1]) and R.passes_filter_chg(c, pc, t - 1, -1e9):
            P, below, _ = R.red_trigger_exact(R.px_or(c, t - 1), U[t - 1], D[t - 1], m4[t - 1], m199[t - 1])
            if np.isfinite(P) and below and P > c[t - 1]:
                e = R.stop_fill(P, R.px_or(o, t), R.px_or(h, t))
                if np.isfinite(e):
                    g = gap_to_low(l, t)
                    out.append({"rule": "A" if g in (1, 2) else "RS_other", "t": t, "ent": t, "entry": e,
                                "dist": P / c[t - 1] - 1, "at_open": False, "gap": g,
                                "gap10": gap_to_low(l, t, 10), "gap30": gap_to_low(l, t, 30)})
        if np.isfinite(c[t]) and bool(red[t]) and not bool(red[t - 1]):
            g = gap_to_low(l, t)
            if g is not None and 1 <= g <= 4:
                for u in range(t, min(t + 3, 389)):
                    if os_leave_flag(fast, slow, off + u):
                        e = R.px_or(o, u + 1)
                        if np.isfinite(e) and R.passes_filter_chg(c, pc, u, -1e9) and u + 1 <= R.T_HI:
                            out.append({"rule": "B1", "t": t, "ent": u + 1, "entry": e, "dist": None, "at_open": True,
                                        "gap": g, "gap10": None, "gap30": None})
                        break
    return out


def outcomes(o, h, l, c, ent, e, at_open):
    rx, xb = R.ratchet_exit(o, h, l, c, ent, e, at_open)
    return [(R.px_or(c, ent + n - 1) / e - 1) * 1e4 for n in HOLDS] + [(rx / e - 1) * 1e4], xb


def peer_trade(kind, d, j, t, ent, dist):
    """Same entry type on name j: 'stop' = same-distance buy-stop above close[t-1] filled inside minute t; 'open' =
    buy open[ent]. -> outcome list or None. Pure."""
    o, h, l, c = d["o"][j], d["h"][j], d["l"][j], d["c"][j]
    if kind == "stop":
        cp = R.px_or(c, t - 1)
        if not np.isfinite(cp):
            return None
        P = np.ceil(round(cp * (1 + dist) * 100, 6)) / 100.0
        e = R.stop_fill(P, R.px_or(o, t), R.px_or(h, t))
        return outcomes(o, h, l, c, t, e, False)[0] if np.isfinite(e) else None
    e = R.px_or(o, ent)
    return outcomes(o, h, l, c, ent, e, True)[0] if np.isfinite(e) else None


def _mean_of(vals, i):
    xs = [v[i] for v in vals if v and np.isfinite(v[i])]
    return float(np.mean(xs)) if xs else np.nan


def day_task(path, outcomes_on=True, ext_variant=True):
    import pickle
    from relative_strength_scan import load_day
    from evening_family import WH_SIP
    z = np.load(path)
    d = {k: z[k] for k in z.files}
    d["syms"] = [str(x) for x in z["syms"]]
    day = Path(path).stem
    d["day"] = day
    cal = sorted(p.stem for p in WH_SIP.glob("20??-??-??.npz"))
    cnt = defaultdict(int)
    if day not in cal or cal.index(day) == 0:
        cnt["no_prior_session"] += 1
        return [], dict(cnt), day
    ef = ETH_OHLC / f"{day}.pkl"
    eth = pickle.load(open(ef, "rb")) if ef.exists() else None
    if eth is None and ext_variant:
        cnt["no_eth_file_day_dropped"] += 1             # review fix 1: never mix prior-RTH %R into the primary
        return [], dict(cnt), day
    pd_ = load_day(WH_SIP / f"{cal[cal.index(day) - 1]}.npz")
    U_ = len(d["syms"])
    gaps = [None] * U_
    sigs_all = {}
    for k, s in enumerate(d["syms"]):
        j = pd_["idx"].get(s)
        if j is None:
            cnt["no_prior_bars"] += 1
            continue
        ext = (eth or {}).get(s) if ext_variant else None
        fast, slow, off = wr_lines(pd_["h"][j], pd_["l"][j], pd_["c"][j], d["h"][k], d["l"][k], d["c"][k], ext)
        sigs_all[k] = signals(d, k, fast, slow, off)
        gaps[k] = [gap_to_low(d["l"][k], t) for t in range(390)]
        red = d["red_pine"][k]
        cnt["red_starts"] += int(sum(1 for t in range(R.T_LO, R.T_HI + 1) if red[t] and not red[t - 1] and np.isfinite(d["c"][k][t])))
        last_ent = defaultdict(lambda: -10 ** 9)
        for sg in sigs_all[k]:
            cnt[f"{sg['rule']}_raw"] += 1
            if sg["ent"] >= last_ent[sg["rule"]] + 15:
                cnt[f"{sg['rule']}_spaced15"] += 1
                last_ent[sg["rule"]] = sg["ent"]
            if sg["rule"] == "A" and any(os_leave_flag(fast, slow, off + u) for u in range(sg["t"], min(sg["t"] + 3, 390))):
                cnt["A_with_triangle_within_2"] += 1
            if sg["rule"] == "B1" and sg["ent"] <= 112:
                cnt["B1_before_1122"] += 1
    rows = []
    if not outcomes_on:
        return rows, dict(cnt), day
    for k, sigs in sigs_all.items():
        s = d["syms"][k]
        free = defaultdict(lambda: -1)
        o, h, l, c = d["o"][k], d["h"][k], d["l"][k], d["c"][k]
        for sg in sigs:
            rule, t, ent, e = sg["rule"], sg["t"], sg["ent"], sg["entry"]
            if ent < free[rule]:
                cnt[f"{rule}_refractory"] += 1
                continue
            vals, xb = outcomes(o, h, l, c, ent, e, sg["at_open"])
            free[rule] = max(xb + 1, ent + 15)
            for ex_, v_ in zip(EX, vals):
                if not np.isfinite(v_):
                    cnt[f"{rule}_nan_{ex_}"] += 1
            r = {"rule": rule, "day": day, "sym": s, "t": t, "ent": ent, "gap": sg["gap"], "gap10": sg["gap10"],
                 "gap30": sg["gap30"], "dist": sg["dist"], **dict(zip(EX, vals)), "exit_bar": xb}
            kind = "open" if sg["at_open"] else "stop"
            order = sorted((jj for jj in range(U_) if jj != k), key=lambda jj: R.hkey("wash-peer", day, d["syms"][jj], ent))
            fbar = t - 1 if kind == "stop" else ent - 1
            peers = [jj for jj in order if R.passes_filter_chg(d["c"][jj], d["pc"][jj], fbar, -1e9)][:5]
            pv = [peer_trade(kind, d, jj, t, ent, sg["dist"]) for jj in peers]
            r["n_peer"] = sum(1 for v in pv if v)
            for i, ex in enumerate(EX):
                m = _mean_of(pv, i)
                r[f"peer_{ex}"] = m
                r[f"lift_{ex}"] = r[ex] - m if np.isfinite(m) and np.isfinite(r[ex]) else np.nan
            if rule == "A":          # information control: fresh low (gap 1-2) but no red trigger, same-distance stop
                def armed_red(jj):
                    P_, b_, _ = R.red_trigger_exact(R.px_or(d["c"][jj], t - 1), d["U_pine"][jj][t - 1], d["D_pine"][jj][t - 1],
                                                    d["m4_pine"][jj][t - 1], d["m199_pine"][jj][t - 1])
                    return np.isfinite(P_) and b_ and P_ > d["c"][jj][t - 1] and not bool(d["red_pine"][jj][t - 1])
                fl = [jj for jj in order if gaps[jj] is not None and gaps[jj][t] in (1, 2) and not bool(d["red_pine"][jj][t - 1])
                      and not armed_red(jj) and R.passes_filter_chg(d["c"][jj], d["pc"][jj], t - 1, -1e9)][:5]
                fv = [peer_trade("stop", d, jj, t, ent, sg["dist"]) for jj in fl]
                for i, ex in enumerate(EX):
                    m = _mean_of(fv, i)
                    r[f"lift_freshlow_{ex}"] = r[ex] - m if np.isfinite(m) and np.isfinite(r[ex]) else np.nan
            rng = np.random.default_rng(R.hkey("wash-unc", day, s, ent, rule) % 2 ** 32)
            for ex in EX:
                r[f"unc_{ex}"] = np.nan
            for _ in range(20):
                jj, tt = int(rng.integers(0, U_)), int(rng.integers(R.T_LO, R.T_HI + 1))
                if R.passes_filter_chg(d["c"][jj], d["pc"][jj], tt if kind == "open" else tt - 1, -1e9):
                    v = peer_trade(kind, d, jj, tt, tt + (1 if kind == "open" else 0), sg["dist"])
                    if v:
                        for i, ex in enumerate(EX):
                            r[f"unc_{ex}"] = v[i]
                    break
            x0 = xb + 2 + R.hkey("wash-later", day, s, ent) % 61
            v = outcomes(o, h, l, c, x0, R.px_or(o, x0), True)[0] if x0 <= R.T_HI and np.isfinite(R.px_or(o, x0)) else None
            for i, ex in enumerate(EX):
                r[f"lift_later_{ex}"] = r[ex] - v[i] if v and np.isfinite(v[i]) and np.isfinite(r[ex]) else np.nan
            rows.append(r)
    return rows, dict(cnt), day


def _counts_only(path):
    return day_task(path, outcomes_on=False)


def _prior_rth_variant(path):
    return day_task(path, ext_variant=False)


# ---------------------------------------------------------------- commands

def examples():
    import pickle
    hm = lambda i: f"{9 + (30 + i) // 60:02d}:{(30 + i) % 60:02d}"
    from relative_strength_scan import load_day
    from evening_family import WH_SIP
    cal = sorted(p.stem for p in WH_SIP.glob("20??-??-??.npz"))
    for day, sym, note in (("2026-11-17", "BE", "A/B1 ~10:55; NOT A at 10:23 (gap 5)"),
                           ("2026-11-18", "WDC", "B1 ~11:58 (gap ~3, triangle ~12:00); NOT at 11:24 (gap 4, no triangle)"),
                           ("2026-11-18", "SHW", "gap 4 at 10:12, no oversold squares on the chart -> likely neither")):
        z = np.load(R.CACHE / f"{day}.npz")
        d = {k: z[k] for k in z.files}
        d["syms"] = [str(x) for x in z["syms"]]
        d["day"] = day
        k = d["syms"].index(sym)
        pd_ = load_day(WH_SIP / f"{cal[cal.index(day) - 1]}.npz")
        j = pd_["idx"][sym]
        ef = ETH_OHLC / f"{day}.pkl"
        ext = pickle.load(open(ef, "rb")).get(sym) if ef.exists() else None
        fast, slow, off = wr_lines(pd_["h"][j], pd_["l"][j], pd_["c"][j], d["h"][k], d["l"][k], d["c"][k], ext)
        sig = [x for x in signals(d, k, fast, slow, off) if x["rule"] in ("A", "B1")]
        print(f"== {sym} {day} ({note}; eth {'yes' if ext else 'no'}): " +
              ", ".join(f"{x['rule']} sig {hm(x['t'])} gap {x['gap']} entry {hm(x['ent'])} @ {x['entry']:.2f}" for x in sig))


def _complement(rows, ex):
    """A minus non-A mid-bar red fills on the same day and trigger-distance bucket (day-clustered). Pure."""
    def bucket(x):
        dd = x["dist"]
        return None if dd is None else (0 if dd < 5e-4 else (1 if dd < 2e-3 else (2 if dd < 5e-3 else 3)))
    per = defaultdict(lambda: ([], []))
    for r in rows:
        if r["rule"] in ("A", "RS_other") and np.isfinite(r[ex]):
            per[(r["day"], bucket(r))][0 if r["rule"] == "A" else 1].append(r[ex])
    byday = defaultdict(list)
    for (day, _), (a, b) in per.items():
        if a and b:
            byday[day].append(np.mean(a) - np.mean(b))
    dm = [float(np.mean(v)) for v in byday.values()]
    if len(dm) < 3:
        return {"days": len(dm)}
    return {"days": len(dm), "diff": R.r2(float(np.mean(dm))), "t": R.r2(float(np.mean(dm) / (np.std(dm, ddof=1) / np.sqrt(len(dm)))))}


def screen():
    from multiprocessing import Pool
    sp = [str(p) for p in sorted(R.CACHE.glob("*.npz")) if R.SEARCH[0] <= p.stem <= R.SEARCH[1]]
    cp = [str(p) for p in sorted(R.CACHE.glob("*.npz")) if R.CONFIRM[0] <= p.stem <= R.CONFIRM[1] and p.stem not in EXCLUDED]
    rows, cnt, ccnt = [], {"h1": defaultdict(int), "h2": defaultdict(int)}, defaultdict(int)
    with Pool(int(os.environ.get("RED_WORKERS", "6"))) as pool:
        for rr, cc, day in pool.imap(day_task, sp):
            rows += rr
            for k, v in cc.items():
                cnt["h1" if day < R.SPLIT else "h2"][k] += v
        for _, cc, _ in pool.imap(_counts_only, cp):
            for k, v in cc.items():
                ccnt[k] += v
    counts = {"SEARCH_h1": dict(cnt["h1"]), "SEARCH_h2": dict(cnt["h2"]), "CONFIRM (sealed, counts only)": dict(ccnt)}
    print("COUNTS", json.dumps(counts), flush=True)
    out = {"prereg": PREREG + " (amended_1)", "counts": counts}
    any_pass = False
    for rule in ("A", "B1"):
        rr = [r for r in rows if r["rule"] == rule]
        ra, rb = R.halves(rr)
        rep = {"n": len(rr), "no_peer_h1": sum(1 for r in ra if r["n_peer"] == 0), "no_peer_h2": sum(1 for r in rb if r["n_peer"] == 0)}
        for ex in EX:
            st = R._stat(rr, ex)
            st["gross_pooled"] = R.r2(R.mean(rr, ex))
            st["lift_pooled"] = R.r2(R.mean([r for r in rr if np.isfinite(r.get(f"lift_{ex}", np.nan))], f"lift_{ex}"))
            dmv = list(R.day_means(rr, f"lift_{ex}").values())
            st["lift_day_mean"] = R.r2(float(np.mean(dmv))) if dmv else None
            st["peer_mean"], st["unconditional_mean"] = R.r2(R.mean(rr, f"peer_{ex}")), R.r2(R.mean(rr, f"unc_{ex}"))
            pu = R._paired_day_t(rr, f"peer_{ex}", rr, f"unc_{ex}")
            st["peer_vs_unconditional_day"] = pu
            st["mismatch_block"] = bool(pu["diff"] is not None and pu["t"] is not None and abs(pu["diff"]) > 3 and abs(pu["t"]) > 2)
            lat = [r for r in rr if np.isfinite(r.get(f"lift_later_{ex}", np.nan))]
            st["same_name_later"] = {"n": len(lat), "lift": R.r2(R.mean(lat, f"lift_later_{ex}")), "t": R.r2(R.day_t(lat, f"lift_later_{ex}"))}
            if rule == "A":
                st["vs_complement (non-A red fills, same day + distance bucket)"] = _complement(rows, ex)
                fl = [r for r in rr if np.isfinite(r.get(f"lift_freshlow_{ex}", np.nan))]
                st["vs_fresh_low_no_red"] = {"n": len(fl), "lift": R.r2(R.mean(fl, f"lift_freshlow_{ex}")), "t": R.r2(R.day_t(fl, f"lift_freshlow_{ex}"))}
            if ex in ("m3", "m5", "ratchet"):
                gbar = 5.0 if ex == "ratchet" else 3.0
                g1 = R._gate_one(st)
                ok = g1 == "PASS" and (st["lift_pooled"] or -1) >= 3.0 and (st["gross_pooled"] or -1) >= gbar and not st["mismatch_block"]
                st["step1_gate"] = "PASS" if ok else ("UNDERPOWERED" if g1 == "UNDERPOWERED" else "FAIL")
                any_pass |= ok
            rep[ex] = st
        out[rule] = rep
    rs = [r for r in rows if r["rule"] in ("A", "RS_other")]
    out["information: gap lines (ratchet / m3 lift vs peers)"] = {
        f"{w}min_gap_{g}": {"n": len(x), "ratchet": R.r2(R.mean(x, "lift_ratchet")), "m3": R.r2(R.mean(x, "lift_m3"))}
        for w, key in ((15, "gap"), (10, "gap10"), (30, "gap30")) for g in (1, 2, 3, 4)
        for x in [[r for r in rs if r[key] == g]]}
    with Pool(int(os.environ.get("RED_WORKERS", "6"))) as pool:     # information: prior-RTH-only %R warm-up variant
        alt = []
        for rr2, _, _ in pool.imap(_prior_rth_variant, sp):
            alt += rr2
    out["information: prior-RTH %R variant (B1)"] = {ex: {"n": len(x), "lift_h1": R.r2(R.mean(R.halves(x)[0], f"lift_{ex}")),
                                                          "lift_h2": R.r2(R.mean(R.halves(x)[1], f"lift_{ex}")), "t": R.r2(R.day_t(x, f"lift_{ex}"))}
                                                     for ex in EX for x in [[r for r in alt if r["rule"] == "B1"]]}
    red_starts = sum(cnt[h].get("red_starts", 0) for h in ("h1", "h2"))
    out["counts"]["A_fills_per_red_start (different denominators: fills vs closed red starts)"] = R.r2(
        sum(cnt[h].get("A_raw", 0) for h in ("h1", "h2")) / max(red_starts, 1))
    out["notes"] = {"same_name_later": "buys at the open even for A's stop entries (information)",
                    "lift_pooled": "trade-level mean; lift_day_mean and t are day-clustered"}
    out["STEP2_GATE"] = "PASS - step 2 (live-exit replay, CONFIRM unsealed)" if any_pass else "STOP - neither rule passes the step-1 gate on SEARCH"
    R.OUT.mkdir(parents=True, exist_ok=True)
    (R.OUT / "washout_screen.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    {"fetch": fetch, "examples": examples, "screen": screen}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
