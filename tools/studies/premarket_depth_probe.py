#!/usr/bin/env python3
"""premarket_depth_probe.py: run exactly per docs/studies/premarket_depth_prereg.json (f7a0e2d).

Premarket (07:00-09:25 ET) momentum names, 2026-06-01..08-28. SIGNAL = first 1-min close above the premarket high
after qualifying; CONTROL = 3 random minutes per name-day. Priced on the Databento XNAS.ITCH book (gross = mid->mid,
net = ask->bid), holds 2/5/15 min and to 09:35; primary 5 min.

Phases (WORK default ~/repo/trading-helper/data/databento/xnas_premarket_2026-06_08, git-ignored):
  rawdaily Alpaca SIP UNADJUSTED daily bars (prior close, 20-day ADV); all bars are raw, never split-adjusted [desk venv]
  scan     Alpaca SIP 30-min bars 04:00-09:30 for the daily-panel base list -> candidates     [desk venv]
  minute   Alpaca SIP 1-min bars 04:00-09:30 for candidates                                     [desk venv]
  plan     point-in-time qualify, SIGNAL, CONTROL, windows -> plan.json                         [any]
  quote    Databento metadata.get_cost for the budget ladder -> quote.json (chosen rung)       [databento venv]
  fetch    usage-priced get_range per name-day window (+ status per day), $10 cap, resume      [databento venv]
  score    features, outcomes, power check, verdict -> result.json                              [databento venv + numpy]
The Databento key is read from config/secrets.json.databento and never printed.
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
WORK = os.environ.get("WORK") or os.path.join(REPO, "data/databento/xnas_premarket_2026-06_08")
LO, HI = "2026-06-01", "2026-08-28"
CAP = float(os.environ.get("SPEND_CAP_USD", "10"))
DS = "XNAS.ITCH"
NS = 1_000_000_000
LAT = 0.5
HOLDS = {"h2": 120, "h5": 300, "h15": 900, "t0935": None}
SEED = 23


def P(*a):
    print(*a, flush=True)


def ts_et(d, hh, mm, ss=0):
    return datetime.strptime(d, "%Y-%m-%d").replace(hour=hh, minute=mm, second=ss, tzinfo=ET)


def panel():
    return pickle.load(open(os.path.join(REPO, "ai_reports/allsym/daily_2025-09-01_2026-10-02.pkl"), "rb"))


def sessions(daily):
    return sorted({r[0] for rows in daily.values() for r in rows if LO <= r[0] <= HI})


def alpaca():
    import socket
    socket.setdefaulttimeout(60)
    sys.path.insert(0, os.path.join(REPO, "tools"))
    sys.path.insert(0, REPO)
    import bars  # noqa: E402
    cl = bars.client()
    rate, lock, nxt = float(os.environ.get("ALPACA_RPM", "150")), threading.Lock(), [time.monotonic()]
    raw = cl.get_stock_bars

    def limited(*a, **k):
        with lock:
            now = time.monotonic()
            w = nxt[0] - now
            nxt[0] = max(now, nxt[0]) + 60.0 / rate
        if w > 0:
            time.sleep(w)
        return raw(*a, **k)
    cl.get_stock_bars = limited
    return cl


def _bars(cl, syms, start, end, tf_min, day=False):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    for att in range(4):
        try:
            r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms, timeframe=(TimeFrame(1, TimeFrameUnit.Day) if day else TimeFrame(tf_min, TimeFrameUnit.Minute)),
                                                   start=start.astimezone(timezone.utc), end=end.astimezone(timezone.utc),
                                                   feed=DataFeed.SIP, adjustment="raw"))
            return {s: [(b.timestamp.timestamp(), float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))
                        for b in rows] for s, rows in (r.data or {}).items()}
        except Exception as e:  # noqa: BLE001
            P(f"  bars fail try {att}: {type(e).__name__} {str(e)[:100]}")
            time.sleep(10 * (att + 1))
    return None


# ---------------------------------------------------------------- raw daily (Alpaca, free)
def rawdaily():
    """UNADJUSTED SIP daily bars for every daily-panel symbol (split-adjusted prices/volumes leak future splits)."""
    daily, cl = panel(), alpaca()
    syms, out = sorted(daily), {}
    for i in range(0, len(syms), 200):
        got = _bars(cl, syms[i:i + 200], ts_et("2026-04-01", 0, 0), ts_et(HI, 23, 0), 1, day=True)
        if got is None:
            P(f"  rawdaily batch {i} FAILED")
            continue
        for s_, rows in got.items():
            out[s_] = [(datetime.fromtimestamp(r[0], ET).strftime("%Y-%m-%d"),) + r[1:] for r in rows]
    pickle.dump(out, open(os.path.join(WORK, "rawdaily.pkl"), "wb"))
    P(f"rawdaily: {len(out)}/{len(syms)} symbols")


def rawpanel():
    return pickle.load(open(os.path.join(WORK, "rawdaily.pkl"), "rb"))


# ---------------------------------------------------------------- scan / minute (Alpaca, free)
def scan():
    daily, cl = rawpanel(), alpaca()
    os.makedirs(os.path.join(WORK, "scan"), exist_ok=True)
    for d in sessions(panel()):
        fp = os.path.join(WORK, "scan", f"{d}.json")
        if os.path.exists(fp):
            continue
        pc = {}
        for s, rows in daily.items():
            prev = [r for r in rows if r[0] < d]
            if prev and any(r[0] == d for r in rows) and 1.5 <= prev[-1][4] <= 25:
                pc[s] = prev[-1][4]
        syms, cands, fails = sorted(pc), {}, 0
        for i in range(0, len(syms), 200):
            got = _bars(cl, syms[i:i + 200], ts_et(d, 4, 0), ts_et(d, 9, 29, 59), 30)
            if got is None:
                fails += 1
                continue
            for s, rows in got.items():
                hi, vol = max(r[2] for r in rows), sum(r[5] for r in rows)
                if hi >= 1.10 * pc[s] and vol >= 20000:
                    cands[s] = {"pc": pc[s], "pm_high": hi, "pm_vol": vol}
        json.dump({"base": len(syms), "cands": cands, "failed_batches": fails}, open(fp, "w"))
        P(f"scan {d}: base {len(syms)} cands {len(cands)} failed_batches {fails}")


def minute():
    cl = alpaca()
    os.makedirs(os.path.join(WORK, "minute"), exist_ok=True)
    for fn in sorted(os.listdir(os.path.join(WORK, "scan"))):
        d = fn[:10]
        fp = os.path.join(WORK, "minute", f"{d}.pkl")
        if os.path.exists(fp):
            continue
        c = sorted(json.load(open(os.path.join(WORK, "scan", fn)))["cands"])
        out = {}
        for i in range(0, len(c), 50):
            got = _bars(cl, c[i:i + 50], ts_et(d, 4, 0), ts_et(d, 9, 29, 59), 1)
            if got is None:
                P(f"  minute {d} batch {i} FAILED")
                continue
            out.update(got)
        pickle.dump(out, open(fp, "wb"))
        P(f"minute {d}: {len(out)}/{len(c)} names")


# ---------------------------------------------------------------- plan (point-in-time)
def plan():
    daily = rawpanel()
    days = sessions(panel())
    rng = random.Random(SEED)
    nd, fails = [], {}
    for d in days:
        sc = json.load(open(os.path.join(WORK, "scan", f"{d}.json")))
        fails[d] = sc["failed_batches"]
        mb = pickle.load(open(os.path.join(WORK, "minute", f"{d}.pkl"), "rb"))
        for s in sorted(sc["cands"]):
            pc = sc["cands"][s]["pc"]
            prev = [r[5] for r in daily.get(s, []) if r[0] < d][-20:]
            if len(prev) < 5:
                continue
            adv = statistics.fmean(prev)
            rows = sorted(mb.get(s, []))
            cum, hi_before, q, sig = 0.0, -1.0, None, None
            for (t, o, h, l, c, v) in rows:
                tl = datetime.fromtimestamp(t, ET)
                mins = tl.hour * 60 + tl.minute
                cum += v
                if q is None and 420 <= mins <= 564 and 2 <= c <= 20 and c >= 1.10 * pc and cum >= 50000 and cum >= 0.10 * adv:
                    q = int(t)
                if q is not None and sig is None and int(t) >= q and mins <= 564 and hi_before > 0 and c > hi_before:
                    sig = int(t) + 60
                hi_before = max(hi_before, h)
            if q is None:
                continue
            qd = datetime.fromtimestamp(q, ET)
            cal = list(range(qd.hour * 60 + qd.minute, 565))          # calendar minutes q..09:24
            pick = sorted(rng.sample(cal, min(3, len(cal))))
            ctrl = [int(ts_et(d, m // 60, m % 60).timestamp()) + 60 for m in pick]
            q_px = next(c for (t, o, h, l, c, v) in rows if int(t) == q)
            nd.append({"day": d, "sym": s, "q": q, "q_px": q_px, "signal": sig, "controls": ctrl,
                       "win": [q - 600, int(ts_et(d, 9, 41).timestamp())]})
    json.dump({"days": days, "namedays": nd, "scan_failed_batches": fails}, open(os.path.join(WORK, "plan.json"), "w"))
    P(f"plan: {len(nd)} qualifying name-days over {len(days)} sessions; with SIGNAL {sum(1 for x in nd if x['signal'])}; "
      f"controls {sum(len(x['controls']) for x in nd)}; $2-10 name-days {sum(1 for x in nd if x['q_px'] <= 10)}; "
      f"scan failed batches {sum(fails.values())}")


# ---------------------------------------------------------------- Databento
def dbclient():
    import databento as db
    return db.Historical(json.load(open(os.path.join(REPO, "config/secrets.json.databento")))["api_key"])


def _try(f, **k):
    for i in range(6):
        try:
            return f(**k)
        except Exception as e:  # noqa: BLE001
            if i == 5 or "symbology" in str(e) or " 4" in f" {getattr(e, 'http_status', '')}"[:2]:
                raise
            time.sleep(3 * (i + 1))


def quote():
    import socket
    socket.setdefaulttimeout(90)
    pl = json.load(open(os.path.join(WORK, "plan.json")))
    c = dbclient()

    unres = set()

    def q(schema, x):
        if f"{x['day']}_{x['sym']}" in unres:
            return 0.0
        try:
            return _try(c.metadata.get_cost, dataset=DS, symbols=[x["sym"]], schema=schema,
                        start=x["win"][0] * NS, end=x["win"][1] * NS)
        except Exception as e:  # noqa: BLE001
            if "symbology" in str(e):          # symbol not resolvable on XNAS that day: no Nasdaq book
                unres.add(f"{x['day']}_{x['sym']}")
                return 0.0
            raise
    res, chosen = {}, None
    with ThreadPoolExecutor(16) as ex:         # pre-pass: find unresolvable symbols
        list(ex.map(lambda x: q("mbp-1", x), pl["namedays"]))
    status = 0.0
    byday = defaultdict(list)
    for x in pl["namedays"]:
        byday[x["day"]].append(x["sym"])
    for d, syms in byday.items():
        syms = [y for y in syms if f"{d}_{y}" not in unres]
        if not syms:
            continue
        status += _try(c.metadata.get_cost, dataset=DS, symbols=sorted(set(syms)), schema="status",
                       start=int(ts_et(d, 4, 0).timestamp()) * NS, end=int(ts_et(d, 9, 45).timestamp()) * NS)
    res["status_usd"] = status
    for schema in ("mbp-10", "mbp-1"):
        with ThreadPoolExecutor(16) as ex:
            per = list(ex.map(lambda x: q(schema, x), pl["namedays"]))
        tot = sum(per)
        res[schema] = {"total_usd": tot, "per": {f"{x['day']}_{x['sym']}": v for x, v in zip(pl["namedays"], per)}}
        P(f"QUOTE {schema}: ${tot:.4f} for {len(per)} name-days (+ status ${status:.4f})")
        if chosen is None and tot + status <= CAP:
            chosen = {"rung": 1 if schema == "mbp-10" else 2, "schema": schema, "namedays": "all", "usd": tot + status}
            if schema == "mbp-10":
                break
    if chosen is None:
        per = res["mbp-1"]["per"]
        sub = [x for x in pl["namedays"] if x["q_px"] <= 10]
        tot = sum(per[f"{x['day']}_{x['sym']}"] for x in sub)
        P(f"QUOTE rung 3 mbp-1 $2-10 only: ${tot:.4f} for {len(sub)} name-days")
        if tot + status <= CAP:
            chosen = {"rung": 3, "schema": "mbp-1", "namedays": "q_px<=10", "usd": tot + status}
        else:
            days = pl["days"]
            for k in range(1, len(days)):
                keep = set(days[k:])
                tot = sum(per[f"{x['day']}_{x['sym']}"] for x in sub if x["day"] in keep)
                if tot + status <= CAP:
                    chosen = {"rung": 4, "schema": "mbp-1", "namedays": "q_px<=10", "first_session": days[k], "usd": tot + status}
                    break
    res["chosen"] = chosen
    res["unresolvable"] = sorted(unres)
    P(f"unresolvable on XNAS: {len(unres)} name-days")
    json.dump(res, open(os.path.join(WORK, "quote.json"), "w"))
    P(f"CHOSEN: {chosen}")


class Spend:
    def __init__(self):
        self.p, self.lock, self.total = os.path.join(WORK, "spend_log.csv"), threading.Lock(), 0.0
        if os.path.exists(self.p):
            self.total = sum(float(r["quoted_usd"]) for r in csv.DictReader(open(self.p)))
        else:
            open(self.p, "w").write("ts_et,schema,symbols,start_ns,end_ns,quoted_usd,cum_usd,file\n")

    def reserve(self, schema, syms, a, b, q, fn):
        with self.lock:
            if self.total + q > CAP:
                return False
            self.total += q
            open(self.p, "a").write(f"{datetime.now(ET).isoformat(timespec='seconds')},{schema},{'|'.join(syms)},{a},{b},"
                                    f"{q:.8f},{self.total:.6f},{os.path.basename(fn)}\n")
            return True


def selected(pl, ch):
    unres = set(json.load(open(os.path.join(WORK, "quote.json"))).get("unresolvable", []))
    nd = [x for x in pl["namedays"] if f"{x['day']}_{x['sym']}" not in unres]
    if ch["namedays"] == "q_px<=10":
        nd = [x for x in nd if x["q_px"] <= 10]
    if ch.get("first_session"):
        nd = [x for x in nd if x["day"] >= ch["first_session"]]
    return nd


def fetch(pipe=False):
    import socket
    socket.setdefaulttimeout(90)
    pl = json.load(open(os.path.join(WORK, "plan.json")))
    ch = json.load(open(os.path.join(WORK, "quote.json")))["chosen"]
    if not ch:
        P("no affordable rung; nothing fetched")
        return
    nd = selected(pl, ch)
    if pipe:
        nd = [sorted(nd, key=lambda x: (x["day"], x["sym"]))[len(nd) // 2]]
        P(f"PIPE {nd[0]['sym']} {nd[0]['day']}")
    D = os.path.join(WORK, "book")
    os.makedirs(D, exist_ok=True)
    c, sp, stop = dbclient(), Spend(), threading.Event()
    P(f"rung {ch['rung']} {ch['schema']}; spend so far ${sp.total:.4f}; cap ${CAP}")

    def get(schema, syms, a, b, fn):
        if os.path.exists(fn) or stop.is_set():
            return
        kw = dict(dataset=DS, symbols=syms, schema=schema, start=a, end=b)
        q = _try(c.metadata.get_cost, **kw)
        for att in range(4):
            if not sp.reserve(schema, syms, a, b, q, fn + (f"#retry{att}" if att else "")):
                stop.set()
                P(f"CAP: ${q:.4f} would exceed ${CAP} (spent ${sp.total:.4f}); stopping")
                return
            try:
                c.timeseries.get_range(path=fn + ".part", **kw)
                os.replace(fn + ".part", fn)
                return
            except Exception as e:  # noqa: BLE001
                P(f"  get_range fail {os.path.basename(fn)} try {att}: {str(e)[:100]}")
                time.sleep(5 * (att + 1))
        P(f"  GAVE UP {os.path.basename(fn)}")

    byday = defaultdict(set)
    for x in nd:
        byday[x["day"]].add(x["sym"])
    for d, syms in sorted(byday.items()):
        get("status", sorted(syms), int(ts_et(d, 4, 0).timestamp()) * NS, int(ts_et(d, 9, 45).timestamp()) * NS,
            os.path.join(D, f"status_{d}.dbn.zst"))
    n = 0
    with ThreadPoolExecutor(8) as ex:
        for _ in ex.map(lambda x: get(ch["schema"], [x["sym"]], x["win"][0] * NS, x["win"][1] * NS,
                                      os.path.join(D, f"{x['day']}_{x['sym']}.dbn.zst")), nd):
            n += 1
            if n % 100 == 0:
                P(f"  {n}/{len(nd)} name-days, quoted spend ${sp.total:.4f}")
    P(f"fetch done{' (STOPPED AT CAP)' if stop.is_set() else ''}: quoted spend ${sp.total:.4f}")


# ---------------------------------------------------------------- score
def _halts(fn):
    import databento as db
    out = defaultdict(list)
    df = db.DBNStore.from_file(fn).to_df(pretty_ts=False, map_symbols=True)
    if df.empty:
        return out
    df = df.reset_index().sort_values("ts_recv")
    for s, g in df.groupby("symbol"):
        st = None
        for _, r in g.iterrows():
            a = int(r["action"])
            if a in (8, 9, 10) and st is None:          # HALT / PAUSE / SUSPEND
                st = int(r["ts_recv"])
            elif a not in (8, 9, 10) and st is not None:
                out[s].append((st, int(r["ts_recv"])))
                st = None
        if st is not None:
            out[s].append((st, 10**20))
    return out


def _cl(y, g, day):
    import numpy as np
    y = np.asarray(y, float)
    X = np.column_stack([np.ones(len(y)), np.asarray(g, float)])
    XI = np.linalg.inv(X.T @ X)
    b = XI @ X.T @ y
    u = y - X @ b
    by = defaultdict(list)
    for i, d in enumerate(day):
        by[d].append(i)
    M = sum(np.outer(X[ix].T @ u[ix], X[ix].T @ u[ix]) for ix in by.values())
    G, N = len(by), len(y)
    V = XI @ M @ XI * (G / (G - 1)) * ((N - 1) / (N - 2))
    return float(b[1]), float(math.sqrt(V[1, 1]))


def _desc(y, day):
    if not y:
        return {"n": 0}
    m = statistics.fmean(y)
    sums = defaultdict(float)
    for v, d in zip(y, day):
        sums[d] += v - m
    G, N = len(sums), len(y)
    se = math.sqrt(sum(s * s for s in sums.values()) * G / (G - 1)) / N if G > 1 else float("nan")
    return {"n": N, "mean": round(m, 2), "median": round(statistics.median(y), 2),
            "t_day": round(m / se, 2) if se and se == se and se > 0 else None, "hit": round(sum(v > 0 for v in y) / N, 3)}


def score():
    import databento as db
    import numpy as np
    pl = json.load(open(os.path.join(WORK, "plan.json")))
    ch = json.load(open(os.path.join(WORK, "quote.json")))["chosen"]
    nd = selected(pl, ch)
    days = [d for d in pl["days"] if not ch.get("first_session") or d >= ch["first_session"]]
    mid = len(days) // 2
    half = {d: (1 if i < mid else 2) for i, d in enumerate(days)}
    D = os.path.join(WORK, "book")
    rows, drops = [], defaultdict(int)
    halts = {}
    for x in nd:
        fn = os.path.join(D, f"{x['day']}_{x['sym']}.dbn.zst")
        if not os.path.exists(fn):
            drops["not_fetched_nameday"] += 1
            continue
        if x["day"] not in halts:
            sf = os.path.join(D, f"status_{x['day']}.dbn.zst")
            halts[x["day"]] = _halts(sf) if os.path.exists(sf) else None
        hl = (halts[x["day"]] or {}).get(x["sym"], [])
        df = db.DBNStore.from_file(fn).to_df(pretty_ts=False, price_type="float").reset_index()
        if df.empty:
            drops["empty_book_nameday"] += 1
            continue
        df = df.sort_values("ts_recv")
        ts = df["ts_recv"].values.astype(np.int64)
        bp, ap = df["bid_px_00"].values, df["ask_px_00"].values
        has5 = "bid_sz_04" in df.columns
        if has5:
            bsz = df[[f"bid_sz_0{k}" for k in range(5)]].sum(axis=1).values.astype(float)
            asz = df[[f"ask_sz_0{k}" for k in range(5)]].sum(axis=1).values.astype(float)

        def book(t):
            i = int(np.searchsorted(ts, t, side="right")) - 1
            if i < 0:
                return None, "no_book"
            b, a = float(bp[i]), float(ap[i])
            if not (b > 0 and a > 0 and math.isfinite(b) and math.isfinite(a)):
                return None, "one_sided"
            if b >= a:
                return None, "crossed_locked"
            return (b, a, i), None
        ents = ([("SIGNAL", x["signal"])] if x["signal"] else []) + [("CONTROL", t) for t in x["controls"]]
        for kind, dec in ents:
            te = int((dec + LAT) * NS)
            e, why = book(te)
            if e is None:
                drops[f"{kind}_entry_{why}"] += 1
                continue
            b0, a0, i0 = e
            r = {"day": x["day"], "sym": x["sym"], "half": half[x["day"]], "kind": kind, "dec": dec,
                 "spread_bp": (a0 - b0) / ((a0 + b0) / 2) * 1e4, "px": (a0 + b0) / 2}
            if has5:
                r["obi5"] = (bsz[i0] - asz[i0]) / (bsz[i0] + asz[i0]) if bsz[i0] + asz[i0] > 0 else None
            for hk, hs in HOLDS.items():
                tx = te + hs * NS if hs else int(ts_et(x["day"], 9, 35).timestamp()) * NS
                if any(a < tx and b > te for a, b in hl):
                    drops[f"{kind}_{hk}_halt"] += 1
                    continue
                ex_, why = book(tx)
                if ex_ is None:
                    drops[f"{kind}_{hk}_exit_{why}"] += 1
                    continue
                b1, a1, _ = ex_
                r[f"g_{hk}"] = (((b1 + a1) / 2) / ((b0 + a0) / 2) - 1) * 1e4
                r[f"n_{hk}"] = (b1 / a0 - 1) * 1e4
            rows.append(r)
    res = {"prereg": "docs/studies/premarket_depth_prereg.json (f7a0e2d)", "rung": ch,
           "sessions": len(days), "split": f"H1 {days[0]}..{days[mid-1]} ({mid}), H2 {days[mid]}..{days[-1]} ({len(days)-mid})",
           "namedays_selected": len(nd), "namedays_with_signal": sum(1 for x in nd if x["signal"]),
           "drops": dict(drops), "entries_scored": len(rows)}

    def test(hh, hk):
        rs = [r for r in rows if r["half"] == hh and r.get(f"g_{hk}") is not None]
        sig = [r for r in rs if r["kind"] == "SIGNAL"]
        ctl = [r for r in rs if r["kind"] == "CONTROL"]
        out = {"SIGNAL_gross": _desc([r[f"g_{hk}"] for r in sig], [r["day"] for r in sig]),
               "CONTROL_gross": _desc([r[f"g_{hk}"] for r in ctl], [r["day"] for r in ctl]),
               "SIGNAL_net": _desc([r[f"n_{hk}"] for r in sig], [r["day"] for r in sig]),
               "CONTROL_net": _desc([r[f"n_{hk}"] for r in ctl], [r["day"] for r in ctl]),
               "median_spread_bp_signal": round(statistics.median(r["spread_bp"] for r in sig), 1) if sig else None,
               "median_spread_bp_control": round(statistics.median(r["spread_bp"] for r in ctl), 1) if ctl else None}
        if sig and ctl and len({r["day"] for r in rs}) > 2:
            b, se = _cl([r[f"g_{hk}"] for r in rs], [r["kind"] == "SIGNAL" for r in rs], [r["day"] for r in rs])
            out.update({"diff_bp": round(b, 2), "se": round(se, 2), "t": round(b / se, 2) if se else None})
        out["RT_bp"] = round(statistics.fmean(r[f"g_{hk}"] - r[f"n_{hk}"] for r in sig), 2) if sig else None
        return out
    T = {hk: {f"H{hh}": test(hh, hk) for hh in (1, 2)} for hk in HOLDS}
    res["tables"] = T
    p1, p2 = T["h5"]["H1"], T["h5"]["H2"]
    power = {"se_H1": p1.get("se"), "2.84xSE": round(2.84 * p1["se"], 2) if p1.get("se") else None, "RT_H1": p1["RT_bp"],
             "n_signal_H1": p1["SIGNAL_gross"]["n"], "n_signal_H2": p2["SIGNAL_gross"]["n"]}
    power["ok"] = bool(p1.get("se") and p1["RT_bp"] is not None and 2.84 * p1["se"] <= p1["RT_bp"] and power["n_signal_H1"] >= 100 and power["n_signal_H2"] >= 100)
    res["power_check"] = power
    crit = {f"H{hh}": {"diff>RT": (T["h5"][f"H{hh}"].get("diff_bp") or -1e9) > (T["h5"][f"H{hh}"]["RT_bp"] if T["h5"][f"H{hh}"]["RT_bp"] is not None else 1e9),
                       "t>=2": (T["h5"][f"H{hh}"].get("t") or 0) >= 2} for hh in (1, 2)}
    res["criteria_h5"] = crit
    res["verdict"] = ("UNDERPOWERED: no verdict" if not power["ok"] else
                      "PASS" if all(all(v.values()) for v in crit.values()) else "FAIL")
    if any("obi5" in r for r in rows):   # information: OBI5 top tercile among CONTROL, demeaned within name-day
        g = defaultdict(list)
        for r in rows:
            if r["kind"] == "CONTROL" and r.get("obi5") is not None:
                g[(r["day"], r["sym"])].append(r)
        for v in g.values():
            m = statistics.fmean(r["obi5"] for r in v)
            for r in v:
                r["obi5_dm"] = r["obi5"] - m
        c1 = [r for r in rows if r.get("obi5_dm") is not None and r["half"] == 1 and r.get("g_h5") is not None]
        if c1:
            cut = sorted(r["obi5_dm"] for r in c1)[int(len(c1) * 2 / 3)]
            info = {}
            for hh in (1, 2):
                rs = [r for r in rows if r.get("obi5_dm") is not None and r["half"] == hh and r.get("g_h5") is not None]
                if len({r["day"] for r in rs}) > 2:
                    b, se = _cl([r["g_h5"] for r in rs], [r["obi5_dm"] >= cut for r in rs], [r["day"] for r in rs])
                    info[f"H{hh}"] = {"top_minus_rest_gross_h5_bp": round(b, 2), "t": round(b / se, 2), "n": len(rs)}
            res["info_obi5_control_h5"] = {"cut_H1": round(cut, 4), **info}
    json.dump(res, open(os.path.join(WORK, "result.json"), "w"), indent=1)
    P(json.dumps({k: v for k, v in res.items() if k != "tables"}, indent=1))
    for hk in HOLDS:
        for hh in ("H1", "H2"):
            t = T[hk][hh]
            P(f"{hk} {hh}: diff {t.get('diff_bp')} t {t.get('t')} RT {t['RT_bp']} | SIG gross {t['SIGNAL_gross']} net {t['SIGNAL_net']}"
              f" | CTL gross {t['CONTROL_gross']} net {t['CONTROL_net']} | med spread sig {t['median_spread_bp_signal']} ctl {t['median_spread_bp_control']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"rawdaily": rawdaily, "scan": scan, "minute": minute, "plan": plan, "quote": quote, "fetch": lambda: fetch("--pipe" in sys.argv),
     "score": score}.get(cmd, lambda: P(__doc__))()
