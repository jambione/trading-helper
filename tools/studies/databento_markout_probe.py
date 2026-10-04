#!/usr/bin/env python3
"""databento_markout_probe.py: stage 2 depth test, run exactly per docs/studies/depth_book_prereg.json (62fcbbc).

Does the Nasdaq top-5 book imbalance (OBI5, demeaned within name-day) at a random entry minute predict the
120 s SIP outcome (buy at ask at +0.5 s, sell at bid at +0.5 s + H) well enough to beat the spread?

Phases (WORK dir default /tmp/dbn/run):
  plan      build the universe + 3 random entries/name-day (seed 17)               [any python]
  fetch     Databento XNAS.ITCH mbp-10 [signal-35 s, signal] per entry + status     [venv with databento]
            every request is priced with metadata.get_cost first and logged to spend_log.csv;
            hard cap SPEND_CAP_USD (default 10) on the cumulative quoted cost. --pipe = 1 name, 1 day.
  features  OBI5 / OBI5_dm / OBI1 / ASK_THIN30 + drops, also at the 1 s guard      [venv with databento]
  outcomes  SIP NBBO via exec_report.nbbo_at (Alpaca) at +0.5 s and +0.5 s + H      [desk venv]
  score     power check, H1 verdict, info tables -> result.json / result.md          [desk venv, numpy]

The Databento key is read from config/secrets.json.databento and is never printed.
Usage-priced historical requests only: no batch jobs, no live, no subscriptions.
"""
from __future__ import annotations

import csv
import json
import math
import os
import pickle
import random
import statistics
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
REPO = os.environ.get("REPO") or os.path.expanduser("~/repo/trading-helper")
WORK = os.environ.get("WORK") or "/tmp/dbn/run"
DATA = os.path.join(WORK, "mbp10")
LO, HI = "2026-06-01", "2026-08-28"
NAMES, PER_NAME, SEED = 40, 3, 17
WIN_S, STALE_S, LAT_S = 35, 30, 0.5
HS = (60, 120, 300)
CAP = float(os.environ.get("SPEND_CAP_USD", "10"))
DS = "XNAS.ITCH"
NS = 1_000_000_000


def P(*a):
    print(*a, flush=True)


def jload(p, d=None):
    return json.load(open(p)) if os.path.exists(p) else d


# ---------------------------------------------------------------- plan
def plan():
    daily = pickle.load(open(os.path.join(REPO, "ai_reports/allsym/daily_2025-09-01_2026-10-02.pkl"), "rb"))
    by = defaultdict(list)
    for sym, rows in daily.items():   # same rule as sip_breakout_study.universe
        for (d0, o0, h0, l0, c0, v0), (d, o, h, l, c, v) in zip(rows, rows[1:]):
            if LO <= d <= HI and o >= 10 and c0 * v0 >= 20e6 and c0 > 0 and o / c0 - 1 >= 0.02:
                by[d].append((o / c0 - 1, sym))
    uni = {d: [s for _, s in sorted(v, reverse=True)[:NAMES]] for d, v in sorted(by.items())}
    rng = random.Random(SEED)
    ent = []
    for d in sorted(uni):
        for s in uni[d]:
            for m in sorted(rng.sample(range(15, 360), PER_NAME)):   # 09:45..15:29 ET
                t = datetime.strptime(d, "%Y-%m-%d").replace(hour=9, minute=30, tzinfo=ET) + timedelta(minutes=m)
                ent.append({"id": f"{d}_{s}_{m:03d}", "day": d, "sym": s, "t_ns": int(t.timestamp()) * NS})
    os.makedirs(WORK, exist_ok=True)
    json.dump({"universe": uni, "entries": ent}, open(os.path.join(WORK, "plan.json"), "w"))
    P(f"plan: {len(uni)} sessions {min(uni)}..{max(uni)}, {sum(map(len, uni.values()))} name-days, {len(ent)} entries")


# ---------------------------------------------------------------- fetch
class Spend:
    def __init__(self):
        self.p = os.path.join(WORK, "spend_log.csv")
        self.lock = threading.Lock()
        self.total = 0.0
        if os.path.exists(self.p):
            for r in csv.DictReader(open(self.p)):
                self.total += float(r["quoted_usd"])
        else:
            with open(self.p, "w") as f:
                f.write("ts_et,schema,symbols,start_ns,end_ns,quoted_usd,cum_usd,file\n")

    def reserve(self, schema, syms, a, b, q, fn):
        with self.lock:
            if self.total + q > CAP:
                return False
            self.total += q
            with open(self.p, "a") as f:
                f.write(f"{datetime.now(ET).isoformat(timespec='seconds')},{schema},{'|'.join(syms)},{a},{b},{q:.8f},{self.total:.6f},{os.path.basename(fn)}\n")
            return True


def client():
    import databento as db
    return db.Historical(json.load(open(os.path.join(REPO, "config/secrets.json.databento")))["api_key"])


def _retry(f, *a, **k):
    for i in range(5):
        try:
            return f(*a, **k)
        except Exception as e:  # noqa: BLE001
            if i == 4:
                raise
            P(f"  retry {i}: {str(e)[:120]}")
            time.sleep(3 * (i + 1))


def fetch(pipe=False):
    import socket
    socket.setdefaulttimeout(90)
    pl = json.load(open(os.path.join(WORK, "plan.json")))
    ent, uni = pl["entries"], pl["universe"]
    if pipe:
        d0 = sorted(uni)[len(uni) // 2]
        s0 = uni[d0][0]
        ent = [e for e in ent if e["day"] == d0 and e["sym"] == s0]
        uni = {d0: [s0]}
        P(f"PIPE CHECK {s0} {d0}: {len(ent)} windows")
    os.makedirs(DATA, exist_ok=True)
    c, sp = client(), Spend()
    P(f"spend so far (quoted): ${sp.total:.4f}; cap ${CAP:.2f}")
    stop = threading.Event()

    def get(schema, syms, a_ns, b_ns, fn):
        if os.path.exists(fn) or stop.is_set():
            return 0.0
        kw = dict(dataset=DS, symbols=syms, schema=schema, start=a_ns, end=b_ns)
        q = _retry(c.metadata.get_cost, **kw)
        for attempt in range(4):   # every attempt is reserved + logged (a retried download may bill again)
            if not sp.reserve(schema, syms, a_ns, b_ns, q, fn + (f"#retry{attempt}" if attempt else "")):
                stop.set()
                P(f"CAP: next request ${q:.4f} would exceed ${CAP:.2f} (spent ${sp.total:.4f}); stopping")
                return 0.0
            try:
                c.timeseries.get_range(path=fn + ".part", **kw)
                os.replace(fn + ".part", fn)
                return q
            except Exception as e:  # noqa: BLE001
                P(f"  get_range fail {os.path.basename(fn)} try {attempt}: {str(e)[:100]}")
                time.sleep(5 * (attempt + 1))
        P(f"  GAVE UP {os.path.basename(fn)}")
        return q

    # halt status for each day's names (08:00-16:00 ET), one request per day
    for d, syms in sorted(uni.items()):
        a = int(datetime.strptime(d, "%Y-%m-%d").replace(hour=8, tzinfo=ET).timestamp()) * NS
        get("status", syms, a, a + 8 * 3600 * NS, os.path.join(DATA, f"status_{d}.dbn.zst"))

    def one(e):
        return get("mbp-10", [e["sym"]], e["t_ns"] - WIN_S * NS, e["t_ns"] + 1, os.path.join(DATA, e["id"] + ".dbn.zst"))

    n = 0
    with ThreadPoolExecutor(16) as ex:
        for _ in ex.map(one, ent):
            n += 1
            if n % 250 == 0:
                P(f"  {n}/{len(ent)} windows, quoted spend ${sp.total:.4f}")
    P(f"fetch done{' (STOPPED AT CAP)' if stop.is_set() else ''}: quoted spend ${sp.total:.4f}")


# ---------------------------------------------------------------- features
def _halts(day):
    import databento as db
    fn = os.path.join(DATA, f"status_{day}.dbn.zst")
    out = defaultdict(list)
    if not os.path.exists(fn):
        return None
    df = db.DBNStore.from_file(fn).to_df(pretty_ts=False, map_symbols=True)
    if df.empty:
        return out
    df = df.reset_index()
    for sym, g in df.groupby("symbol"):
        start = None
        for _, r in g.sort_values("ts_recv").iterrows():
            act = int(r["action"])
            trading = str(r.get("is_trading", "")).strip()
            halted = act in (8, 9, 10) or trading == "N"   # HALT, PAUSE, SUSPEND
            resumed = act == 7 or trading == "Y"            # TRADING
            if halted and start is None:
                start = int(r["ts_recv"])
            elif resumed and start is not None:
                out[sym].append((start, int(r["ts_recv"])))
                start = None
        if start is not None:
            out[sym].append((start, 10**20))
    return out


def _book(df, S):
    """features of the book at time S (ns) from mbp-10 rows (ts_recv index, ns ints)."""
    ts = df["ts_recv"].values
    idx = (ts <= S).nonzero()[0]
    if len(idx) == 0:
        return None, "no_book"
    i = idx[-1]
    if ts[i] <= S - STALE_S * NS:
        return None, "stale"
    r = df.iloc[i]
    bp = [r[f"bid_px_0{k}"] for k in range(5)]
    ap = [r[f"ask_px_0{k}"] for k in range(5)]
    bs = [float(r[f"bid_sz_0{k}"]) for k in range(5)]
    as_ = [float(r[f"ask_sz_0{k}"]) for k in range(5)]
    if any((not math.isfinite(x)) or x <= 0 for x in bp + ap) or min(bs + as_) <= 0:
        return None, "lt5"
    if bp[0] >= ap[0]:
        return None, "crossed_locked"
    obi5 = (sum(bs) - sum(as_)) / (sum(bs) + sum(as_))
    obi1 = (bs[0] - as_[0]) / (bs[0] + as_[0])
    # time-weighted ask depth (top 5) over [S-30, S]
    a0 = S - STALE_S * NS
    askd = df[[f"ask_sz_0{k}" for k in range(5)]].sum(axis=1).values.astype(float)
    j0 = (ts <= a0).nonzero()[0]
    seg_t = [a0] if len(j0) else []
    seg_v = [askd[j0[-1]]] if len(j0) else []
    for j in ((ts > a0) & (ts <= S)).nonzero()[0]:
        seg_t.append(int(ts[j]))
        seg_v.append(askd[j])
    tw = 0.0
    for k in range(len(seg_t)):
        t_end = seg_t[k + 1] if k + 1 < len(seg_t) else S
        tw += seg_v[k] * (t_end - seg_t[k])
    span = S - seg_t[0]
    thin = (sum(as_) / (tw / span) - 1) if span > 0 and tw > 0 else 0.0
    return {"obi5": obi5, "obi1": obi1, "ask_thin30": thin, "bid": float(bp[0]), "ask": float(ap[0]),
            "tw_full": bool(len(j0))}, None


def features():
    import databento as db
    pl = json.load(open(os.path.join(WORK, "plan.json")))
    halts_by_day = {}
    out, drops = {}, defaultdict(int)
    for n, e in enumerate(pl["entries"]):
        fn = os.path.join(DATA, e["id"] + ".dbn.zst")
        if not os.path.exists(fn):
            drops["not_fetched"] += 1
            continue
        if e["day"] not in halts_by_day:
            halts_by_day[e["day"]] = _halts(e["day"])
        h = halts_by_day[e["day"]]
        S = e["t_ns"]
        if h is None:
            drops["no_status_file"] += 1
            continue
        # halt overlapping the book window or the longest outcome horizon
        if any(a < S + int((LAT_S + max(HS)) * NS) and b > S - WIN_S * NS for a, b in h.get(e["sym"], [])):
            drops["halt"] += 1
            continue
        df = db.DBNStore.from_file(fn).to_df(pretty_ts=False, price_type="float").reset_index()
        if df.empty:
            drops["no_book"] += 1
            continue
        df = df.sort_values("ts_recv")
        f, why = _book(df, S)
        if f is None:
            drops[why] += 1
            continue
        g, why_g = _book(df, S - NS)
        f["guard"] = g
        f["guard_drop"] = why_g
        out[e["id"]] = {**e, **f}
        if (n + 1) % 1000 == 0:
            P(f"  features {n + 1}/{len(pl['entries'])}")
    # within name-day demeaning (survivors only)
    grp = defaultdict(list)
    for k, v in out.items():
        grp[(v["day"], v["sym"])].append(k)
    for ks in grp.values():
        m = statistics.fmean(out[k]["obi5"] for k in ks)
        gs = [out[k]["guard"]["obi5"] for k in ks if out[k]["guard"]]
        mg = statistics.fmean(gs) if gs else 0.0
        for k in ks:
            out[k]["obi5_dm"] = out[k]["obi5"] - m
            if out[k]["guard"]:
                out[k]["guard"]["obi5_dm"] = out[k]["guard"]["obi5"] - mg
    json.dump({"features": out, "drops": dict(drops)}, open(os.path.join(WORK, "features.json"), "w"))
    P(f"features: {len(out)} survivors; drops {dict(drops)}")


# ---------------------------------------------------------------- outcomes
def outcomes():
    sys.path.insert(0, os.path.join(REPO, "tools"))
    sys.path.insert(0, REPO)
    import bars  # noqa: E402
    import exec_report as er  # noqa: E402
    import socket
    socket.setdefaulttimeout(30)
    cl = bars.client()
    # polite shared rate limit on the raw Alpaca quote requests (nbbo_at may issue up to 3 per lookup)
    rate = float(os.environ.get("ALPACA_RPM", "150"))
    _raw, _rl = cl.get_stock_quotes, threading.Lock()
    _next = [time.monotonic()]

    def _limited(*a, **k):
        with _rl:
            now = time.monotonic()
            wait = _next[0] - now
            _next[0] = max(now, _next[0]) + 60.0 / rate
        if wait > 0:
            time.sleep(wait)
        return _raw(*a, **k)
    cl.get_stock_quotes = _limited
    F = json.load(open(os.path.join(WORK, "features.json")))["features"]
    qp = os.path.join(WORK, "sip_quotes.json")
    qc = jload(qp, {})
    lock = threading.Lock()
    todo = []
    for k, v in F.items():
        for off in (LAT_S,) + tuple(LAT_S + h for h in HS):
            key = f"{v['sym']}|{v['t_ns'] + int(off * NS)}"
            if qc.get(key) is None:   # unseen or previously failed (429) -> (re)try
                todo.append((key, v["sym"], v["t_ns"] + int(off * NS), int(off * NS)))
    # decision-critical lookups first (entry +0.5 s and exit +120.5 s), information horizons after
    prim = {int(LAT_S * NS), int((LAT_S + 120) * NS)}
    todo.sort(key=lambda it: 0 if it[3] in prim else 1)
    todo = [it[:3] for it in todo]
    P(f"outcomes: {len(F)} entries, {len(todo)} SIP lookups to do ({len(qc)} cached)")

    def look(item):
        key, sym, tns = item
        t = datetime.fromtimestamp(tns / NS, timezone.utc)
        q = er.nbbo_at(cl, sym, t)
        for back in (10, 30):   # retries with backoff (429s have silently dropped names before)
            if q is not None:
                break
            time.sleep(back)
            q = er.nbbo_at(cl, sym, t)
        with lock:
            qc[key] = list(q) if q else None
        return q is not None

    n = 0
    with ThreadPoolExecutor(2) as ex:
        for _ in ex.map(look, todo):
            n += 1
            if n % 250 == 0:
                with lock:
                    json.dump(qc, open(qp + ".tmp", "w"))
                    os.replace(qp + ".tmp", qp)
                P(f"  {n}/{len(todo)} lookups {datetime.now(ET).strftime('%H:%M:%S')} fails={sum(v is None for v in qc.values())}")
    json.dump(qc, open(qp, "w"))
    P("outcomes done")


# ---------------------------------------------------------------- score
def _cluster(y, g, day):
    """OLS y = a + b*g with day-clustered (CR1) SE. Returns (b, se_b, a, se_a)."""
    import numpy as np
    y = np.asarray(y, float)
    X = np.column_stack([np.ones(len(y)), np.asarray(g, float)])
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    u = y - X @ beta
    meat = np.zeros((2, 2))
    days = defaultdict(list)
    for i, d in enumerate(day):
        days[d].append(i)
    for idx in days.values():
        s = X[idx].T @ u[idx]
        meat += np.outer(s, s)
    G, N = len(days), len(y)
    V = XtX_inv @ meat @ XtX_inv * (G / (G - 1)) * ((N - 1) / (N - 2))
    return float(beta[1]), float(math.sqrt(V[1, 1])), float(beta[0]), float(math.sqrt(V[0, 0]))


def _mean_t(y, day):
    """mean and day-clustered t of an intercept-only regression."""
    if len(y) < 3 or len(set(day)) < 2:
        return (statistics.fmean(y) if y else float("nan")), float("nan")
    m = statistics.fmean(y)
    sums = defaultdict(float)
    for v, d in zip(y, day):
        sums[d] += v - m
    G, N = len(sums), len(y)
    se = math.sqrt(sum(s * s for s in sums.values()) * G / (G - 1)) / N
    return m, (m / se if se > 0 else float("nan"))


def _desc(rows, key):
    y = [r[key] for r in rows if r.get(key) is not None]
    d = [r["day"] for r in rows if r.get(key) is not None]
    if not y:
        return {"n": 0}
    m, t = _mean_t(y, d)
    return {"n": len(y), "mean": round(m, 2), "median": round(statistics.median(y), 2), "t_day": round(t, 2),
            "hit": round(sum(v > 0 for v in y) / len(y), 3)}


def _wins(rows, key):
    y = [r[key] for r in rows]
    m, s = statistics.fmean(y), statistics.pstdev(y)
    return [{**r, key: min(max(r[key], m - 3 * s), m + 3 * s)} for r in rows]


def score():
    pl = json.load(open(os.path.join(WORK, "plan.json")))
    FJ = json.load(open(os.path.join(WORK, "features.json")))
    F, drops = FJ["features"], FJ["drops"]
    qc = jload(os.path.join(WORK, "sip_quotes.json"), {})
    days = sorted(pl["universe"])
    mid = len(days) // 2                         # 63 sessions: H1 = 1..31, H2 = 32..63
    half = {d: (1 if i < mid else 2) for i, d in enumerate(days)}
    rows, qfail = [], 0
    for k, v in F.items():
        q0 = qc.get(f"{v['sym']}|{v['t_ns'] + int(LAT_S * NS)}")
        qx = {h: qc.get(f"{v['sym']}|{v['t_ns'] + int((LAT_S + h) * NS)}") for h in HS}
        if not q0 or not qx[120]:
            qfail += 1
            continue
        b0, a0 = q0
        r = {"id": k, "day": v["day"], "sym": v["sym"], "half": half[v["day"]], "obi5_dm": v["obi5_dm"],
             "obi1": v["obi1"], "ask_thin30": v["ask_thin30"], "spread_bp": (a0 - b0) / ((a0 + b0) / 2) * 1e4,
             "hour": datetime.fromtimestamp(v["t_ns"] / NS, ET).hour,
             "g_obi5_dm": (v["guard"] or {}).get("obi5_dm")}
        for h in HS:
            if qx[h]:
                b1, a1 = qx[h]
                r[f"net{h}"] = (b1 / a0 - 1) * 1e4                                  # buy ask, sell bid
                r[f"mid{h}"] = (((b1 + a1) / 2) / ((b0 + a0) / 2) - 1) * 1e4       # gross vs mid
            else:
                r[f"net{h}"] = r[f"mid{h}"] = None
        rows.append(r)
    n_surv = len(F)
    fail_rate = qfail / n_surv if n_surv else 1.0
    res = {"entries_planned": len(pl["entries"]), "book_drops": drops, "book_survivors": n_surv,
           "quote_failures": qfail, "quote_fail_rate": round(fail_rate, 4), "split": f"H1 = {days[0]}..{days[mid-1]} ({mid}), H2 = {days[mid]}..{days[-1]} ({len(days)-mid})"}
    if fail_rate > 0.02:
        res["verdict"] = "ABORT: more than 2% of entries lost a SIP quote; no verdict"
        _write(res)
        return
    H1 = [r for r in rows if r["half"] == 1]
    H2 = [r for r in rows if r["half"] == 2]
    cut = sorted(r["obi5_dm"] for r in H1)[int(len(H1) * 2 / 3)]     # top tercile on H1, fixed
    g1 = [r for r in H1 if r["g_obi5_dm"] is not None]
    cut_g = sorted(r["g_obi5_dm"] for r in g1)[int(len(g1) * 2 / 3)]
    res["cut_obi5_dm_H1"] = round(cut, 5)
    res["cut_guard_H1"] = round(cut_g, 5)

    def gate(rs, key="net120", feat="obi5_dm", c=cut):
        rs = [r for r in rs if r.get(key) is not None and r.get(feat) is not None]
        g = [1 if r[feat] >= c else 0 for r in rs]
        b, se, a, _ = _cluster([r[key] for r in rs], g, [r["day"] for r in rs])
        gated = [r for r, x in zip(rs, g) if x]
        rest = [r for r, x in zip(rs, g) if not x]
        return {"gated_minus_rest_bp": round(b, 2), "se": round(se, 2), "t": round(b / se, 2) if se else None,
                "gated": _desc(gated, key), "rest": _desc(rest, key), "all": _desc(rs, key)}

    pw = gate(H1)
    res["power_check_H1"] = {"se": pw["se"], "2.84xSE": round(2.84 * pw["se"], 2), "ok": 2.84 * pw["se"] <= 5.0}
    res["H1_first_half"] = pw
    main = gate(H2)
    guard = gate(H2, feat="g_obi5_dm", c=cut_g)
    res["H1_second_half"] = main
    res["guard_second_half"] = guard
    crit = {"a_diff>=5_t>=2": main["gated_minus_rest_bp"] >= 5 and (main["t"] or 0) >= 2,
            "b_gated_net>0": main["gated"]["mean"] > 0,
            "c_gated_n>=600": main["gated"]["n"] >= 600,
            "d_guard_t>=1.5": (guard["t"] or 0) >= 1.5}
    res["criteria"] = crit
    if not res["power_check_H1"]["ok"]:
        res["verdict"] = "UNDERPOWERED: power check failed on H1; no verdict"
    else:
        res["verdict"] = "PASS" if all(crit.values()) else "FAIL"
    # information
    info = {}
    for h in HS:
        for hh, rs in (("H1", H1), ("H2", H2)):
            info[f"net{h}_{hh}"] = gate(rs, key=f"net{h}")
            info[f"mid{h}_{hh}"] = gate(rs, key=f"mid{h}")
    for hh, rs in (("H1", H1), ("H2", H2)):
        info[f"winsor3sd_net120_{hh}"] = gate(_wins([r for r in rs if r["net120"] is not None], "net120"))
    c1 = sorted(r["obi1"] for r in H1)[int(len(H1) * 2 / 3)]
    ct = sorted(r["ask_thin30"] for r in H1)[int(len(H1) / 3)]
    for hh, rs in (("H1", H1), ("H2", H2)):
        info[f"H3_obi1_top_{hh}"] = gate(rs, feat="obi1", c=c1)
        rs2 = [{**r, "neg_thin": -r["ask_thin30"]} for r in rs]
        info[f"H2_askthin_bottom_{hh}"] = gate(rs2, feat="neg_thin", c=-ct)
        info[f"hour_strata_{hh}"] = {hr: gate([r for r in rs if r["hour"] == hr])
                                     for hr in sorted({r["hour"] for r in rs}) if sum(r["hour"] == hr for r in rs) > 50}
    res["info"] = info
    res["median_spread_bp"] = round(statistics.median(r["spread_bp"] for r in rows), 2)
    _write(res)


def _write(res):
    json.dump(res, open(os.path.join(WORK, "result.json"), "w"), indent=1)
    P(json.dumps({k: res[k] for k in res if k != "info"}, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"plan": plan, "fetch": lambda: fetch(pipe="--pipe" in sys.argv), "features": features,
     "outcomes": outcomes, "score": score}.get(cmd, lambda: P(__doc__))()
