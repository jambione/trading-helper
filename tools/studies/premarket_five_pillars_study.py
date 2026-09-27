"""Premarket five-pillars study -- 2026-09-27 (studies-only, read-only on the desk; outputs in /tmp/pm).

Question: does buying five-pillar momentum names PREMARKET (entries 07:00-09:25 ET) capture a move after real
SIP spreads? (Decides whether Alpaca's paid live SIP feed is worth it.) Free Alpaca historical SIP only.
Phases (run in order):
  scan     30-min SIP bars 04:00-09:30 for every active common stock with prior close $1-25, every session
           (2025-09-02..2026-09-25; the first 20 sessions only feed the premarket-volume baseline)
  cands    premarket movers: 04:00-09:30 high >= +10% vs prior close and premarket volume >= 20k; baseline
           = name's own mean cumulative premarket volume per half-hour over the prior 20 sessions
  minute   1-min SIP bars 04:00-10:31 per candidate (reuses /tmp/fp/min where present)
  loose    name-days meeting the loosest cell ($2-20, >= +10%, cum premarket vol >= 50k) by the 09:25 decision
  news     Alpaca/benzinga news prior 16:00 -> decision minute (reuses /tmp/fp/news.json)
  float    current float (reuses /tmp/fp/float.json; desk float_cache read-only; else float_feed/Finnhub)
  quotes   SIP NBBO spread at marks 07:00..09:20 every 10 min, 09:25, 09:35, 10:00 (10 s windows)
  analyze  point-in-time qualify minute per cell; entries P1-P4 + random; exits; costs -> /tmp/pm/grid.pkl
  report   tables (train = even-index sessions, test = odd)
Usage: PYTHONPATH=/tmp:$PWD:$PWD/tools:$PWD/tools/studies python tools/studies/premarket_five_pillars_study.py <phase>
Needs /tmp/fp/{daily.pkl,cands.json,min/,news.json,float.json} from tools/studies/five_pillars_study.py.
See docs/studies/PREMARKET_FIVE_PILLARS_2026-09-27.md.
"""
import math
from datetime import timedelta
import json, os, pickle, sys, time, collections
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import numpy as np
ET = ZoneInfo("America/New_York")
OUT = "/tmp/pm"

def scan_main():  # noqa: C901
    """30-min SIP bars 04:00-09:30 for every symbol with prior close $1-25 each session -> per-bucket high/vol/close."""
    from config import load_config
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    cfg = load_config() or {}
    cl = StockHistoricalDataClient(cfg.get("api_key"), cfg.get("secret_key"))
    daily = pickle.load(open("/tmp/fp/daily.pkl", "rb"))
    pcs = collections.defaultdict(dict)
    for s, rows in daily.items():
        for k in range(1, len(rows)):
            if 1.0 <= rows[k - 1][4] <= 25.0:
                pcs[rows[k][0]][s] = rows[k - 1][4]
    days = sorted(pcs)
    os.makedirs(f"{OUT}/scan", exist_ok=True)
    for d in days:
        fp = f"{OUT}/scan/{d}.pkl"
        if os.path.exists(fp):
            continue
        dd = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET)
        d0 = dd.replace(hour=4).timestamp()
        syms = sorted(pcs[d]); res = {}; err = 0
        for i in range(0, len(syms), 200):
            b = syms[i:i + 200]
            for att in range(3):
                try:
                    bs = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=b, timeframe=TimeFrame(30, TimeFrameUnit.Minute),
                                                            start=dd.replace(hour=4).astimezone(timezone.utc),
                                                            end=dd.replace(hour=9, minute=29, second=59).astimezone(timezone.utc),
                                                            feed=DataFeed.SIP, adjustment="split", limit=10000))
                    for s, rows in (bs.data or {}).items():
                        A = np.zeros((3, 11), np.float32); A[0] = np.nan; A[2] = np.nan
                        for r in rows:
                            j = int((r.timestamp.timestamp() - d0) // 1800)
                            if 0 <= j < 11:
                                A[:, j] = (r.high, r.volume, r.close)
                        res[s] = A
                    break
                except Exception as e:  # noqa: BLE001
                    print("scan", d, i, type(e).__name__, str(e)[:100], flush=True); err += 1
                    time.sleep(5 * (att + 1))
            time.sleep(0.25)
        pickle.dump({"pc": pcs[d], "bars": res, "err": err}, open(fp, "wb"))
        print(d, "syms", len(syms), "with_pm_bars", len(res), "err", err, flush=True)



# ======================= phase: cands =======================
STUDY0 = "2025-10-01"
N = 391                 # grid minutes 04:00 .. 10:30 (bar start); j = minutes after 04:00
BASE_FLOOR = 5000.0     # premarket RVOL baseline floor (shares)


def cands_main():
    """Premarket movers from the 30-min scan: prior close $1-25, 04:00-09:30 high >= +10% vs prior close,
    04:00-09:30 volume >= 20k. Baseline = name's own mean cumulative premarket volume at each half-hour
    boundary over the prior 20 scanned sessions (sessions with no premarket bars count as 0)."""
    days = sorted(f[:10] for f in os.listdir(f"{OUT}/scan"))
    hist = collections.defaultdict(list)   # sym -> list of (day, cum11)
    out = []
    for d in days:
        S = pickle.load(open(f"{OUT}/scan/{d}.pkl", "rb"))
        bars = S["bars"]
        for s, pc in S["pc"].items():
            A = bars.get(s)
            cum = np.cumsum(A[1]) if A is not None else np.zeros(11)
            if d >= STUDY0 and A is not None:
                hi = np.nanmax(A[0]) if np.any(~np.isnan(A[0])) else np.nan
                if hi == hi and hi >= 1.10 * pc and cum[-1] >= 20000:
                    prev = hist[s][-20:]
                    base = np.mean([c for _, c in prev], axis=0).tolist() if len(prev) >= 5 else None
                    out.append({"day": d, "sym": s, "pc": pc, "pm_hi": float(hi), "pm_vol": float(cum[-1]),
                                "base": base, "nbase": len(prev)})
            hist[s].append((d, cum))
    json.dump(out, open(f"{OUT}/cands.json", "w"))
    fpc = {(x["day"], x["sym"]) for x in json.load(open("/tmp/fp/cands.json"))}
    new = sum((x["day"], x["sym"]) not in fpc for x in out)
    print("premarket candidate name-days", len(out), "days", len({x['day'] for x in out}),
          "not in daily-bar candidate set (premarket-only movers):", new,
          "no baseline:", sum(x["base"] is None for x in out), flush=True)


# ======================= phase: minute =======================
def minute_main():
    from config import load_config
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    cfg = load_config() or {}
    cl = StockHistoricalDataClient(cfg.get("api_key"), cfg.get("secret_key"))
    cands = json.load(open(f"{OUT}/cands.json"))
    byday = collections.defaultdict(list)
    for x in cands:
        byday[x["day"]].append(x["sym"])
    os.makedirs(f"{OUT}/min", exist_ok=True)
    st = collections.Counter()
    for d in sorted(byday):
        fp = f"{OUT}/min/{d}.pkl"
        if os.path.exists(fp):
            continue
        dd = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET)
        d0 = dd.replace(hour=4).timestamp()
        keep = {}
        fpm = f"/tmp/fp/min/{d}.pkl"
        old = pickle.load(open(fpm, "rb")) if os.path.exists(fpm) else {}
        todo = []
        for s in byday[d]:
            if s in old:
                keep[s] = old[s]["G"][:, :N].copy(); st["reused"] += 1
            else:
                todo.append(s)
        for i in range(0, len(todo), 50):
            b = todo[i:i + 50]
            for att in range(3):
                try:
                    bs = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=b, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                                                            start=dd.replace(hour=4).astimezone(timezone.utc),
                                                            end=dd.replace(hour=10, minute=31).astimezone(timezone.utc),
                                                            feed=DataFeed.SIP, adjustment="split"))
                    data = bs.data or {}
                    break
                except Exception as e:  # noqa: BLE001
                    print("bars", d, i, type(e).__name__, str(e)[:100], flush=True)
                    data = {}
                    time.sleep(5 * (att + 1))
            for s, rows in data.items():
                G = np.full((6, N), np.nan, dtype=np.float32)
                for r in rows:
                    j = int((r.timestamp.timestamp() - d0) // 60)
                    if 0 <= j < N:
                        G[:, j] = (r.open, r.high, r.low, r.close, r.volume, 1)
                real = ~np.isnan(G[5]); last = np.nan
                for j in range(N):
                    if real[j]:
                        last = G[3, j]
                    else:
                        G[0:4, j] = last; G[4, j] = 0; G[5, j] = 0
                keep[s] = G; st["fetched"] += 1
            time.sleep(0.3)
        pickle.dump(keep, open(fp, "wb"))
        print(d, "cands", len(byday[d]), "kept", len(keep), dict(st), flush=True)


# ======================= loose set (used by aux phases) =======================
def loose_set():
    """Name-days that meet the loosest grid cell premarket (real minute <= 09:24 close: $2-20, >= +10%, cum vol >= 50k)."""
    p = f"{OUT}/loose.json"
    if os.path.exists(p):
        return json.load(open(p))
    cands = {(x["day"], x["sym"]): x for x in json.load(open(f"{OUT}/cands.json"))}
    out = collections.defaultdict(dict)
    for f in sorted(os.listdir(f"{OUT}/min")):
        d = f[:10]
        K = pickle.load(open(f"{OUT}/min/{f}", "rb"))
        for s, G in K.items():
            x = cands.get((d, s))
            if not x:
                continue
            c = G[3, :325].astype(float); real = G[5, :325] == 1
            cum = np.cumsum(G[4, :325].astype(float))
            m = real & (c >= 2) & (c <= 20) & (c / x["pc"] - 1 >= 0.10) & (cum >= 50000)
            if m.any():
                out[d][s] = int(np.argmax(m))
    json.dump(out, open(p, "w"))
    print("loose name-days", sum(len(v) for v in out.values()), "days", len(out), flush=True)
    return out


def _cfg():
    from config import load_config
    c = load_config() or {}
    return c.get("api_key"), c.get("secret_key")


# ======================= phase: news / float =======================
def news_main():
    """Reuse /tmp/fp/news.json where the name-day was queried there (fp window prior 16:00 -> 11:00, timestamps
    filtered to the decision minute later); otherwise query Alpaca news prior 16:00 -> 09:30."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest
    nc = NewsClient(*_cfg())
    L = loose_set()
    fpn = json.load(open("/tmp/fp/news.json"))
    fpq = collections.defaultdict(set)
    for f in os.listdir("/tmp/fp/min"):
        K = pickle.load(open(f"/tmp/fp/min/{f}", "rb"))
        fpq[f[:10]] = {s for s, v in K.items() if v["loose"]}
    p = f"{OUT}/news.json"
    nw = json.load(open(p)) if os.path.exists(p) else {}
    alld = sorted(f[:10] for f in os.listdir(f"{OUT}/scan"))
    st = collections.Counter()
    for d in sorted(L):
        if d in nw:
            continue
        got = {}
        todo = []
        for s in L[d]:
            if s in fpq.get(d, set()) and d in fpn:
                if s in fpn[d]:
                    got[s] = fpn[d][s]
                st["reused"] += 1
            else:
                todo.append(s)
        prev = alld[alld.index(d) - 1]
        stt = datetime.strptime(prev, "%Y-%m-%d").replace(tzinfo=ET, hour=16)
        en = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET, hour=9, minute=30)
        for i in range(0, len(todo), 20):
            b = todo[i:i + 20]; tok = None
            for _ in range(30):
                try:
                    kw = dict(symbols=",".join(b), start=stt, end=en, limit=50, include_content=False)
                    if tok:
                        kw["page_token"] = tok
                    r = nc.get_news(NewsRequest(**kw))
                except Exception as e:  # noqa: BLE001
                    print("news", d, type(e).__name__, str(e)[:80], flush=True); time.sleep(3); break
                items = r.data["news"] if isinstance(r.data, dict) else r.data
                for it in items:
                    for s in (it.symbols or []):
                        if s in b:
                            got.setdefault(s, []).append([it.created_at.timestamp(), (it.headline or "")[:120], it.source])
                tok = getattr(r, "next_page_token", None)
                time.sleep(0.3)
                if not tok:
                    break
            st["queried"] += len(b)
        nw[d] = got
        json.dump(nw, open(p, "w"))
        print("news", d, len(L[d]), "with news", len(got), dict(st), flush=True)


def float_main():
    import float_feed as ff
    key = ff._api_key()
    L = loose_set()
    p = f"{OUT}/float.json"
    fl = json.load(open(p)) if os.path.exists(p) else dict(json.load(open("/tmp/fp/float.json")))
    desk = ff.load_cache() or {}
    syms = sorted({s for v in L.values() for s in v})
    n0 = 0
    for s in syms:
        if s in fl:
            continue
        row = desk.get(s)
        if isinstance(row, dict) and row.get("float_m") is not None:
            fl[s] = {"float_m": row["float_m"], "src": "desk_cache", "ts": row.get("ts")}
            continue
        r = ff._fetch_one(s, key)
        if r is ff.RATE_LIMITED:
            time.sleep(60); r = ff._fetch_one(s, key)
        fl[s] = ({"float_m": r.get("float_m"), "shares_out": r.get("shares_out"), "src": "finnhub_now", "ts": time.time()}
                 if isinstance(r, dict) else {"float_m": None, "src": "fail"})
        n0 += 1
        if n0 % 100 == 0:
            json.dump(fl, open(p, "w")); print("float", n0, flush=True)
        time.sleep(1.1)
    json.dump(fl, open(p, "w"))
    print("float done; loose syms", len(syms), "known", sum((fl.get(s) or {}).get("float_m") is not None for s in syms), flush=True)


# ======================= phase: quotes =======================
QWIN = 10
MARKS = list(range(180, 325, 10)) + [325, 335, 360]   # 07:00..09:20 every 10 min, 09:25, 09:35, 10:00


def quotes_main():
    """SIP NBBO at fixed marks for each loose name-day (only marks >= its first loose-qualify minute - 10).
    Spread = median (ask-bid)/mid over quotes in [mark, mark+QWIN s]. Batched by (day, mark)."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockQuotesRequest
    from alpaca.data.enums import DataFeed
    cl = StockHistoricalDataClient(*_cfg())
    L = loose_set()
    rev = len(sys.argv) > 2 and sys.argv[2] == "rev"   # optional 2nd worker: newest days first, own file
    p = f"{OUT}/spreads2.json" if rev else f"{OUT}/spreads.json"
    other = f"{OUT}/spreads.json" if rev else f"{OUT}/spreads2.json"
    sp = json.load(open(p)) if os.path.exists(p) else {}
    for d in sorted(L, reverse=rev):
        if f"__done|{d}" in sp:
            continue
        if os.path.exists(other) and f'"__done|{d}"' in open(other).read():
            break   # the other worker already covered from here on
        dd = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET)
        res = collections.defaultdict(dict)
        for m in MARKS:
            syms = sorted(s for s, q in L[d].items() if m >= min(q, 325) - 10 or m >= 325)
            t0 = dd.replace(hour=4) + timedelta(minutes=m)
            for i in range(0, len(syms), 10):
                b = syms[i:i + 10]; acc = collections.defaultdict(list)
                for _ in range(2):
                    try:
                        kw = dict(symbol_or_symbols=b, start=t0.astimezone(timezone.utc),
                                  end=(t0 + timedelta(seconds=QWIN)).astimezone(timezone.utc), feed=DataFeed.SIP)
                        q = cl.get_stock_quotes(StockQuotesRequest(**kw))
                    except Exception as e:  # noqa: BLE001
                        print("quotes", d, m, type(e).__name__, str(e)[:80], flush=True); time.sleep(3); break
                    for s, rows in (q.data or {}).items():
                        for r in rows:
                            bb, a = float(r.bid_price), float(r.ask_price)
                            if bb > 0 and a >= bb:
                                acc[s].append((a - bb) / ((a + bb) / 2) * 1e4)
                    break   # SDK paginates all quotes in the window (no limit => no cross-symbol truncation)
                time.sleep(0.25)
                for s in b:
                    v = sorted(acc.get(s) or [])
                    res[s][str(m)] = v[len(v) // 2] if v else None
        for s, v in res.items():
            sp[f"{s}|{d}"] = v
        sp[f"__done|{d}"] = 1
        json.dump(sp, open(p, "w"))
        print("quotes", d, len(L[d]), flush=True)


# ======================= phase: analyze =======================
PRICES = {"$2-20": (2, 20), "$2-5": (2, 5), "$5-10": (5, 10), "$10-20": (10, 20)}
GAINS = (0.10, 0.20, 0.30)
VOLS = (50_000, 250_000)
RVOLS = (0, 5, 10)
FLOATS = {"any": None, "<10M": 10}
NEWS = ("any", "yes")
ENTRIES = ("P1", "P2", "P3", "P4", "RND")
EXITS = ("BRpct", "BRc", "H5", "H15", "H30", "T0935", "T1000")
W0, W1 = 180, 325          # entry bar starts 07:00 .. 09:25
QMAX = 324                 # last decision bar (close 09:25)
CAP = 60                   # bracket time cap (minutes)
COLS = ([f"g_{x}" for x in EXITS] + [f"n_{x}" for x in EXITS] +
        ["mfe30", "mae30", "se", "E", "e", "q", "dv5", "day", "nd", "sx_fallback", "se_dist"])
ENTRY_TOL, EXIT_TOL = 10, 20   # max minutes between a trade minute and the quote mark used for its spread
CI = {k: i for i, k in enumerate(COLS)}


def hm(j):
    t = 240 + int(j)
    return f"{t // 60:02d}:{t % 60:02d}"


def sim_bracket(o, h, l, c, real, start, E, t1, t2, trail, stop):
    """Thirds: +t1, +t2, last third trails `trail` from the high once +t2 prints; all start with a fixed stop.
    Stop checked first within a bar (fills at min(stop, open)); targets fill at the level on a trade print.
    Remaining pieces exit at the close of the cap bar. Returns [(price, exit_minute)] per third."""
    fixed = E - stop
    op = [True, True, True]; px = [None] * 3; trail_on = False; hi = None
    last = start; endj = min(start + CAP - 1, N - 1)
    for k in range(start, endj + 1):
        if not real[k]:
            continue
        last = k
        lv = [fixed, fixed, (max(fixed, hi - trail) if trail_on else fixed)]
        for p in range(3):
            if op[p] and l[k] <= lv[p]:
                op[p] = False; px[p] = (min(lv[p], o[k]), k)
        if op[0] and h[k] >= E + t1:
            op[0] = False; px[0] = (E + t1, k)
        if op[1] and h[k] >= E + t2:
            op[1] = False; px[1] = (E + t2, k); trail_on = True; hi = h[k]
        if trail_on:
            hi = max(hi, h[k])
        if not any(op):
            break
    for p in range(3):
        if op[p]:
            px[p] = (c[endj], endj)
    return px


class ND:
    """One loose name-day with point-in-time premarket features."""
    def __init__(self, d, s, G, x, fl, nts, spd):
        self.d, self.s = d, s
        self.o, self.h, self.l, self.c, v, r = [G[k].astype(float) for k in range(6)]
        self.real = r == 1
        self.pc = x["pc"]
        self.cum = np.cumsum(v)
        self.gain = self.c / self.pc - 1
        if x["base"]:
            bj = np.interp(np.arange(N) + 1, np.arange(0, 331, 30), [0.0] + list(x["base"]))
            bj[330:] = x["base"][-1]
            self.rvol = self.cum / np.maximum(bj, BASE_FLOOR)
        else:
            self.rvol = None
        tp = (self.h + self.l + self.c) / 3
        cv = np.cumsum(tp * v)
        with np.errstate(invalid="ignore", divide="ignore"):
            self.vwap = np.where(self.cum > 0, cv / np.where(self.cum > 0, self.cum, 1), np.nan)
        hh = np.where(self.real, self.h, -np.inf)
        self.pmh_prev = np.concatenate([[-np.inf], np.maximum.accumulate(hh)[:-1]])
        b5 = np.array([hh[i:i + 5].max() for i in range(0, N, 5)])
        idx = np.arange(N) // 5 - 1
        self.h5_prev = np.where(idx >= 0, b5[np.maximum(idx, 0)], -np.inf)
        self.dv = self.c * v
        d0 = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET, hour=4).timestamp()
        nts = sorted(nts)
        dec_t = d0 + (np.arange(N) + 1) * 60
        self.news = np.array([bool(nts) and nts[0] <= t for t in dec_t])
        self.float = fl
        self.sp = {int(k): v for k, v in (spd or {}).items() if v is not None and v > 0}

    def spread_at(self, j, tol=10, dist=False):
        best = None
        for m, v in self.sp.items():
            dd = abs(m - j)
            if dd <= tol and (best is None or dd < best[0]):
                best = (dd, v)
        if dist:
            return best
        return best[1] if best else None

    def qualify(self, lo, hi, g, vol, rv, fmax, news):
        if fmax is not None and (self.float is None or self.float >= fmax):
            return None
        if rv and self.rvol is None:
            return None
        sl = slice(0, QMAX + 1)
        m = self.real[sl] & (self.c[sl] >= lo) & (self.c[sl] <= hi) & (self.gain[sl] >= g) & (self.cum[sl] >= vol)
        if rv:
            m &= self.rvol[sl] >= rv
        if news == "yes":
            m &= self.news[sl]
        return int(np.argmax(m)) if m.any() else None

    def entry(self, kind, q):
        a = max(q + 1, W0)
        if kind == "P1":
            for e in range(a, W1 + 1):
                if self.real[e]:
                    return e, self.o[e], e
            return None
        if kind in ("P2", "P3"):
            lvl = self.pmh_prev if kind == "P2" else self.h5_prev
            for e in range(a, W1 + 1):
                L = lvl[e] + 0.01
                if self.real[e] and np.isfinite(L) and self.h[e] >= L:
                    return e, max(self.o[e], L), e + 1
            return None
        if kind == "P4":
            dipped = False
            for m in range(q + 1, W1):
                if not self.real[m] or not np.isfinite(self.vwap[m]):
                    continue
                if self.c[m] < self.vwap[m]:
                    dipped = True
                elif dipped and self.c[m] > self.vwap[m]:
                    if m + 1 < W0:
                        dipped = False
                        continue
                    for e in range(m + 1, W1 + 1):
                        if self.real[e]:
                            return e, self.o[e], e
                    return None
            return None

    def exits(self, e, E, start):
        k = (e, round(float(E), 6), start)
        if not hasattr(self, "_memo"):
            self._memo = {}
        if k not in self._memo:
            self._memo[k] = self._exits(e, E, start)
        return self._memo[k]

    def _exits(self, e, E, start):
        """-> row values (gross bp, net bp per exit, mfe30, mae30, se, E, e, dv5, sx_fallback) or None if no entry spread."""
        sb = self.spread_at(e, ENTRY_TOL, dist=True)
        if sb is None:
            return None
        sdist, se = sb
        o, h, l, c, real = self.o, self.h, self.l, self.c, self.real
        slip = (0.01 / E * 1e4) if E < 5 else 0.0
        fb = 0
        def sx(j):
            nonlocal fb
            v = self.spread_at(j, EXIT_TOL)
            if v is None:
                fb = 1
                return se
            return v
        g, n = {}, {}
        for nm, args in (("BRpct", (0.02 * E, 0.03 * E, 0.02 * E, 0.05 * E)), ("BRc", (0.05, 0.08, 0.10, 0.15))):
            px = sim_bracket(o, h, l, c, real, start, E, *args)
            g[nm] = float(np.mean([(p / E - 1) * 1e4 for p, _ in px]))
            n[nm] = g[nm] - se / 2 - float(np.mean([sx(j) for _, j in px])) / 2 - slip
        for k in (5, 15, 30):
            j = min(start + k - 1, N - 1)
            g[f"H{k}"] = (c[j] / E - 1) * 1e4
            n[f"H{k}"] = g[f"H{k}"] - se / 2 - sx(j) / 2 - slip
        for nm, j in (("T0935", 334), ("T1000", 359)):
            g[nm] = (c[j] / E - 1) * 1e4
            n[nm] = g[nm] - se / 2 - sx(j) / 2 - slip
        w = slice(start, min(start + 30, N))
        mfe = (np.max(h[w][real[w]]) / E - 1) * 1e4 if real[w].any() else 0.0
        mae = (np.min(l[w][real[w]]) / E - 1) * 1e4 if real[w].any() else 0.0
        dv5 = float(self.dv[e:e + 5].sum())
        return [g[x] for x in EXITS] + [n[x] for x in EXITS] + [mfe, mae, se, E, e, dv5, fb, sdist]


def build_nds():
    L = loose_set()
    cands = {(x["day"], x["sym"]): x for x in json.load(open(f"{OUT}/cands.json"))}
    nw = json.load(open(f"{OUT}/news.json"))
    fl = json.load(open(f"{OUT}/float.json"))
    sp = json.load(open(f"{OUT}/spreads.json"))
    if os.path.exists(f"{OUT}/spreads2.json"):
        sp.update(json.load(open(f"{OUT}/spreads2.json")))
    nds = []
    for d in sorted(L):
        K = pickle.load(open(f"{OUT}/min/{d}.pkl", "rb"))
        for s in sorted(L[d]):
            nds.append(ND(d, s, K[s], cands[(d, s)], (fl.get(s) or {}).get("float_m"),
                          [t for t, _, _ in (nw.get(d, {}).get(s) or [])], sp.get(f"{s}|{d}")))
    return nds


def analyze_main():
    nds = build_nds()
    days = sorted({x.d for x in nds})
    alld = sorted(f[:10] for f in os.listdir(f"{OUT}/min"))
    dix = {d: i for i, d in enumerate(alld)}
    print("loose name-days", len(nds), "days with any", len(days), "of", len(alld), flush=True)
    rows = []; cache = {}
    cells = {}
    miss = collections.Counter()
    for ni, x in enumerate(nds):
        qcache = {}
        for pn, (lo, hi) in PRICES.items():
            for g in GAINS:
                for vol in VOLS:
                    for rv in RVOLS:
                        for fn, fmax in FLOATS.items():
                            for nwk in NEWS:
                                q = x.qualify(lo, hi, g, vol, rv, fmax, nwk)
                                if q is None:
                                    continue
                                key = (pn, g, vol, rv, fn, nwk)
                                for en in ENTRIES:
                                    ck = (ni, en, q)
                                    if ck not in cache:
                                        cache[ck] = event_row(x, en, q, ni, dix, miss)
                                    rid = cache[ck]
                                    if rid is not None:
                                        cells.setdefault((key, en), []).append(rid)
                                    elif en == "P1":
                                        miss["cell_P1_noentry_or_nospread"] += 1
        x._memo = {}   # free memory
        if ni % 500 == 0:
            print("nd", ni, len(nds), "events", len(EV), flush=True)
    M = np.array(EV, dtype=float)
    pickle.dump({"M": M, "cells": cells, "days": alld, "miss": dict(miss),
                 "nd_meta": [(x.d, x.s, x.float, bool(x.news[QMAX]), x.rvol is not None) for x in nds]},
                open(f"{OUT}/grid.pkl", "wb"))
    print("events", M.shape, "cell-entries", len(cells), "miss", dict(miss), flush=True)


EV = []


def event_row(x, en, q, ni, dix, miss):
    if en == "RND":
        a = max(q + 1, W0)
        rs = [x.exits(e, x.o[e], e) for e in range(a, W1 + 1) if x.real[e]]
        rs = [r for r in rs if r is not None]
        if not rs:
            miss["RND_none"] += 1
            return None
        A = np.array(rs)
        r = list(A.mean(axis=0)); r[CI["e"]] = float(np.median(A[:, CI["e"]]))  # exits() row: e at same index
    else:
        ent = x.entry(en, q)
        if ent is None:
            miss[f"{en}_no_trigger"] += 1
            return None
        e, E, start = ent
        r = x.exits(e, E, start)
        if r is None:
            miss[f"{en}_no_spread"] += 1
            return None
    # row layout: exits(g,n), mfe, mae, se, E, e, [q], dv5, day, nd, fb
    row = r[:len(EXITS) * 2 + 5] + [q] + [r[len(EXITS) * 2 + 5], dix[x.d], ni, r[-2], r[-1]]
    EV.append(row)
    return len(EV) - 1


# ======================= phase: report =======================
HEAD = {
    "loosest ($2-20, >=10%, PMvol>=50k)": ("$2-20", 0.10, 50_000, 0, "any", "any"),
    "simple ($2-20, >=10%, PMvol>=250k, PM-RVOL>=5)": ("$2-20", 0.10, 250_000, 5, "any", "any"),
    "strict 5 pillars ($2-20, >=10%, >=50k, RVOL>=5, float<10M, news)": ("$2-20", 0.10, 50_000, 5, "<10M", "yes"),
}
QB = (("<07:00", 0, 180), ("07-08", 180, 240), ("08-09", 240, 300), ("09-09:25", 300, 325))
TIERS = (("$2-5", 2, 5), ("$5-10", 5, 10), ("$10-20", 10, 20.0001))


def report_main():
    Z = pickle.load(open(f"{OUT}/grid.pkl", "rb"))
    M, cells, alld = Z["M"], Z["cells"], Z["days"]
    ndays = len(alld)
    half = np.array([0 if i % 2 == 0 else 1 for i in range(ndays)])   # A = even-index sessions (train), B = odd (test)
    evh = half[M[:, CI["day"]].astype(int)]
    print(f"sessions {ndays} ({alld[0]}..{alld[-1]}); events {len(M)}; misses {Z['miss']}")
    print(f"exit-spread fallback (no mark within {EXIT_TOL} min -> entry spread) on {100*M[:, CI['sx_fallback']].mean():.1f}% of event rows")
    nr = M[:, CI["se_dist"]]
    print(f"entry-spread mark distance: 0 min {100*(nr==0).mean():.0f}%, <=2 {100*(nr<=2).mean():.0f}%, <=5 {100*(nr<=5).mean():.0f}% (RND rows average over minutes)")

    def stats(ids, ex, h=None):
        ids = np.asarray(ids, int)
        if h is not None:
            ids = ids[evh[ids] == h]
        if len(ids) == 0:
            return None
        R = M[ids]
        net = R[:, CI[f"n_{ex}"]] if ex in EXITS else None
        dd = collections.defaultdict(list)
        for di, v in zip(R[:, CI["day"]].astype(int), net):
            dd[di].append(v)
        dm = np.array([np.mean(v) for v in dd.values()])
        t = dm.mean() / (dm.std() / math.sqrt(len(dm))) if len(dm) > 2 and dm.std() > 0 else float("nan")
        return {"n": len(ids), "days": len(dd), "gross": R[:, CI[f"g_{ex}"]].mean(), "net": net.mean(),
                "win": 100 * (net > 0).mean(), "t": t, "se": np.median(R[:, CI["se"]]),
                "dv5": np.median(R[:, CI["dv5"]]), "e": np.median(R[:, CI["e"]]), "q": np.median(R[:, CI["q"]]),
                "E": np.median(R[:, CI["E"]]), "mfe": np.median(R[:, CI["mfe30"]]), "mae": np.median(R[:, CI["mae30"]])}

    def fmt(s, lab=""):
        if not s:
            return f"{lab:34s} n=0"
        flag = " THIN($5k>10%)" if s["dv5"] < 50_000 else (" thin($5k>5%)" if s["dv5"] < 100_000 else "")
        return (f"{lab:34s} n={s['n']:5d} d={s['days']:3d} gross={s['gross']:7.0f} net={s['net']:7.0f} t={s['t']:5.1f} "
                f"win={s['win']:4.0f}% spr={s['se']:4.0f} px={s['E']:5.2f} q={hm(s['q'])} e={hm(s['e'])} "
                f"mfe30={s['mfe']:5.0f} mae30={s['mae']:6.0f} dv5=${s['dv5']/1e3:6.0f}k{flag}")

    ntests = len(cells) * len(EXITS)
    print(f"grid: {len({k for k, _ in cells})} qualifying cells x {len(ENTRIES)} entries x {len(EXITS)} exits = {ntests} tests (cell-entry pairs with events: {len(cells)})")

    for lab, key in HEAD.items():
        print(f"\n######## {lab}")
        ids = cells.get((key, "P1"), [])
        ndp = len({int(M[i, CI['nd']]) for i in ids})
        perday = collections.Counter(int(M[i, CI["day"]]) for i in ids)
        print(f"P1 name-days {ndp} = {ndp/ndays:.2f}/day; days with >=3: {sum(1 for v in perday.values() if v >= 3)}/{ndays}")
        for en in ENTRIES:
            ids = cells.get((key, en), [])
            for ex in EXITS:
                a, b = stats(ids, ex, 0), stats(ids, ex, 1)
                print(fmt(a, f"{en} {ex} A") + "\n" + fmt(b, f"{en} {ex} B"))
        for en in ("P1", "P2", "RND"):
            ids = np.asarray(cells.get((key, en), []), int)
            if not len(ids):
                continue
            print(f"  -- {en} by qualify-time bucket (pooled A+B; H15 / T0935)")
            for qn, a0, a1 in QB:
                sel = ids[(M[ids, CI["q"]] >= a0) & (M[ids, CI["q"]] < a1)]
                print("   ", fmt(stats(sel, "H15"), f"q {qn} H15"), "|", fmt(stats(sel, "T0935"), "T0935")[34:80])
            print(f"  -- {en} by price tier at entry (pooled; H15 / T0935)")
            for tn, p0, p1 in TIERS:
                sel = ids[(M[ids, CI["E"]] >= p0) & (M[ids, CI["E"]] < p1)]
                print("   ", fmt(stats(sel, "H15"), f"{tn} H15"), "|", fmt(stats(sel, "T0935"), "T0935")[34:80])
            print(f"  -- {en} by entry time (pooled; H15)")
            for qn, a0, a1 in (("07-08", 180, 240), ("08-09", 240, 300), ("09-09:25", 300, 326)):
                sel = ids[(M[ids, CI["e"]] >= a0) & (M[ids, CI["e"]] < a1)]
                print("   ", fmt(stats(sel, "H15"), f"entry {qn} H15"))

    print("\n######## TRAIN(A)-selected best cell per entry x exit (A n>=30), shown with TEST(B)")
    summ = []
    for en in ENTRIES:
        for ex in EXITS:
            best = None; posboth = 0; nboth = 0; bt = []
            for (key, e2), ids in cells.items():
                if e2 != en:
                    continue
                a = stats(ids, ex, 0)
                if not a or a["n"] < 30:
                    continue
                b = stats(ids, ex, 1)
                if b and b["n"] >= 30:
                    nboth += 1; posboth += (a["net"] > 0 and b["net"] > 0)
                    if b["t"] == b["t"]:
                        bt.append((b["t"], key))
                if best is None or a["net"] > best[1]["net"]:
                    best = (key, a, b)
            if best:
                print(f"{en} {ex} best-A cell {best[0]}\n   " + fmt(best[1], "A") + "\n   " + fmt(best[2], "B"))
                mx = max(bt) if bt else None
                print(f"   cells n>=30 both halves {nboth}; net>0 in both {posboth}; max test t {round(mx[0], 2) if mx else None} ({mx[1] if mx else ''})")
                summ.append((en, ex, best[0], best[1]["net"], best[2]["net"] if best[2] else None, posboth, nboth))
    pickle.dump(summ, open(f"{OUT}/summ.pkl", "wb"))

    print("\n######## gross positive? cells with pooled GROSS > 0 and n>=60 (H15), by entry")
    for en in ENTRIES:
        gp = []
        for (key, e2), ids in cells.items():
            if e2 == en and len(ids) >= 60:
                s = stats(ids, "H15")
                gp.append(s["gross"])
        if gp:
            print(f"{en}: cells {len(gp)}; gross>0 {sum(1 for g in gp if g > 0)}; median gross {np.median(gp):.0f}; max {max(gp):.0f}")

    print("\n######## days with >=3 tradeable names (qualified by 09:25 via P1 entry)")
    for lab, key in HEAD.items():
        ids = np.asarray(cells.get((key, "P1"), []), int)
        for cond, f in (("all", np.ones(len(ids), bool)),
                        ("spread<=100bp", M[ids, CI["se"]] <= 100),
                        ("spread<=100bp & dv5>=$50k", (M[ids, CI["se"]] <= 100) & (M[ids, CI["dv5"]] >= 50_000)),
                        ("spread<=50bp & dv5>=$100k", (M[ids, CI["se"]] <= 50) & (M[ids, CI["dv5"]] >= 100_000))):
            pd_ = collections.Counter(int(M[i, CI["day"]]) for i in ids[f])
            print(f"{lab[:40]:40s} {cond:28s} names/day {f.sum()/ndays:5.2f}  days>=3 {sum(1 for v in pd_.values() if v >= 3):3d}/{ndays}  days>=1 {len(pd_)}")
        # tradeable subset results
        f = (M[ids, CI["se"]] <= 100) & (M[ids, CI["dv5"]] >= 50_000)
        for ex in ("H15", "T0935", "BRpct"):
            print("   tradeable subset P1", fmt(stats(ids[f], ex, 0), f"{ex} A"), "\n   tradeable subset P1", fmt(stats(ids[f], ex, 1), f"{ex} B"))


if __name__ == "__main__":
    mode = sys.argv[1]
    {"scan": lambda: scan_main(), "cands": cands_main, "minute": minute_main, "loose": loose_set, "news": news_main,
     "float": float_main, "quotes": quotes_main, "analyze": analyze_main, "report": report_main}[mode]()
