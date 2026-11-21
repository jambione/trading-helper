#!/usr/bin/env python3
"""rsi2_red_htf - the operator's RSI-2 red (Pine rule) on 5- and 10-minute bars.
Spec: docs/studies/rsi2_red_htf_prereg.json (amended_1). Reuses rsi2_colour_timing's cache, Pine arithmetic, exact
trigger, approximate ratchet and stop fill.

    .venv/bin/python tools/studies/rsi2_red_htf.py fetch     # per-day SIP 1-minute history (all hours) for the chain
    .venv/bin/python tools/studies/rsi2_red_htf.py screen    # step 1, SEARCH only (CONFIRM sealed)
"""
from __future__ import annotations

import json
import math
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import rsi2_colour_timing as R  # noqa: E402

ET = ZoneInfo("America/New_York")
HIST = R.OUT / "htf_hist"
PREREG = "docs/studies/rsi2_red_htf_prereg.json (amended_1)"
TFS = (5, 10)
RULES = ("S", "C", "P2")
T_LO, T_HI = 15, 360                       # entry minute 09:45 .. 15:30


# ---------------------------------------------------------------- bars (pure)

def buckets(points, tf):
    """points: [(et_datetime_minute, close)] in time order (any hours). -> list of (bucket_key, close) with key =
    (date, minute_of_day // tf), close = the bucket's LAST 1-minute close; buckets with no minute do not exist. Pure."""
    out = []
    for t, c in points:
        if not np.isfinite(c):
            continue
        key = (t.date(), (t.hour * 60 + t.minute) // tf)
        if out and out[-1][0] == key:
            out[-1] = (key, c)
        else:
            out.append((key, c))
    return out


def htf_state(closes):
    """Pine red and the trigger state on HTF closes. -> dict of arrays. Pure."""
    x = np.asarray(closes, float)
    rsi, U, D = R.pine_rsi(x)
    s200, s5, m4, m199 = R.sma(x, 200), R.sma(x, 5), R.sma(x, 4), R.sma(x, 199)
    with np.errstate(invalid="ignore"):
        red = (rsi > 90) & (x < s200) & (x > s5)
    return {"c": x, "rsi": rsi, "U": U, "D": D, "m4": m4, "m199": m199, "red": np.nan_to_num(red, nan=0).astype(bool)}


def adjacent(k1, k2):
    return k1[0] == k2[0] and k2[1] == k1[1] + 1


def rth_minute(day_date, key, tf):
    """RTH minute index of the CLOCK START of bucket key on day_date, or None if not that day. Pure."""
    if key[0] != day_date:
        return None
    return key[1] * tf - (9 * 60 + 30)


# ---------------------------------------------------------------- signals + trades (pure)

def name_signals(day, hist_pts, o, h, l, c, pc, tf):
    """Signals for one name-day on timeframe tf. hist_pts: [(et_dt, close)] before today's 09:30 (chained history);
    today's RTH minutes come from o/h/l/c (390). -> list of dicts {rule, sig_bucket_start, ent, entry, at_open, dist,
    one_min_red_start (filled later)}. Pure."""
    dd = datetime.fromisoformat(day).date()
    pts = list(hist_pts) + [(datetime.fromisoformat(day + "T09:30").replace(tzinfo=ET) + timedelta(minutes=m), c[m])
                            for m in range(390)]
    bk = buckets(pts, tf)
    if len(bk) < 210:
        return None                                       # not enough history for SMA200 (counted by the caller)
    st = htf_state([b[1] for b in bk])
    keys = [b[0] for b in bk]
    out = []
    for j in range(2, len(bk)):
        m0 = rth_minute(dd, keys[j], tf)
        if m0 is None or m0 < 0:
            continue
        m_end = m0 + tf                                   # first minute at or after the clock end (amended_1 #3)
        prev_red = bool(st["red"][j - 1])
        # S: armed at the start of bucket j (starts at or after 09:45), previous existing bucket not red
        if m0 >= T_LO and m0 <= T_HI and not prev_red and R.passes_filter_chg(np.array([st["c"][j - 1]]), pc, 0, -1e9):
            P, below, _ = R.red_trigger_exact(st["c"][j - 1], st["U"][j - 1], st["D"][j - 1], st["m4"][j - 1], st["m199"][j - 1])
            if np.isfinite(P) and below and P > st["c"][j - 1]:
                for m in range(m0, min(m0 + tf, 390)):
                    e = R.stop_fill(P, R.px_or(o, m), R.px_or(h, m))
                    if np.isfinite(e):
                        out.append({"rule": "S", "b0": m0, "ent": m, "entry": e, "at_open": False,
                                    "dist": P / st["c"][j - 1] - 1})
                        break
        if not bool(st["red"][j]) or m_end > T_HI or m_end < T_LO or m_end >= 390:
            continue
        e = R.px_or(o, m_end)
        if not np.isfinite(e) or not (R.PX_LO <= st["c"][j] <= R.PX_HI):
            continue
        if not prev_red:
            out.append({"rule": "C", "b0": m0, "ent": m_end, "entry": e, "at_open": True, "dist": None})
        elif adjacent(keys[j - 1], keys[j]) and not (bool(st["red"][j - 2]) and adjacent(keys[j - 2], keys[j - 1])):
            out.append({"rule": "P2", "b0": m0, "ent": m_end, "entry": e, "at_open": True, "dist": None})
    return out


def outcomes(o, h, l, c, ent, e, at_open, tf):
    """1 and 3 HTF bars of minutes from the entry minute, and the approximate ratchet. -> (h1, h3, ratchet, exit). Pure."""
    rx, xb = R.ratchet_exit(o, h, l, c, ent, e, at_open)
    return ((R.px_or(c, ent + tf - 1) / e - 1) * 1e4, (R.px_or(c, ent + 3 * tf - 1) / e - 1) * 1e4,
            (rx / e - 1) * 1e4, xb)


def peer_trade(rule, o, h, l, c, sig, tf, pc_prev_close):
    """Peer entry of the same type: S = same-distance stop armed at the bucket start above the peer's last close before
    it, first crossing inside the bucket, exits from its own fill; C/P2 = the same open minute. -> outcomes or None."""
    if rule == "S":
        cp = pc_prev_close
        if not np.isfinite(cp):
            return None
        P = math.ceil(round(cp * (1 + sig["dist"]) * 100, 6)) / 100.0
        for m in range(sig["b0"], min(sig["b0"] + tf, 390)):
            e = R.stop_fill(P, R.px_or(o, m), R.px_or(h, m))
            if np.isfinite(e):
                return outcomes(o, h, l, c, m, e, False, tf)
        return None
    e = R.px_or(o, sig["ent"])
    return outcomes(o, h, l, c, sig["ent"], e, True, tf) if np.isfinite(e) else None


def last_close_before(c, m):
    for i in range(m - 1, -1, -1):
        if np.isfinite(c[i]):
            return float(c[i])
    return np.nan


EX = ("h1", "h3", "ratchet")


def day_task(args):
    """One SEARCH day: every name's signals on both timeframes with peers and controls. Pure given the files."""
    path, hpath = args
    z = np.load(path)
    d = {k: z[k] for k in z.files}
    syms = [str(x) for x in z["syms"]]
    day = Path(path).stem
    hist = pickle.load(open(hpath, "rb")) if Path(hpath).exists() else {}
    U_ = len(syms)
    rows, cnt = [], defaultdict(int)
    red1 = d["red_pine"]
    for tf in TFS:
        for k, s in enumerate(syms):
            hp = hist.get(s)
            if not hp:
                cnt[f"tf{tf}_no_history"] += 1
                continue
            pts = [(datetime.fromtimestamp(ts, ET).replace(second=0, microsecond=0), cl) for ts, cl in hp]
            o, h, l, c = d["o"][k], d["h"][k], d["l"][k], d["c"][k]
            sigs = name_signals(day, pts, o, h, l, c, d["pc"][k], tf)
            if sigs is None:
                cnt[f"tf{tf}_short_history"] += 1
                continue
            free = {r: -1 for r in RULES}
            for sg in sigs:
                rule = sg["rule"]
                cnt[f"tf{tf}_{rule}_raw"] += 1
                if sg["ent"] < free[rule]:
                    cnt[f"tf{tf}_{rule}_refractory"] += 1
                    continue
                f = outcomes(o, h, l, c, sg["ent"], sg["entry"], sg["at_open"], tf)
                free[rule] = max(f[3] + 1, sg["ent"] + tf)
                r = {"tf": tf, "rule": rule, "day": day, "sym": s, "t": sg["ent"], **dict(zip(EX, f[:3])), "exit_bar": f[3],
                     "one_min_red_start": bool(any(red1[k][m] and (m == 0 or not red1[k][m - 1])
                                                   for m in range(max(sg["b0"], 1), min(sg["b0"] + tf, 390))))}
                order = sorted((j for j in range(U_) if j != k), key=lambda j: R.hkey("htf-peer", day, tf, syms[j], sg["ent"]))
                fbar = sg["b0"] - 1 if rule == "S" else sg["ent"] - 1
                peers = [j for j in order if R.passes_filter_chg(d["c"][j], d["pc"][j], fbar, -1e9)][:5]
                vals = []
                for j in peers:
                    v = peer_trade(rule, d["o"][j], d["h"][j], d["l"][j], d["c"][j], sg, tf, last_close_before(d["c"][j], sg["b0"]))
                    if v:
                        vals.append(v)
                r["peers_tried"], r["peers_filled"] = len(peers), len(vals)
                for i, ex in enumerate(EX):
                    xs = [v[i] for v in vals if np.isfinite(v[i])]
                    r[f"peer_{ex}"] = float(np.mean(xs)) if xs else np.nan
                    r[f"lift_{ex}"] = r[ex] - r[f"peer_{ex}"] if xs and np.isfinite(r[ex]) else np.nan
                rng = np.random.default_rng(R.hkey("htf-unc", day, tf, s, sg["ent"], rule) % 2 ** 32)
                for ex in EX:
                    r[f"unc_{ex}"] = np.nan
                for _ in range(20):
                    j, b0 = int(rng.integers(0, U_)), int(rng.integers(T_LO, T_HI + 1 - tf))
                    b0 -= b0 % tf                       # clock-aligned (09:30 is on both grids)
                    if R.passes_filter_chg(d["c"][j], d["pc"][j], b0 - 1, -1e9):
                        sg2 = {**sg, "b0": b0, "ent": b0 + (tf if rule != "S" else 0)}
                        v = peer_trade(rule, d["o"][j], d["h"][j], d["l"][j], d["c"][j], sg2, tf, last_close_before(d["c"][j], b0))
                        if v:
                            for i, ex in enumerate(EX):
                                r[f"unc_{ex}"] = v[i]
                        break
                x0 = f[3] + 2 + R.hkey("htf-later", day, tf, s, sg["ent"]) % 61
                v = outcomes(o, h, l, c, x0, R.px_or(o, x0), True, tf) if x0 <= T_HI and np.isfinite(R.px_or(o, x0)) else None
                for i, ex in enumerate(EX):
                    r[f"later_{ex}"] = v[i] if v else np.nan
                    r[f"lift_later_{ex}"] = r[ex] - v[i] if v and np.isfinite(v[i]) and np.isfinite(r[ex]) else np.nan
                rows.append(r)
    return rows, dict(cnt), day


# ---------------------------------------------------------------- I/O

def fetch():
    """amended_1 #4: per SEARCH day, SIP 1-minute bars (all hours) for that day's names from 04:00 two sessions back
    through 09:29 that day. -> HIST/DAY.pkl {sym: [(epoch, close)]}. After 16:05 ET only. Resumable."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment
    from evening_family import WH_SIP
    now = datetime.now(ET)
    if now.weekday() < 5 and "0900" <= now.strftime("%H%M") < "1605":
        sys.exit("market hours - rerun after 16:05 ET")
    k = json.load(open(ROOT / "secrets_data.json"))
    cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    HIST.mkdir(parents=True, exist_ok=True)
    cal = sorted(p.stem for p in WH_SIP.glob("20??-??-??.npz"))
    days = sorted(p.stem for p in R.CACHE.glob("*.npz") if R.SEARCH[0] <= p.stem <= R.SEARCH[1])
    failed = []
    for day in days:
        if (HIST / f"{day}.pkl").exists() or day not in cal or cal.index(day) < 2:
            continue
        syms = [str(x) for x in np.load(R.CACHE / f"{day}.npz")["syms"]]
        a = datetime.fromisoformat(cal[cal.index(day) - 2] + "T04:00").replace(tzinfo=ET)
        b = datetime.fromisoformat(day + "T09:29:59").replace(tzinfo=ET)
        res, ok = {}, True
        for i in range(0, len(syms), 50):
            got = None
            for attempt in range(6):
                try:
                    got = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms[i:i + 50], timeframe=TimeFrame.Minute,
                                                             start=a, end=b, feed=DataFeed.SIP, adjustment=Adjustment.RAW))
                    break
                except Exception as e:                  # noqa: BLE001
                    time.sleep(60 if "429" in str(e) else 5 * (attempt + 1))
            if got is None:
                ok = False
                break
            for s, rows in (got.data or {}).items():
                res[s] = [(r.timestamp.timestamp(), float(r.close)) for r in rows]
        if not ok:
            failed.append(day)
            continue
        pickle.dump(res, open(HIST / f"{day}.pkl", "wb"))
        print(day, len(res), sum(len(v) for v in res.values()), flush=True)
    print("FETCH_DONE failed", failed, flush=True)


def stat(rows, key):
    return R._stat(rows, key)


def screen():
    from multiprocessing import Pool
    paths = [(str(p), str(HIST / f"{p.stem}.pkl")) for p in sorted(R.CACHE.glob("*.npz")) if R.SEARCH[0] <= p.stem <= R.SEARCH[1]]
    rows, cnt, nohist = [], defaultdict(int), []
    with Pool(int(os.environ.get("RED_WORKERS", "6"))) as pool:
        for rr, cc, day in pool.imap(day_task, paths):
            if not (HIST / f"{day}.pkl").exists():
                nohist.append(day)
            rows += rr
            for k, v in cc.items():
                cnt[k] += v
    counts = {"sessions": len(paths), "sessions_without_history_file": nohist, **dict(cnt),
              "signals": {f"tf{tf}_{rule}": {h: sum(1 for r in x if r["tf"] == tf and r["rule"] == rule)
                                             for h, x in zip(("h1", "h2"), R.halves(rows))} for tf in TFS for rule in RULES}}
    print("COUNTS", json.dumps(counts, default=str), flush=True)
    out = {"prereg": PREREG, "counts": counts}
    cells = []
    for tf in TFS:
        for rule in RULES:
            rr = [r for r in rows if r["tf"] == tf and r["rule"] == rule]
            rep = {"n": len(rr), "one_min_red_start_share": R.r2(float(np.mean([r["one_min_red_start"] for r in rr]))) if rr else None,
                   "peer_fill_rate": R.r2(sum(r["peers_filled"] for r in rr) / max(sum(r["peers_tried"] for r in rr), 1))}
            best = None
            for ex in EX:
                st = stat(rr, ex)
                st["gross_pooled"] = R.r2(R.mean(rr, ex))
                st["lift_pooled"] = R.r2(R.mean([r for r in rr if np.isfinite(r.get(f"lift_{ex}", np.nan))], f"lift_{ex}"))
                st["peer_mean"], st["unconditional_mean"] = R.r2(R.mean(rr, f"peer_{ex}")), R.r2(R.mean(rr, f"unc_{ex}"))
                pu = R._paired_day_t(rr, f"peer_{ex}", rr, f"unc_{ex}")
                st["peer_vs_unconditional_day"] = pu
                st["mismatch_block"] = bool(pu["diff"] is not None and pu["t"] is not None and abs(pu["diff"]) > 3 and abs(pu["t"]) > 2)
                lat = [r for r in rr if np.isfinite(r.get(f"lift_later_{ex}", np.nan))]
                st["same_name_later"] = {"n": len(lat), "lift": R.r2(R.mean(lat, f"lift_later_{ex}")), "t": R.r2(R.day_t(lat, f"lift_later_{ex}"))}
                gbar = 3.0 if ex == "h1" else 5.0
                g1 = R._gate_one(st)
                ok = g1 == "PASS" and (st["lift_pooled"] or -1) >= 3.0 and (st["gross_pooled"] or -1) >= gbar and not st["mismatch_block"]
                st["step1_gate"] = "PASS" if ok else ("UNDERPOWERED" if g1 == "UNDERPOWERED" else "FAIL")
                if ok and (best is None or (st["t"] or 0) > best[1]):
                    best = (ex, st["t"] or 0)
                rep[ex] = st
            rep["opening_0945_1030 (information)"] = stat([r for r in rr if r["t"] <= 60], "ratchet")
            if best:
                cells.append((f"tf{tf}_{rule}", best[0], best[1]))
            out[f"tf{tf}_{rule}"] = rep
    cells.sort(key=lambda x: -x[2])
    out["STEP2_ADVANCE (max 2, by gate t)"] = cells[:2]
    out["STEP2_GATE"] = "PASS - step 2 for the advanced cells" if cells else "STOP - no HTF cell passes the step-1 gate on SEARCH"
    R.OUT.mkdir(parents=True, exist_ok=True)
    (R.OUT / "htf_screen.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"fetch": fetch, "screen": screen}.get(cmd, lambda: sys.exit(__doc__))()
