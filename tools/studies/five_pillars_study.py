"""Five Pillars study (Ross Cameron name criteria) -- 2026-09-27.

Studies-only, read-only on the desk. All outputs go to /tmp/fp. Free Alpaca SIP data (keys via config.load_config()).
Phases (run in order):
  daily    universe of tradable common stocks, daily bars, candidate name-days (prior close $1-25, daily high >= +10%)
  minute   1-min SIP bars 04:00-11:31 for candidates; keep name-days >= +10% by 11:00 at $1-25; volume curve; "loose" flag
  sn       Alpaca historical news (benzinga) prior-day 16:00 -> 11:00, then SIP quoted spreads at 09:31/09:45/10:15
  float    current float (desk float_cache read-only, else float_feed/Finnhub) -> /tmp/fp/float.json
  analyze  point-in-time qualify minute per threshold cell; outcomes 09:30-11:00 (qbuy / EXH %R -50 cross / random); grid
  report   print tables
Usage: PYTHONPATH=/tmp:$PWD:$PWD/tools:$PWD/tools/studies python tools/studies/five_pillars_study.py <phase>
Needs tools/studies/momentum_runway_study.py (crosses_on).  See docs/studies/FIVE_PILLARS_2026-09-27.md.
"""


# ======================= phase: daily =======================

import json, os, pickle, sys, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
OUT = "/tmp/fp"
START, END = "2025-09-01", "2026-09-26"


def daily_main():
    from config import load_config
    from ticker_filters import is_common, is_levered_etp
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    cfg = load_config() or {}
    api, sec = cfg.get("api_key"), cfg.get("secret_key")
    ap = f"{OUT}/assets.json"
    if os.path.exists(ap):
        syms = json.load(open(ap))
    else:
        assets = None
        for paper in (True, False):
            try:
                assets = TradingClient(api, sec, paper=paper).get_all_assets(
                    GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE))
                break
            except Exception as e:  # noqa: BLE001
                print("assets", type(e).__name__)
        ok_ex = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
        syms = sorted(a.symbol for a in assets if a.tradable and str(a.exchange).split(".")[-1] in ok_ex
                      and is_common(a.symbol) and not is_levered_etp(a.symbol, a.name or ""))
        json.dump(syms, open(ap, "w"))
    print("universe", len(syms), flush=True)
    cl = StockHistoricalDataClient(api, sec)
    dp = f"{OUT}/daily.pkl"
    daily = pickle.load(open(dp, "rb")) if os.path.exists(dp) else {}
    todo = [s for s in syms if s not in daily]
    for i in range(0, len(todo), 200):
        b = todo[i:i + 200]
        try:
            bs = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=b, timeframe=TimeFrame.Day,
                                                    start=datetime.fromisoformat(START).replace(tzinfo=ET),
                                                    end=datetime.fromisoformat(END).replace(tzinfo=ET),
                                                    feed=DataFeed.SIP, adjustment="split"))
            for s, rows in (bs.data or {}).items():
                daily[s] = [(r.timestamp.astimezone(ET).strftime("%Y-%m-%d"), float(r.open), float(r.high),
                             float(r.low), float(r.close), float(r.volume)) for r in rows]
            for s in b:
                daily.setdefault(s, [])
        except Exception as e:  # noqa: BLE001
            print("daily", i, type(e).__name__, str(e)[:100], flush=True)
            time.sleep(5)
        if i % 2000 == 0:
            print("daily", i, len(todo), flush=True)
            pickle.dump(daily, open(dp, "wb"))
        time.sleep(0.3)
    pickle.dump(daily, open(dp, "wb"))
    # candidates: prior close $1-25, day high >= +10% vs prior close (or open >= +10%), plus ADV20
    cands = []
    for s, rows in daily.items():
        for k in range(21, len(rows)):
            d, o, h, l, c, v = rows[k]
            pc = rows[k - 1][4]
            if not (1.0 <= pc <= 25.0) or pc <= 0:
                continue
            if h / pc - 1 < 0.10:
                continue
            adv = sum(r[5] for r in rows[k - 20:k]) / 20.0
            cands.append({"day": d, "sym": s, "pc": pc, "adv20": adv, "o": o, "h": h, "c": c, "v": v})
    json.dump(cands, open(f"{OUT}/cands.json", "w"))
    days = sorted({x["day"] for x in cands})
    print("candidate name-days", len(cands), "days", len(days), days[:1], days[-1:], flush=True)



# ======================= phase: minute =======================

import json, os, pickle, sys, time, statistics, collections
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import numpy as np
ET = ZoneInfo("America/New_York")
OUT = "/tmp/fp"
N = 452  # grid minutes 04:00 .. 11:31 (bar start)


def curve():
    """f[j] = median cumulative volume from 04:00 through grid minute j / that day's daily-bar volume,
    over non-mover name-days (|day chg| < 3%) in /tmp/momrun/bars.pkl."""
    p = f"{OUT}/curve.npy"
    if os.path.exists(p):
        return np.load(p)
    bars = pickle.load(open("/tmp/momrun/bars.pkl", "rb"))
    daily = pickle.load(open(f"{OUT}/daily.pkl", "rb"))
    rows = []
    for (s, d), rec in bars.items():
        if not rec or not rec[0]:
            continue
        dr = {x[0]: x for x in daily.get(s, [])}
        if d not in dr or not rec[1]:
            continue
        dv = dr[d][5]; chg = dr[d][4] / rec[1] - 1
        if dv <= 0 or abs(chg) > 0.03:
            continue
        t, o, h, l, c, v = rec[0]
        d0 = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET, hour=4).timestamp()
        g = np.zeros(N)
        for k in range(len(t)):
            j = int((t[k] - d0) // 60)
            if 0 <= j < N:
                g[j] += v[k]
        rows.append(np.cumsum(g) / dv)
    f = np.median(np.array(rows), axis=0)
    np.save(p, f)
    print("curve from", len(rows), "non-mover name-days; f(09:29)=%.4f f(09:59)=%.4f f(10:59)=%.4f" % (f[329], f[359], f[419]))
    return f


def minute_main():
    from config import load_config
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    cfg = load_config() or {}
    cl = StockHistoricalDataClient(cfg.get("api_key"), cfg.get("secret_key"))
    f = np.maximum(curve(), 0.001)
    cands = json.load(open(f"{OUT}/cands.json"))
    byday = collections.defaultdict(list)
    for x in cands:
        byday[x["day"]].append(x)
    os.makedirs(f"{OUT}/min", exist_ok=True)
    stats = collections.Counter()
    for d in sorted(byday):
        fp = f"{OUT}/min/{d}.pkl"
        if os.path.exists(fp):
            continue
        dd = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET)
        d0 = dd.replace(hour=4).timestamp()
        xs = {x["sym"]: x for x in byday[d]}
        syms = sorted(xs)
        keep = {}
        for i in range(0, len(syms), 50):
            b = syms[i:i + 50]
            try:
                bs = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=b, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                                                        start=dd.replace(hour=4).astimezone(timezone.utc),
                                                        end=dd.replace(hour=11, minute=32).astimezone(timezone.utc),
                                                        feed=DataFeed.SIP, adjustment="split"))
                data = bs.data or {}
            except Exception as e:  # noqa: BLE001
                print("bars", d, i, type(e).__name__, str(e)[:100], flush=True)
                time.sleep(3)
                continue
            for s, rows in data.items():
                G = np.full((6, N), np.nan, dtype=np.float32)
                for r in rows:
                    j = int((r.timestamp.timestamp() - d0) // 60)
                    if 0 <= j < N:
                        G[:, j] = (r.open, r.high, r.low, r.close, r.volume, 1)
                real = ~np.isnan(G[5])
                # forward fill o/h/l/c, v=0 on empty minutes
                last = np.nan
                for j in range(N):
                    if real[j]:
                        last = G[3, j]
                    else:
                        G[0:4, j] = last; G[4, j] = 0; G[5, j] = 0
                x = xs[s]
                cum = np.nancumsum(G[4])
                rvol = cum / (x["adv20"] * f) if x["adv20"] > 0 else np.full(N, np.nan)
                gain = G[3] / x["pc"] - 1
                dec = np.arange(N) <= 420  # bar start <= 11:00 -> decision by 11:01
                loose = dec & real & (G[3] >= 2) & (G[3] <= 20) & (gain >= 0.10) & (rvol >= 2)
                hit10 = (dec & real & (gain >= 0.10) & (G[3] >= 1) & (G[3] <= 25)).any()
                stats["bars"] += 1; stats["hit10_by11"] += bool(hit10); stats["loose"] += bool(loose.any())
                if hit10:
                    keep[s] = {"G": G, "pc": x["pc"], "adv20": x["adv20"], "loose": bool(loose.any())}
            time.sleep(0.3)
        pickle.dump(keep, open(fp, "wb"))
        print(d, "cands", len(syms), "kept", len(keep), dict(stats), flush=True)



# ======================= phase: aux =======================

import json, os, pickle, sys, time, collections
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
OUT = "/tmp/fp"
SAMPLES = ((9, 31), (9, 45), (10, 15))


def loose_namedays():
    out = collections.defaultdict(list)
    for f in sorted(os.listdir(f"{OUT}/min")):
        d = f[:10]
        k = pickle.load(open(f"{OUT}/min/{f}", "rb"))
        out[d] = sorted(s for s, v in k.items() if v["loose"])
    return out


def cfg():
    from config import load_config
    c = load_config() or {}
    return c.get("api_key"), c.get("secret_key")


def spreads(nd):
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockQuotesRequest
    from alpaca.data.enums import DataFeed
    cl = StockHistoricalDataClient(*cfg())
    p = f"{OUT}/spreads.json"
    sp = json.load(open(p)) if os.path.exists(p) else {}
    for d, syms in sorted(nd.items()):
        dd = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET)
        for hh, mm in SAMPLES:
            todo = [s for s in syms if f"{s}|{d}|{hh:02d}{mm:02d}" not in sp]
            t0 = dd.replace(hour=hh, minute=mm)
            acc = collections.defaultdict(list)
            for i in range(0, len(todo), 10):
                b = todo[i:i + 10]
                try:
                    q = cl.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=b, start=t0.astimezone(timezone.utc),
                                                               end=(t0 + timedelta(seconds=15)).astimezone(timezone.utc),
                                                               feed=DataFeed.SIP, limit=20000))
                    for s, rows in (q.data or {}).items():
                        for r in rows:
                            bb, a = float(r.bid_price), float(r.ask_price)
                            if bb > 0 and a >= bb:
                                acc[s].append((a - bb) / ((a + bb) / 2) * 1e4)
                except Exception as e:  # noqa: BLE001
                    print("quotes", d, type(e).__name__, str(e)[:80], flush=True)
                    time.sleep(2)
                time.sleep(0.3)
            for s in todo:
                v = sorted(acc.get(s) or [])
                sp[f"{s}|{d}|{hh:02d}{mm:02d}"] = v[len(v) // 2] if v else None
        json.dump(sp, open(p, "w"))
        print("spreads", d, len(syms), flush=True)


def news(nd):
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest
    nc = NewsClient(*cfg())
    p = f"{OUT}/news.json"
    nw = json.load(open(p)) if os.path.exists(p) else {}
    days = sorted(nd)
    alld = sorted({x["day"] for x in json.load(open(f"{OUT}/cands.json"))})
    for d in days:
        if d in nw:
            continue
        prev = alld[alld.index(d) - 1] if alld.index(d) > 0 else (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=3 if datetime.strptime(d, "%Y-%m-%d").weekday() == 0 else 1)).strftime("%Y-%m-%d")
        st = datetime.strptime(prev, "%Y-%m-%d").replace(tzinfo=ET, hour=16)
        en = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET, hour=11)
        got = collections.defaultdict(list)
        syms = nd[d]
        for i in range(0, len(syms), 40):
            b = syms[i:i + 40]
            tok = None
            for _ in range(20):
                try:
                    kw = dict(symbols=",".join(b), start=st, end=en, limit=50, include_content=False)
                    if tok:
                        kw["page_token"] = tok
                    r = nc.get_news(NewsRequest(**kw))
                except Exception as e:  # noqa: BLE001
                    print("news", d, type(e).__name__, str(e)[:80], flush=True)
                    time.sleep(2)
                    break
                items = r.data["news"] if isinstance(r.data, dict) else r.data
                for it in items:
                    for s in (it.symbols or []):
                        if s in b:
                            got[s].append([it.created_at.timestamp(), (it.headline or "")[:120], it.source])
                tok = getattr(r, "next_page_token", None)
                time.sleep(0.3)
                if not tok:
                    break
        nw[d] = got
        json.dump(nw, open(p, "w"))
        print("news", d, len(syms), "with news", len(got), flush=True)


def floats(nd):
    import float_feed as ff
    key = ff._api_key()
    p = f"{OUT}/float.json"
    fl = json.load(open(p)) if os.path.exists(p) else {}
    desk = ff.load_cache() or {}
    syms = sorted({s for v in nd.values() for s in v})
    for s in syms:
        if s in fl:
            continue
        row = desk.get(s)
        if isinstance(row, dict) and row.get("float_m") is not None:
            fl[s] = {"float_m": row["float_m"], "src": "desk_cache", "ts": row.get("ts")}
            continue
        r = ff._fetch_one(s, key)
        if r is ff.RATE_LIMITED:
            time.sleep(60)
            r = ff._fetch_one(s, key)
        if isinstance(r, dict):
            fl[s] = {"float_m": r.get("float_m"), "shares_out": r.get("shares_out"), "src": "finnhub_now", "ts": time.time()}
        else:
            fl[s] = {"float_m": None, "src": "fail"}
        if len(fl) % 100 == 0:
            json.dump(fl, open(p, "w"))
            print("float", len(fl), len(syms), flush=True)
        time.sleep(1.1)
    json.dump(fl, open(p, "w"))
    print("float done", len(fl), flush=True)



# ======================= phase: analyze =======================

import json, os, pickle, sys, math, statistics, collections, itertools
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv
import momentum_runway_study as S
ET = ZoneInfo("America/New_York")
OUT = "/tmp/fp"
N = 452
J0, J1 = 329, 419          # decision bar starts 09:29..10:59 -> entries 09:30..11:00 open
PRICES = {"$2-5": (2, 5), "$3-8": (3, 8), "$5-10": (5, 10), "$10-20": (10, 20), "$2-20": (2, 20)}
GAINS = (0.10, 0.20, 0.30)
RVOLS = (2, 5, 10)
FLOATS = {"<10M": 10, "<20M": 20, "<50M": 50, "any": None}
CATS = ("any", "yes", "no")
DETAIL = {("$2-20", 0.10, 5, "<10M", "yes"), ("$2-20", 0.10, 2, "any", "any"), ("$2-20", 0.10, 5, "any", "any"), ("$2-20", 0.10, 5, "<10M", "any")}
SAMPLE_MIN = {"0931": 331, "0945": 345, "1015": 375}  # grid index of sample time


def outcomes(G, spv):
    o, h, l, c, v, real = [G[k].astype(float) for k in range(6)]
    half = spv / 2.0
    js = np.arange(J0, J1 + 1)
    E = o[js + 1] * (1 + half[js] / 1e4)
    ok = real[js + 1] == 1
    W = 30
    hw = np.array([h[j + 1:j + 1 + W] for j in js]); lw = np.array([l[j + 1:j + 1 + W] for j in js])
    R = {}
    with np.errstate(invalid="ignore", divide="ignore"):
        R["mfe30"] = (np.nanmax(hw, axis=1) / E - 1) * 1e4
        sb = lw <= (E * (1 - 35 / 1e4))[:, None]
        fs = np.where(sb.any(axis=1), sb.argmax(axis=1), 999)
        for X in (20, 50):
            tb = hw >= (E * (1 + X / 1e4))[:, None]
            ft = np.where(tb.any(axis=1), tb.argmax(axis=1), 999)
            R[f"h{X}"] = ((ft < 999) & (ft < fs)).astype(float)
        tb = lw <= (E * (1 - 50 / 1e4))[:, None]
        ft = np.where(tb.any(axis=1), tb.argmax(axis=1), 999)
        sb2 = hw >= (E * (1 + 35 / 1e4))[:, None]
        fs2 = np.where(sb2.any(axis=1), sb2.argmax(axis=1), 999)
        R["rev50"] = ((ft < 999) & (ft < fs2)).astype(float)
        R["end30"] = (c[js + 30] / E - 1 - half[js] / 1e4) * 1e4
        R["gross30"] = (c[js + 30] / o[js + 1] - 1) * 1e4
        hold = (c[419] / E - 1 - half[js] / 1e4) * 1e4
        hold[js + 1 > 419] = np.nan
        R["hold11"] = hold
        R["spread"] = spv[js]
    for k in R:
        R[k] = np.where(ok, R[k], np.nan)
    return R, ok


def analyze_main():
    sp = json.load(open(f"{OUT}/spreads.json"))
    nw = json.load(open(f"{OUT}/news.json"))
    fl = json.load(open(f"{OUT}/float.json"))
    days = sorted(f[:10] for f in os.listdir(f"{OUT}/min"))
    A = set(days[0::2])
    recs = []
    miss = collections.Counter()
    for d in days:
        K = pickle.load(open(f"{OUT}/min/{d}.pkl", "rb"))
        d0 = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET, hour=4).timestamp()
        for s, x in K.items():
            if not x["loose"]:
                continue
            G = x["G"]
            pts = [(SAMPLE_MIN[k], sp.get(f"{s}|{d}|{k}")) for k in SAMPLE_MIN]
            pts = [(m, v) for m, v in pts if v is not None and v > 0]
            if not pts:
                miss["no_spread"] += 1
                continue
            spv = np.array([min(pts, key=lambda p: abs(p[0] - j))[1] for j in range(N)])
            R, ok = outcomes(G, spv)
            c = G[3].astype(float); real = G[5] == 1
            cum = np.nancumsum(G[4].astype(float))
            f = np.maximum(np.load(f"{OUT}/curve.npy"), 0.001)
            rvol = cum / (x["adv20"] * f) if x["adv20"] > 0 else np.full(N, np.nan)
            gain = c / x["pc"] - 1
            nts = sorted(t for t, _, _ in (nw.get(d, {}).get(s) or []))
            dec_t = d0 + (np.arange(N) + 1) * 60
            has_news = np.array([bool(nts) and nts[0] <= t for t in dec_t])
            fm = (fl.get(s) or {}).get("float_m")
            if fm is None:
                miss["no_float"] += 1
            cx = np.zeros(J1 - J0 + 1, bool)
            for i in S.crosses_on(G.astype(float)):
                if J0 <= i <= J1:
                    cx[i - J0] = True
            recs.append({"day": d, "sym": s, "half": "A" if d in A else "B", "c": c, "real": real, "rvol": rvol,
                         "gain": gain, "news": has_news, "float": fm, "R": R, "ok": ok, "cx": cx,
                         "any_news": bool(nts)})
    print("name-days", len(recs), "missing", dict(miss), "float coverage",
          sum(r["float"] is not None for r in recs), flush=True)
    pickle.dump({"n": len(recs)}, open(f"{OUT}/an_meta.pkl", "wb"))
    # qualify minute per (price, gain, rvol, cat) per record
    jidx = np.arange(421)
    keys = list(itertools.product(PRICES, GAINS, RVOLS, CATS))
    for r in recs:
        q = {}
        base = r["real"][:421]
        for pn, (lo, hi) in PRICES.items():
            pm = base & (r["c"][:421] >= lo) & (r["c"][:421] <= hi)
            for g in GAINS:
                gm = pm & (r["gain"][:421] >= g)
                for rv in RVOLS:
                    m = gm & (r["rvol"][:421] >= rv)
                    for ct in CATS:
                        mm = m if ct == "any" else (m & r["news"][:421] if ct == "yes" else m & ~r["news"][:421])
                        q[(pn, g, rv, ct)] = int(np.argmax(mm)) if mm.any() else None
        r["q"] = q
        # suffix sums for random-minute aggregation
        R = r["R"]
        valid = ~np.isnan(R["end30"])
        r["suf"] = {k: np.concatenate([np.cumsum(np.nan_to_num(R[k])[::-1])[::-1], [0]]) for k in ("end30", "gross30", "h50", "h20", "rev50", "mfe30")}
        r["sufn"] = np.concatenate([np.cumsum(valid[::-1])[::-1], [0]])
        hv = ~np.isnan(R["hold11"])
        r["sufh"] = np.concatenate([np.cumsum(np.nan_to_num(R["hold11"])[::-1])[::-1], [0]])
        r["sufhn"] = np.concatenate([np.cumsum(hv[::-1])[::-1], [0]])
    # aggregate cells
    results = []
    DET = {}
    for (pn, g, rv, ct) in keys:
        for fn, fmax in FLOATS.items():
            agg = {t: {h: collections.defaultdict(lambda: [0.0, 0, 0.0, 0, 0.0, 0.0, 0.0, 0.0, 0.0]) for h in "AB"} for t in ("rand", "cross", "qbuy")}
            qts = []; nds = {"A": 0, "B": 0}; perday = collections.Counter()
            for r in recs:
                if fmax is not None and (r["float"] is None or r["float"] >= fmax):
                    continue
                qj = r["q"][(pn, g, rv, ct)]
                if qj is None:
                    continue
                qts.append(qj); nds[r["half"]] += 1; perday[r["day"]] += 1
                st = max(qj, J0) - J0
                if st > J1 - J0:
                    continue  # qualified at 11:00 exactly
                R = r["R"]; h = r["half"]; d = r["day"]
                # random minutes: suffix sums
                a = agg["rand"][h][d]
                n = r["sufn"][st]
                a[0] += r["suf"]["end30"][st]; a[1] += n; a[2] += r["sufh"][st]; a[3] += r["sufhn"][st]
                a[4] += r["suf"]["h50"][st]; a[5] += r["suf"]["rev50"][st]; a[6] += r["suf"]["gross30"][st]; a[7] += r["suf"]["h20"][st]; a[8] += r["suf"]["mfe30"][st]
                # crosses
                sel = np.where(r["cx"][st:] & ~np.isnan(R["end30"][st:]))[0] + st
                if len(sel):
                    a = agg["cross"][h][d]
                    a[0] += float(np.sum(R["end30"][sel])); a[1] += len(sel)
                    hs = sel[~np.isnan(R["hold11"][sel])]
                    a[2] += float(np.sum(R["hold11"][hs])); a[3] += len(hs)
                    a[4] += float(np.sum(R["h50"][sel])); a[5] += float(np.sum(R["rev50"][sel])); a[6] += float(np.sum(R["gross30"][sel])); a[7] += float(np.sum(R["h20"][sel])); a[8] += float(np.nansum(R["mfe30"][sel]))
                # qualify buy
                if not np.isnan(R["end30"][st]):
                    a = agg["qbuy"][h][d]
                    a[0] += R["end30"][st]; a[1] += 1
                    if not np.isnan(R["hold11"][st]):
                        a[2] += R["hold11"][st]; a[3] += 1
                    a[4] += R["h50"][st]; a[5] += R["rev50"][st]; a[6] += R["gross30"][st]; a[7] += R["h20"][st]; a[8] += np.nan_to_num(R["mfe30"][st])
            if not qts:
                continue
            if (pn, g, rv, fn, ct) in DETAIL:
                DET[(pn, g, rv, fn, ct)] = [(r["day"], r["sym"], r["q"][(pn, g, rv, ct)], float(np.nanmedian(r["R"]["spread"])), r["c"][r["q"][(pn, g, rv, ct)]]) for r in recs
                          if r["q"][(pn, g, rv, ct)] is not None and not (fmax is not None and (r["float"] is None or r["float"] >= fmax))]
            row = {"cell": (pn, g, rv, fn, ct), "nd": nds, "names_per_day": round((nds["A"] + nds["B"]) / len(days), 2),
                   "days_with": len(perday), "q_pre_share": round(100 * sum(1 for j in qts if j < 330) / len(qts), 1),
                   "q_med": int(statistics.median(qts))}
            for t in agg:
                for h in "AB":
                    D = agg[t][h]
                    n = sum(v[1] for v in D.values())
                    if n == 0:
                        row[f"{t}_{h}"] = None
                        continue
                    dm = [v[0] / v[1] for v in D.values() if v[1] > 0]
                    dh = [v[2] / v[3] for v in D.values() if v[3] > 0]
                    tt = lambda xs: (statistics.mean(xs) / (statistics.pstdev(xs) / math.sqrt(len(xs)))) if len(xs) > 2 and statistics.pstdev(xs) > 0 else None
                    row[f"{t}_{h}"] = {"n": n, "days": len(dm), "end30": sum(v[0] for v in D.values()) / n,
                                       "hold11": (sum(v[2] for v in D.values()) / max(1, sum(v[3] for v in D.values()))),
                                       "gross30": sum(v[6] for v in D.values()) / n, "mfe30": sum(v[8] for v in D.values()) / n,
                                       "spread": None,
                                       "h20": 100 * sum(v[7] for v in D.values()) / n,
                                       "h50": 100 * sum(v[4] for v in D.values()) / n, "rev50": 100 * sum(v[5] for v in D.values()) / n,
                                       "t_end30": tt(dm), "t_hold11": tt(dh)}
            results.append(row)
    pickle.dump(results, open(f"{OUT}/grid.pkl", "wb"))
    cov = {"recs": len(recs), "float_known": sum(r["float"] is not None for r in recs), "any_news": sum(r["any_news"] for r in recs),
           "miss": dict(miss), "days": days, "spread_med": float(np.median([np.nanmedian(r["R"]["spread"]) for r in recs]))}
    pickle.dump({"det": DET, "cov": cov}, open(f"{OUT}/detail.pkl", "wb"))
    print("cells", len(results))



# ======================= phase: report =======================
def report():

    import pickle, statistics, collections, math
    import numpy as np
    OUT = "/tmp/fp"
    G = pickle.load(open(f"{OUT}/grid.pkl", "rb"))
    D = pickle.load(open(f"{OUT}/detail.pkl", "rb"))
    cov = D["cov"]; ndays = len(cov["days"])
    print("COVERAGE", {k: v for k, v in cov.items() if k != "days"}, "days", ndays, cov["days"][0], cov["days"][-1])
    print("cells", len(G))


    def hm(j):
        t = 240 + j  # minutes after midnight for bar start
        return f"{t // 60:02d}:{t % 60:02d}"


    def fmt(x, k):
        return "   -  " if x is None or x.get(k) is None else f"{x[k]:6.1f}"


    def line(r, t):
        a, b = r.get(f"{t}_A"), r.get(f"{t}_B")
        c = r["cell"]
        nm = f"{c[0]:>6} >={int(c[1]*100):2d}% rv>={c[2]:<2d} fl{c[3]:>5} cat={c[4]:<3}"
        def part(x):
            if not x:
                return "n=0"
            te = x["t_end30"]; th = x["t_hold11"]
            return (f"n={x['n']:6d} d={x['days']:3d} mfe={x['mfe30']:6.0f} g30={x['gross30']:6.0f} e30={x['end30']:6.0f}"
                    f" t={te if te is None else round(te,1)} h11={x['hold11']:6.0f} t={th if th is None else round(th,1)}"
                    f" h20={x['h20']:4.1f} h50={x['h50']:4.1f} rev50={x['rev50']:4.1f}")
        return f"{nm} nd/day={r['names_per_day']:5.2f} pre%={r['q_pre_share']:5.1f} qmed={hm(r['q_med'])} | A: {part(a)} | B: {part(b)}"


    for t in ("qbuy", "cross", "rand"):
        ok = [r for r in G if r.get(f"{t}_A") and r.get(f"{t}_B")]
        print(f"\n==== {t}: top 12 by TRAIN (A) end30 net, test B shown (cells with A n>=30)")
        tr = sorted([r for r in ok if r[f"{t}_A"]["n"] >= 30], key=lambda r: -r[f"{t}_A"]["end30"])[:12]
        for r in tr:
            print(line(r, t))
        print(f"---- {t}: top 12 by TRAIN hold11 net")
        for r in sorted([r for r in ok if r[f"{t}_A"]["n"] >= 30], key=lambda r: -r[f"{t}_A"]["hold11"])[:12]:
            print(line(r, t))
        print(f"---- {t}: top 12 by TEST (B) end30 net (selection on test => optimistic; B n>=30)")
        for r in sorted([r for r in ok if r[f"{t}_B"]["n"] >= 30], key=lambda r: -r[f"{t}_B"]["end30"])[:12]:
            print(line(r, t))
        print(f"---- {t}: top 12 by TEST hold11 net")
        for r in sorted([r for r in ok if r[f"{t}_B"]["n"] >= 30], key=lambda r: -r[f"{t}_B"]["hold11"])[:12]:
            print(line(r, t))
        # how many cells positive in both halves / |t|
        both = [r for r in ok if r[f"{t}_A"]["n"] >= 30 and r[f"{t}_B"]["n"] >= 30]
        pe = sum(1 for r in both if r[f"{t}_A"]["end30"] > 0 and r[f"{t}_B"]["end30"] > 0)
        ph = sum(1 for r in both if r[f"{t}_A"]["hold11"] > 0 and r[f"{t}_B"]["hold11"] > 0)
        ts = [r[f"{t}_B"]["t_hold11"] for r in both if r[f"{t}_B"]["t_hold11"] is not None]
        print(f"---- {t}: cells n>=30 both halves {len(both)}; end30>0 both {pe}; hold11>0 both {ph}; max test t_hold11 {max(ts) if ts else None}")
        # train-selected -> test result correlation
        if both:
            xa = np.array([r[f"{t}_A"]["hold11"] for r in both]); xb = np.array([r[f"{t}_B"]["hold11"] for r in both])
            print(f"     corr(A,B) hold11 across cells {np.corrcoef(xa, xb)[0,1]:.2f}; end30 corr "
                  f"{np.corrcoef([r[f'{t}_A']['end30'] for r in both], [r[f'{t}_B']['end30'] for r in both])[0,1]:.2f}")

    # marginal effects: average over cells of pooled (A+B) end30/hold11 per level, qbuy and rand
    def pooled(r, t, k):
        a, b = r.get(f"{t}_A"), r.get(f"{t}_B")
        xs = [x for x in (a, b) if x]
        n = sum(x["n"] for x in xs)
        return (sum(x[k] * x["n"] for x in xs) / n, n) if n else (None, 0)

    print("\n==== MARGINAL: each threshold level, others at loosest ($2-20, >=10%, rv>=2, float any, cat any); pooled halves")
    base = ("$2-20", 0.10, 2, "any", "any")
    idx = {r["cell"]: r for r in G}
    vary = [(0, ["$2-5", "$3-8", "$5-10", "$10-20", "$2-20"]), (1, [0.10, 0.20, 0.30]), (2, [2, 5, 10]), (3, ["<10M", "<20M", "<50M", "any"]), (4, ["any", "yes", "no"])]
    for pos, lev in vary:
        for L in lev:
            c = list(base); c[pos] = L; r = idx.get(tuple(c))
            if not r:
                print(tuple(c), "none"); continue
            out = []
            for t in ("qbuy", "cross", "rand"):
                e, n = pooled(r, t, "end30"); h, _ = pooled(r, t, "hold11"); g, _ = pooled(r, t, "gross30")
                out.append(f"{t}: n={n} e30={e if e is None else round(e)} g30={g if g is None else round(g)} h11={h if h is None else round(h)}")
            print(f"{str(tuple(c)):45s} nd/day={r['names_per_day']:5.2f} pre%={r['q_pre_share']:5.1f} qmed={hm(r['q_med'])} | " + " | ".join(out))

    print("\n==== STRICT and reference cells (all t)")
    for c in [("$2-20", 0.10, 5, "<10M", "yes"), ("$2-20", 0.10, 5, "<10M", "any"), ("$2-20", 0.10, 5, "any", "any"), ("$2-20", 0.10, 2, "any", "any")]:
        r = idx.get(c)
        if r:
            for t in ("qbuy", "cross", "rand"):
                print(t, line(r, t))

    print("\n==== STRICT cell detail: names/day, qualify-time, concurrency")
    for c, rows in D["det"].items():
        per = collections.defaultdict(list)
        for d, s, q, spm, px in rows:
            per[d].append(q)
        counts = [len(per.get(d, [])) for d in cov["days"]]
        qs = [q for _, _, q, _, _ in rows]
        by = lambda lim: statistics.mean(sum(1 for q in per.get(d, []) if q <= lim) for d in cov["days"])
        print(c, f"name-days {len(rows)}; per day mean {statistics.mean(counts):.2f} median {statistics.median(counts)} "
              f"zero-days {sum(1 for x in counts if x == 0)}/{ndays} >=3 days {sum(1 for x in counts if x >= 3)} >=4 days {sum(1 for x in counts if x >= 4)}")
        print("   qualified by (mean names/day): 08:00 %.2f  09:29 %.2f  10:00 %.2f  10:30 %.2f  11:00 %.2f" % (by(240), by(329), by(359), by(389), by(420)))
        print("   premarket share %.1f%%; qualify-time quartiles %s; median spread bp %.0f; median price at qualify %.2f" % (
            100 * sum(q < 330 for q in qs) / max(1, len(qs)), [hm(int(x)) for x in np.percentile(qs, [25, 50, 75])] if qs else None,
            np.median([x[3] for x in rows]) if rows else float('nan'), np.median([x[4] for x in rows]) if rows else float('nan')))



if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "daily":
        daily_main()
    elif mode == "minute":
        minute_main()
    elif mode in ("sn", "float"):
        nd = loose_namedays()
        print("loose name-days", sum(len(v) for v in nd.values()), "days", len(nd), flush=True)
        if mode == "sn":
            news(nd)
            spreads(nd)
        else:
            floats(nd)
    elif mode == "analyze":
        analyze_main()
    elif mode == "report":
        report()
