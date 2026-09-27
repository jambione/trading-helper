#!/usr/bin/env python3
"""momentum_runway_study.py — do momentum seeds offer runway at the moment we would open them?

Read-only on the desk (reads ai_reports/shadow.jsonl, proposal_ledger/, trades.jsonl;
writes only to $MOMRUN_OUT, default /tmp/momrun). Findings: docs/studies/MOMENTUM_RUNWAY_2026-09-27.md

Seeds   9/16-9/25: proposal_ledger stage=seed decision=kept (first ts per proposer);
        9/1-9/15: watch-book admissions (shadow.jsonl source) — the only seed record there.
Events  every eligible 1m decision minute 09:35-15:30 in $20-$100 (random-minute baseline),
        live-arm crosses (fast %R21/EWM7 up through -50, slow %R112/EWM3 rising; SIP 1m,
        premarket-warmed), and desk buys (trades.jsonl). Tagged by the sources seated then.
Entry   next bar open + half the name-day SIP spread (median of 10 s quote windows at
        10:30/12:30/14:30). Runway = +X bp before -35 bp within 30 min (lows first);
        mir50 = the mirror (-50 before +35) as a volatility null. end30 net of full spread.
USAGE (on the mini, from the repo root, niced)
  PYTHONPATH=$PWD:$PWD/tools:$PWD/tools/studies nice -n 15 .venv/bin/python tools/studies/momentum_runway_study.py fetch
  PYTHONPATH=$PWD:$PWD/tools:$PWD/tools/studies nice -n 15 .venv/bin/python tools/studies/momentum_runway_study.py analyze
"""

import json, os, pickle, random, sys, time, statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
ROOT = os.getcwd()
AR = os.path.join(ROOT, "ai_reports")
OUT = os.environ.get("MOMRUN_OUT", "/tmp/momrun")
os.makedirs(OUT, exist_ok=True)
D0, D1 = "2026-09-01", "2026-09-25"
RES = {"anthropic", "xai", "agy", "research", "claude", "grok"}


def norm(src):
    s = str(src or "").lower()
    if s.startswith("research") or s in RES:
        return "research"
    if s in ("mom", "momentum") or s.startswith("momentum"):
        return "momentum"
    if s in ("st", "stocktwits", "trending"):
        return "trending"
    if s in ("movers", "bb_live"):
        return s
    return None


def dayof(ts):
    return datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")


def seeds():
    """(day, sym) -> {source: first_ts}; plus 'admit' map from the watch-book shadow."""
    seed = defaultdict(dict)
    admit = defaultdict(dict)
    for line in open(os.path.join(AR, "shadow.jsonl")):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ts = r.get("ts")
        if not ts:
            continue
        d = dayof(ts)
        if not (D0 <= d <= D1):
            continue
        src = norm(r.get("source"))
        if not src:
            continue
        k = (d, str(r.get("symbol") or "").upper())
        if src not in admit[k] or ts < admit[k][src]:
            admit[k][src] = ts
    pdir = os.path.join(AR, "proposal_ledger")
    pdays = set()
    for f in sorted(os.listdir(pdir)):
        d = f[:10]
        if not (D0 <= d <= D1):
            continue
        pdays.add(d)
        for line in open(os.path.join(pdir, f)):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("stage") != "seed" or r.get("decision") != "kept":
                continue
            src = norm(r.get("proposer_norm") or r.get("proposer"))
            if not src:
                continue
            k = (d, str(r.get("symbol") or "").upper())
            ts = float(r["ts"])
            if src not in seed[k] or ts < seed[k][src]:
                seed[k][src] = ts
    # days without a proposal ledger: the book admission (shadow owner) is the seed record
    for k, v in admit.items():
        if k[0] not in pdays:
            for s, ts in v.items():
                seed[k].setdefault(s, ts)
        else:  # also merge the admission owner (it can be a seed the ledger missed)
            for s, ts in v.items():
                if s not in seed[k]:
                    seed[k][s] = ts
    return dict(seed), dict(admit), sorted(pdays)


def buys():
    out = []
    for line in open(os.path.join(AR, "trades.jsonl")):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("action") != "buy" or not r.get("ts"):
            continue
        d = dayof(r["ts"])
        if D0 <= d <= D1:
            out.append({"day": d, "sym": str(r["symbol"]).upper(), "ts": float(r["ts"]),
                        "reason": r.get("reason"), "mode": r.get("mode")})
    return out


def client():
    import ai_entry_watch as ew
    return ew._data_client()


def fetch_bars(want, cache):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    cl = client()
    byday = defaultdict(list)
    for d, s in want:
        if (s, d) not in cache:
            byday[d].append(s)
    for d, syms in sorted(byday.items()):
        dd = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET)
        # prev close
        pc = {}
        for i in range(0, len(syms), 100):
            try:
                bs = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=syms[i:i + 100], timeframe=TimeFrame.Day,
                    start=(dd - timedelta(days=10)).astimezone(timezone.utc),
                    end=dd.astimezone(timezone.utc), feed=DataFeed.SIP))
                for s, rows in (bs.data or {}).items():
                    rows = [r for r in rows if r.timestamp.astimezone(ET).strftime("%Y-%m-%d") < d]
                    if rows:
                        pc[s] = float(rows[-1].close)
            except Exception as e:  # noqa: BLE001
                print("daily", d, type(e).__name__, str(e)[:100])
            time.sleep(0.4)
        for i in range(0, len(syms), 50):
            batch = syms[i:i + 50]
            got = {}
            try:
                bs = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=batch, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=dd.replace(hour=4).astimezone(timezone.utc),
                    end=dd.replace(hour=16, minute=5).astimezone(timezone.utc), feed=DataFeed.SIP))
                for s, rows in (bs.data or {}).items():
                    got[s] = ([r.timestamp.timestamp() for r in rows], [float(r.open) for r in rows],
                              [float(r.high) for r in rows], [float(r.low) for r in rows],
                              [float(r.close) for r in rows], [float(r.volume) for r in rows])
            except Exception as e:  # noqa: BLE001
                print("bars", d, i, type(e).__name__, str(e)[:100])
                continue
            for s in batch:
                cache[(s, d)] = (got.get(s), pc.get(s))
            time.sleep(0.4)
        print("bars", d, len(syms), flush=True)
    return cache


def fetch_spreads(namedays, sp):
    """Typical SIP spread per name-day: median (ask-bid)/mid in bp over 10 s windows
    at 10:30, 12:30 and 14:30 ET (batched by symbol)."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    cl = client()
    byday = defaultdict(list)
    for d, s in namedays:
        if f"{s}|{d}" not in sp:
            byday[d].append(s)
    for d, syms in sorted(byday.items()):
        dd = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET)
        acc = defaultdict(list)
        for hh, mm in ((10, 30), (12, 30), (14, 30)):
            t0 = dd.replace(hour=hh, minute=mm)
            for i in range(0, len(syms), 10):
                batch = syms[i:i + 10]
                try:
                    q = cl.get_stock_quotes(StockQuotesRequest(
                        symbol_or_symbols=batch, start=t0.astimezone(timezone.utc),
                        end=(t0 + timedelta(seconds=10)).astimezone(timezone.utc),
                        feed=DataFeed.SIP, limit=20000))
                    for s, rows in (q.data or {}).items():
                        for r in rows:
                            b, a = float(r.bid_price), float(r.ask_price)
                            if b > 0 and a >= b:
                                acc[s].append((a - b) / ((a + b) / 2) * 1e4)
                except Exception as e:  # noqa: BLE001
                    print("quotes", d, type(e).__name__, str(e)[:100])
                time.sleep(0.35)
        for s in syms:
            v = sorted(acc.get(s) or [])
            sp[f"{s}|{d}"] = v[len(v) // 2] if v else None
        print("spreads", d, len(syms), flush=True)
        json.dump(sp, open(os.path.join(OUT, "spreads.json"), "w"))
    return sp


def main_fetch():
    seed, admit, pdays = seeds()
    B = buys()
    print("seed name-days", len(seed), "proposal-ledger days", pdays, "buys", len(B))
    days = sorted({d for d, _ in seed})
    seeded_syms = sorted({s for _, s in seed})
    # non-seed control: names seeded on some day in the period, on days they were NOT seeded
    rng = random.Random(7)
    ctrl = []
    for d in days:
        pool = [s for s in seeded_syms if (d, s) not in seed and s.isalpha()]
        rng.shuffle(pool)
        ctrl += [(d, s) for s in pool[:60]]
    want = set(seed) | set(ctrl) | {(b["day"], b["sym"]) for b in B}
    cp = os.path.join(OUT, "bars.pkl")
    cache = pickle.load(open(cp, "rb")) if os.path.exists(cp) else {}
    src = os.path.join(AR, "source_study_bars.pkl")
    if os.path.exists(src):
        old = pickle.load(open(src, "rb"))
        for (s, d), v in old.items():
            if (d, s) in want and (s, d) not in cache and v and v[0]:
                cache[(s, d)] = v
        del old
    print("want", len(want), "cached", sum((s, d) in cache for d, s in want), flush=True)
    for chunk_day in days:
        sub = [(d, s) for d, s in want if d == chunk_day]
        fetch_bars(sub, cache)
        pickle.dump(cache, open(cp, "wb"))
    # in-band name-days only need spreads
    nd = []
    for d, s in want:
        v = cache.get((s, d))
        if v and v[0] and any(20 <= c <= 100 for c in v[0][4]):
            nd.append((d, s))
    spp = os.path.join(OUT, "spreads.json")
    sp = json.load(open(spp)) if os.path.exists(spp) else {}
    fetch_spreads(nd, sp)
    json.dump({"seed": {f"{d}|{s}": v for (d, s), v in seed.items()},
               "admit": {f"{d}|{s}": v for (d, s), v in admit.items()},
               "ctrl": [f"{d}|{s}" for d, s in ctrl], "buys": B, "pdays": pdays},
              open(os.path.join(OUT, "meta.json"), "w"))
    print("done")




# ---------------------------------------------------------------- analysis

import json, os, pickle, statistics, sys, math
from collections import defaultdict, Counter
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv
sys.path.insert(0, os.path.join(os.getcwd(), "tools", "studies"))
import mid_rise_runway_study as mr  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = os.environ.get("MOMRUN_OUT", "/tmp/momrun")
SRCS = ("momentum", "movers", "trending", "research", "bb_live")
T_OPEN, T_FIRST, T_LAST = 570, 575, 930   # 9:30, 9:35, 15:30 (decision minute = bar close)
STOP, TGTS = 35.0, (20.0, 35.0, 50.0)
HZ = (5, 15, 30, 60)
SPLIT = "2026-09-15"  # train < SPLIT <= test


def med(x):
    x = [v for v in x if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return round(statistics.median(x), 1) if x else None


def grid(B, day):
    """Full 1-minute grid 04:00..16:04 ET; missing minutes carry the last close (h=l=o=c, v=0)."""
    t, o, h, l, c, v = B
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET, hour=4).timestamp()
    n = 12 * 60 + 5
    G = np.full((6, n), np.nan)
    for k in range(len(t)):
        j = int((t[k] - d0) // 60)
        if 0 <= j < n:
            G[:, j] = (o[k], h[k], l[k], c[k], v[k], 1)
    last = np.nan
    for j in range(n):
        if np.isnan(G[3, j]):
            G[0:4, j] = last
            G[4, j] = 0
            G[5, j] = 0
        else:
            last = G[3, j]
    return G  # rows o,h,l,c,v,real ; column j = minute 240+j of the day


def analyze_nameday(G, pc, sp_bp):
    o, h, l, c, v, real = G
    n = G.shape[1]
    mod = np.arange(n) + 240  # minute-of-day of the bar's start
    rth = mod >= T_OPEN
    i_open = int(np.argmax(rth))
    # causal session stats (from 9:30)
    hod = np.full(n, np.nan); lod = np.full(n, np.nan); vwap = np.full(n, np.nan); lasthi = np.full(n, np.nan)
    ch = cl = None; pv = vv = 0.0; lh = None
    for j in range(i_open, n):
        if np.isnan(c[j]):
            continue
        if ch is None or h[j] > ch:
            ch = h[j]; lh = j
        cl = l[j] if cl is None else min(cl, l[j])
        tp = (h[j] + l[j] + c[j]) / 3
        pv += tp * v[j]; vv += v[j]
        hod[j], lod[j], lasthi[j] = ch, cl, lh
        vwap[j] = pv / vv if vv > 0 else c[j]
    # fast %R on real bars (premarket warm), mapped to grid
    idx = np.where(real == 1)[0]
    wr = np.full(n, np.nan)
    if len(idx) > 30:
        fr = mr.percent_r_series(list(h[idx]), list(l[idx]), list(l[idx] * 0 + c[idx]), 21, 7.0)
        for k, j in enumerate(idx):
            if fr[k] is not None:
                wr[j] = fr[k]
        # forward fill
        lastv = np.nan
        for j in range(n):
            if np.isnan(wr[j]):
                wr[j] = lastv
            else:
                lastv = wr[j]
    half = (sp_bp if sp_bp else 10.0) / 2.0
    # decision at close of bar i (minute mod[i]+1); entry at open of bar i+1
    dec_min = mod + 1
    elig = (dec_min >= T_FIRST) & (dec_min <= T_LAST)
    E = np.full(n, np.nan)
    E[:-1] = o[1:] * (1 + half / 1e4)
    W = 61
    Hp = np.concatenate([h, np.full(W, np.nan)])
    Lp = np.concatenate([l, np.full(W, np.nan)])
    Cp = np.concatenate([c, np.full(W, np.nan)])
    hw = swv(Hp[1:], W)[:n]  # hw[i,k] = h[i+1+k]
    lw = swv(Lp[1:], W)[:n]
    cw = swv(Cp[1:], W)[:n]
    res = {}
    with np.errstate(invalid="ignore", divide="ignore"):
        for H in HZ:
            res[f"mfe{H}"] = (np.nanmax(hw[:, :H], axis=1) / E - 1) * 1e4
        h30 = hw[:, :30]; l30 = lw[:, :30]
        pk = np.nanargmax(np.where(np.isnan(h30), -np.inf, h30), axis=1)
        lmin_cum = np.fmin.accumulate(np.where(np.isnan(l30), np.inf, l30), axis=1)
        res["mae_pk"] = (lmin_cum[np.arange(n), pk] / E - 1) * 1e4
        stop_px = E * (1 - STOP / 1e4)
        sb = l30 <= stop_px[:, None]
        fs = np.where(sb.any(axis=1), sb.argmax(axis=1), 999)
        for X in TGTS:
            tb = h30 >= (E * (1 + X / 1e4))[:, None]
            ft = np.where(tb.any(axis=1), tb.argmax(axis=1), 999)
            # lows first on the same bar (conservative)
            res[f"hit{int(X)}"] = (ft < 999) & (ft < fs)
        # mirror: -50 before +35 on the same entry (highs first = conservative for the mirror)
        tb_s = l30 <= (E * (1 - 50 / 1e4))[:, None]
        ft_s = np.where(tb_s.any(axis=1), tb_s.argmax(axis=1), 999)
        sb_s = h30 >= (E * (1 + STOP / 1e4))[:, None]
        fs_s = np.where(sb_s.any(axis=1), sb_s.argmax(axis=1), 999)
        res["mir50"] = (ft_s < 999) & (ft_s < fs_s)
        res["end30"] = (cw[:, 29] / E - 1 - half / 1e4) * 1e4  # net of full spread
        res["end30g"] = (cw[:, 29] / np.concatenate([o[1:], [np.nan]]) - 1) * 1e4  # gross, next-open to close+30
        # features (causal, at bar i close)
        res["dhod"] = (hod - c) / c * 1e4
        res["daychg"] = (c / pc - 1) * 100 if pc else np.full(n, np.nan)
        rng_ = hod - lod
        res["rangepos"] = np.where(rng_ > 0, (c - lod) / rng_, np.nan)
        H15 = swv(np.concatenate([np.full(14, np.nan), h]), 15)[:n]
        res["pb15"] = (np.nanmax(H15, axis=1) - c) / c * 1e4
        res["wr"] = wr
        res["vwap"] = (c / vwap - 1) * 1e4
        res["since_hi"] = np.arange(n) - lasthi
        c1 = np.concatenate([[np.nan], c[:-1]]); c5 = np.concatenate([np.full(5, np.nan), c[:-5]])
        res["r1"] = (c / c1 - 1) * 1e4
        res["r5"] = (c / c5 - 1) * 1e4
        res["spread"] = np.full(n, sp_bp if sp_bp else np.nan)
        res["tod"] = dec_min.astype(float)
    ok = elig & ~np.isnan(E) & (c >= 20) & (c <= 100) & ~np.isnan(res["mfe30"])
    # day-level (hindsight) facts
    rth_idx = np.where(rth & ~np.isnan(c) & (mod < 960))[0]
    day = {}
    if len(rth_idx):
        op = o[rth_idx[0]]
        hi_j = rth_idx[np.nanargmax(h[rth_idx])]
        day = {"open": op, "hod": h[hi_j], "hod_min": int(mod[hi_j]), "i0": int(rth_idx[0])}
    return res, ok, c, day


def crosses_on(G):
    """live arm (fast %R21/EWM7 up through -50, slow %R112/EWM3 rising) on real bars -> grid idx."""
    import source_optimal_study as so
    o, h, l, c, v, real = G
    idx = np.where(real == 1)[0]
    B = ([float(j) for j in idx], list(o[idx]), list(h[idx]), list(l[idx]), list(c[idx]), list(v[idx]))
    return [int(idx[i]) for i, rising in so.crosses(B) if rising]


def summarize(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    g = lambda k: [r[k] for r in rows]
    return {"n": n, "mfe5": med(g("mfe5")), "mfe15": med(g("mfe15")), "mfe30": med(g("mfe30")),
            "mfe60": med(g("mfe60")), "mae_pk": med(g("mae_pk")),
            "hit20": round(100 * sum(g("hit20")) / n, 1), "hit35": round(100 * sum(g("hit35")) / n, 1),
            "hit50": round(100 * sum(g("hit50")) / n, 1), "mir50": round(100 * sum(g("mir50")) / n, 1),
            "end30_med": med(g("end30")), "end30g_mean": round(float(np.nanmean([x for x in g("end30g") if x is not None])), 1),
            "days": len({r["day"] for r in rows}), "namedays": len({(r["day"], r["sym"]) for r in rows}),
            "end30_mean": round(float(np.nanmean([x for x in g("end30") if x is not None])), 1),
            "dhod": med(g("dhod")), "daychg": med(g("daychg")), "tod": med(g("tod")),
            "rangepos": (lambda m: None if m is None else round(m, 2))(statistics.median([x for x in g("rangepos") if x is not None]) if any(x is not None for x in g("rangepos")) else None)}


FEATS = ("dhod", "daychg", "rangepos", "pb15", "wr", "vwap", "since_hi", "r1", "r5", "spread", "tod")


def main_analyze():
    meta = json.load(open(os.path.join(OUT, "meta.json")))
    bars = pickle.load(open(os.path.join(OUT, "bars.pkl"), "rb"))
    sp = json.load(open(os.path.join(OUT, "spreads.json")))
    seed = {tuple(k.split("|")): v for k, v in meta["seed"].items()}
    ctrl = {tuple(k.split("|")) for k in meta["ctrl"]}
    buys = meta["buys"]
    namedays = sorted(set(seed) | ctrl | {(b["day"], b["sym"]) for b in buys})
    minute_rows = defaultdict(list)   # group -> rows (random-minute baseline, all eligible minutes)
    cross_rows = defaultdict(list)
    buy_rows = defaultdict(list)
    mom_minutes = []                  # every eligible post-seed minute on momentum name-days
    windows = []                      # good windows on momentum name-days
    entry_class = defaultdict(lambda: defaultdict(float))
    seed_timing = []
    nd_used = Counter()
    buys_by = defaultdict(list)
    for b in buys:
        buys_by[(b["day"], b["sym"])].append(b["ts"])
    ctrl_minutes_good = [0, 0]

    def tags_at(k, ts):
        if k in ctrl:
            return ["non-seed"]
        tg = [s for s, t0 in seed.get(k, {}).items() if t0 <= ts]
        return tg if tg else (["pre-seed"] if k in seed else ["non-seed"])

    for k in namedays:
        d, s = k
        rec = bars.get((s, d))
        if not rec or not rec[0]:
            continue
        G = grid(rec[0], d)
        res, ok, c, day = analyze_nameday(G, rec[1], sp.get(f"{s}|{d}"))
        if not ok.any() or not day:
            continue
        d0 = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET, hour=4).timestamp()
        ts_of = lambda i: d0 + (i + 1) * 60  # decision time (bar close)

        def row(i, **kw):
            r = {f: (None if np.isnan(res[f][i]) else float(res[f][i])) if f not in ("hit20", "hit35", "hit50", "mir50") else bool(res[f][i])
                 for f in res}
            r.update(day=d, sym=s, i=i, **kw)
            return r
        oki = np.where(ok)[0]
        for g in (set(seed.get(k, {})) | ({"non-seed"} if k in ctrl else set())):
            nd_used[g] += 1
        # random-minute baseline: every eligible minute, tagged by the sources seated at that minute
        for i in oki:
            for tg in tags_at(k, ts_of(i)):
                minute_rows[tg].append(row(i))
        good = res["hit50"] & ok
        # crosses
        cx = [i for i in crosses_on(G) if ok[i]]
        for i in cx:
            for tg in tags_at(k, ts_of(i)):
                cross_rows[tg].append(row(i))
        # desk buys (decision = the minute containing the buy)
        for bts in buys_by.get(k, []):
            i = int((bts - d0) // 60)
            if 0 <= i < len(ok) and ok[i]:
                for tg in tags_at(k, bts):
                    buy_rows[tg].append(row(i))
        if k in ctrl:
            ctrl_minutes_good[0] += int(good[oki].sum()); ctrl_minutes_good[1] += len(oki)
        mts = seed.get(k, {}).get("momentum")
        if mts is None:
            continue
        # momentum name-day: good windows, entry classification, seed timing, feature table
        seed_i = int((mts - d0) // 60)
        post = ok & (np.arange(len(ok)) >= seed_i)
        runs = []
        j = 0
        while j < len(ok):
            if good[j]:
                a = j
                while j + 1 < len(ok) and good[j + 1]:
                    j += 1
                runs.append((a, j))
            j += 1
        for a, b in runs:
            windows.append({"day": d, "sym": s, "start": int(res["tod"][a]), "len": b - a + 1,
                            "post_seed": bool(a >= seed_i)})
        start_set = {a for a, _ in runs}
        for i in np.where(post)[0]:
            r = row(i)
            r["good"] = bool(good[i]); r["gstart"] = i in start_set
            mom_minutes.append(r)
        gi = np.where(good)[0]

        def klass(i):
            if good[i]:
                return "in_window"
            ahead = gi[(gi > i) & (gi <= i + 15)]
            behind = gi[(gi < i) & (gi >= i - 30)]
            if len(ahead):
                return "early(<=15m)"
            if len(behind):
                return "late(window closed <=30m ago)"
            return "chop/no window" if len(gi) else "dead name-day"
        for i in cx:
            if i >= seed_i:
                entry_class["cross"][klass(i)] += 1
        for i in np.where(post)[0]:
            entry_class["random_postseed_minute"][klass(i)] += 1
        pre = ok & (np.arange(len(ok)) < seed_i)
        entry_class["_good_rate_pre_post"]["pre_n"] += int(pre.sum())
        entry_class["_good_rate_pre_post"]["pre_good"] += int((good & pre).sum())
        entry_class["_good_rate_pre_post"]["post_n"] += int(post.sum())
        entry_class["_good_rate_pre_post"]["post_good"] += int((good & post).sum())
        for i in np.where(pre)[0]:
            entry_class["_pre_end30"]["n"] += 1
            entry_class["_pre_end30"]["sum_net"] += float(res["end30"][i])
        for bts in buys_by.get(k, []):
            i = int((bts - d0) // 60)
            if 0 <= i < len(ok) and ok[i] and "momentum" in tags_at(k, bts):
                entry_class["buy"][klass(i)] += 1
        # seed timing vs the day's move
        op, hodf = day["open"], day["hod"]
        seed_min = int(datetime.fromtimestamp(mts, ET).hour * 60 + datetime.fromtimestamp(mts, ET).minute)
        px_seed = c[min(max(seed_i, day["i0"]), len(c) - 1)] if seed_min >= T_OPEN else None
        frac = ((px_seed - op) / (hodf - op)) if (px_seed is not None and hodf > op) else None
        n_good_post = int(sum(1 for a, b in runs if a >= seed_i))
        n_good_pre = int(sum(1 for a, b in runs if a < seed_i))
        seed_timing.append({"day": d, "sym": s, "seed_min": seed_min, "hod_min": day["hod_min"],
                            "frac_move_done": frac, "hod_before_seed": day["hod_min"] < seed_min,
                            "open_to_hod_bp": (hodf / op - 1) * 1e4, "win_pre": n_good_pre,
                            "win_post": n_good_post, "in_band": bool(((c >= 20) & (c <= 100))[ok].any())})

    out = {"namedays_used": dict(nd_used)}
    out["random_minutes"] = {g: summarize(v) for g, v in minute_rows.items()}
    out["crosses"] = {g: summarize(v) for g, v in cross_rows.items()}
    out["buys"] = {g: summarize(v) for g, v in buy_rows.items()}
    # momentum context splits: crosses vs random minutes
    def bucket(r, f, edges, labels):
        x = r.get(f)
        if x is None:
            return None
        for e, lb in zip(edges, labels):
            if x < e:
                return lb
        return labels[-1]
    splits = {
        "dhod": ([25, 75, 150, 1e9], ["<25bp (at HOD)", "25-75", "75-150", ">150"]),
        "tod": ([630, 720, 840, 1e9], ["09:35-10:30", "10:30-12:00", "12:00-14:00", "14:00-15:30"]),
        "daychg": ([3, 6, 10, 1e9], ["<3%", "3-6%", "6-10%", ">10%"]),
        "rangepos": ([0.5, 0.8, 0.95, 1e9], ["<0.5", "0.5-0.8", "0.8-0.95", ">=0.95"]),
    }
    out["mom_splits"] = {}
    for f, (e, lb) in splits.items():
        tab = {}
        for L in lb:
            cr = [r for r in cross_rows["momentum"] if bucket(r, f, e, lb) == L]
            rm = [r for r in minute_rows["momentum"] if bucket(r, f, e, lb) == L]
            tab[L] = {"cross": summarize(cr), "random": summarize(rm)}
        out["mom_splits"][f] = tab
    # hindsight windows
    W = [w for w in windows if w["post_seed"]]
    nmd = len(seed_timing)
    nmd_band = sum(1 for x in seed_timing if x["in_band"])
    per = Counter((w["day"], w["sym"]) for w in W)
    zero = sum(1 for x in seed_timing if x["in_band"] and per.get((x["day"], x["sym"]), 0) == 0)
    lens = [w["len"] for w in W]
    out["windows"] = {
        "mom_namedays": nmd, "mom_namedays_in_band": nmd_band,
        "windows_post_seed": len(W), "namedays_with_0_windows": zero,
        "per_nameday_median": med([per.get((x["day"], x["sym"]), 0) for x in seed_timing if x["in_band"]]),
        "per_nameday_mean": round(len(W) / max(1, nmd_band), 2),
        "len_median_min": med(lens), "len_p75": float(np.percentile(lens, 75)) if lens else None,
        "len_share_1min": round(100 * sum(1 for x in lens if x == 1) / max(1, len(lens)), 1),
        "start_tod_hist": dict(Counter(f"{w['start'] // 60:02d}:{(w['start'] % 60) // 30 * 30:02d}" for w in W)),
        "good_minute_rate_mom_postseed": round(100 * sum(r["good"] for r in mom_minutes) / max(1, len(mom_minutes)), 1),
        "good_minute_rate_nonseed": round(100 * ctrl_minutes_good[0] / max(1, ctrl_minutes_good[1]), 1),
        "windows_pre_seed": sum(1 for w in windows if not w["post_seed"]),
    }
    out["entry_class"] = {k: dict(v) for k, v in entry_class.items()}
    st = [x for x in seed_timing if x["in_band"]]
    out["seed_timing"] = {
        "n": len(st),
        "seed_min_median": med([x["seed_min"] for x in st]),
        "hod_min_median": med([x["hod_min"] for x in st]),
        "share_premarket_seed": round(100 * sum(x["seed_min"] < T_OPEN for x in st) / max(1, len(st)), 1),
        "share_hod_before_seed": round(100 * sum(x["hod_before_seed"] for x in st) / max(1, len(st)), 1),
        "frac_move_done_median": med([x["frac_move_done"] for x in st if x["frac_move_done"] is not None]),
        "frac_move_done_ge_0.8": round(100 * sum(1 for x in st if (x["frac_move_done"] or 0) >= 0.8) / max(1, len(st)), 1),
        "open_to_hod_bp_median": med([x["open_to_hod_bp"] for x in st]),
        "windows_pre_vs_post": [sum(x["win_pre"] for x in st), sum(x["win_post"] for x in st)],
        "seed_hour_hist": dict(Counter(f"{x['seed_min'] // 60:02d}" for x in st)),
    }
    # (3) observable features: good-window starts vs non-good minutes (momentum, post-seed)
    gs = [r for r in mom_minutes if r["gstart"]]
    ng = [r for r in mom_minutes if not r["good"]]
    out["features_gstart_vs_nongood"] = {f: {"gstart": med([r[f] for r in gs]), "nongood": med([r[f] for r in ng]),
                                            "good_any": med([r[f] for r in mom_minutes if r["good"]])}
                                        for f in FEATS}
    out["features_n"] = {"gstart": len(gs), "nongood": len(ng), "minutes": len(mom_minutes)}
    # rule search: single/pair thresholds; events = first minute a rule turns true, 15-min refractory per name
    alld = sorted({r["day"] for r in mom_minutes})
    half_a = set(alld[0::2])
    train = [r for r in mom_minutes if r["day"] in half_a]
    test = [r for r in mom_minutes if r["day"] not in half_a]
    out["split"] = {"train_days": sorted(half_a), "test_days": sorted(set(alld) - half_a)}

    def events(rows, pred):
        ev, last = [], {}
        for r in sorted(rows, key=lambda r: (r["day"], r["sym"], r["i"])):
            k = (r["day"], r["sym"])
            if pred(r) and r["i"] - last.get(k, -99) >= 15:
                ev.append(r); last[k] = r["i"]
        return ev

    def score(ev):
        if not ev:
            return None
        return {"n": len(ev), "names": len({(r["day"], r["sym"]) for r in ev}),
                "hit50": round(100 * sum(r["hit50"] for r in ev) / len(ev), 1),
                "hit35": round(100 * sum(r["hit35"] for r in ev) / len(ev), 1),
                "mir50": round(100 * sum(r["mir50"] for r in ev) / len(ev), 1),
                "end30_mean": round(float(np.nanmean([r["end30"] for r in ev if r["end30"] is not None])), 1),
                "end30_med": med([r["end30"] for r in ev])}
    qs = {}
    for f in FEATS:
        vals = [r[f] for r in train if r[f] is not None]
        if len(vals) > 100:
            qs[f] = sorted(set(round(float(np.percentile(vals, p)), 2) for p in (10, 20, 30, 40, 50, 60, 70, 80, 90)))
    singles = []
    for f, th in qs.items():
        for t_ in th:
            for op in ("<", ">"):
                pred = (lambda f, t_, op: (lambda r: r[f] is not None and (r[f] < t_ if op == "<" else r[f] > t_)))(f, t_, op)
                singles.append((f"{f}{op}{t_}", pred))
    base_tr = score(events(train, lambda r: True)); base_te = score(events(test, lambda r: True))
    res_s = []
    for name, pred in singles:
        a = score(events(train, pred))
        if a and a["n"] >= 150:
            res_s.append((a["hit50"], name, pred, a))
    res_s.sort(key=lambda x: -x[0])
    top = res_s[:12]
    pairs = []
    for x in range(len(top)):
        for y in range(x + 1, len(top)):
            if top[x][1].split("<")[0].split(">")[0] == top[y][1].split("<")[0].split(">")[0]:
                continue
            p1, p2 = top[x][2], top[y][2]
            pred = (lambda p1, p2: (lambda r: p1(r) and p2(r)))(p1, p2)
            a = score(events(train, pred))
            if a and a["n"] >= 100:
                pairs.append((a["hit50"], top[x][1] + " & " + top[y][1], pred, a))
    pairs.sort(key=lambda x: -x[0])
    out["rules"] = {"base_train": base_tr, "base_test": base_te,
                    "top_singles": [{"rule": nm, "train": a, "test": score(events(test, p))} for _, nm, p, a in res_s[:10]],
                    "top_pairs": [{"rule": nm, "train": a, "test": score(events(test, p))} for _, nm, p, a in pairs[:8]],
                    "n_candidates_tested": len(singles) + len(pairs)}
    # also score the same top rules on non-seed control minutes? (kept simple: momentum only)
    json.dump(out, open(os.path.join(OUT, "results.json"), "w"), indent=1, default=str)
    pickle.dump({"windows": windows, "seed_timing": seed_timing}, open(os.path.join(OUT, "detail.pkl"), "wb"))
    def line(tag, d):
        if not d or not d.get("n"):
            return f"{tag:34s} n=0"
        return (f"{tag:34s} n={d['n']:6d} nd={d['namedays']:4d} days={d['days']:2d} mfe5/15/30/60={d['mfe5']}/{d['mfe15']}/{d['mfe30']}/{d['mfe60']} "
                f"maePk={d['mae_pk']} h20/35/50={d['hit20']}/{d['hit35']}/{d['hit50']} mir50={d['mir50']} end30net med/mean={d['end30_med']}/{d['end30_mean']} gross_mean={d['end30g_mean']} "
                f"dhod={d['dhod']} chg={d['daychg']} tod={d['tod']} rpos={d['rangepos']}")
    for sec in ("random_minutes", "crosses", "buys"):
        print("==", sec)
        for g, d in out[sec].items():
            print(line(g, d))
    for f, tab in out["mom_splits"].items():
        print("== momentum split", f)
        for L, x in tab.items():
            print(line("X " + L, x["cross"]))
            print(line("R " + L, x["random"]))
    for k in ("windows", "entry_class", "seed_timing", "features_n", "features_gstart_vs_nongood", "split"):
        print("==", k, json.dumps(out.get(k), default=str))
    print("== rules base", out["rules"]["base_train"], out["rules"]["base_test"], "tested", out["rules"]["n_candidates_tested"])
    for x in out["rules"]["top_singles"] + out["rules"]["top_pairs"]:
        print("  ", x["rule"], "| train", x["train"], "| test", x["test"])




if __name__ == "__main__":
    {"fetch": main_fetch, "analyze": main_analyze}[sys.argv[1] if len(sys.argv) > 1 else "analyze"]()
