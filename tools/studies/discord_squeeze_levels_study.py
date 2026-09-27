"""Discord "Squeeze Potential Alert" level-break backtest (read-only research).

Inputs (not committed; raw Discord text stays out of the repo):
  $BBSQ_DIR/squeeze_alerts_sep.txt   "YYYY-MM-DD HH:MM | TICKER | ww close over L1/L2/L3 | extra"
  $BBSQ_DIR/bb_watchlists_sep.txt    nightly watchlists (category: A - B - C | ...)
Data: Alpaca historical SIP 1-min bars (04:00-16:00 ET), SIP quotes (spread at entry),
      IEX 1-min bars (06:30-09:30) for the live-feasibility check.
Usage:  python discord_squeeze_levels_study.py fetch|analyze|all
"""
import json, math, os, pickle, random, re, statistics, sys, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DIR = os.environ.get("BBSQ_DIR", "/tmp/bbsq")
CACHE = os.path.join(DIR, "cache.pkl")
HOLIDAYS = {"2026-09-07"}
END_MIN = 10 * 60 + 30          # last entry minute (ET, minute-of-day)
CAP_MIN = 60                    # bracket time cap (minutes after entry)
HOLD_MIN = 30                   # X3 / X4 window
NBASE = 5                       # random baseline minutes per alert
SLEEP = 0.33


# ---------------------------------------------------------------- inputs
def parse_alerts():
    out = []
    for line in open(os.path.join(DIR, "squeeze_alerts_sep.txt")):
        p = [x.strip() for x in line.split("|")]
        if len(p) < 3:
            continue
        m = re.search(r"close over ([\d./]+)", p[2])
        lv = [float(x) for x in m.group(1).split("/") if x]
        t = datetime.strptime(p[0], "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        out.append({"day": p[0][:10], "t": t, "min": t.hour * 60 + t.minute,
                    "sym": p[1].upper().lstrip("$"), "L": lv})
    return out


def trading_days(d0="2026-08-25", d1="2026-09-30"):
    d = datetime.strptime(d0, "%Y-%m-%d")
    out = []
    while d <= datetime.strptime(d1, "%Y-%m-%d"):
        s = d.strftime("%Y-%m-%d")
        if d.weekday() < 5 and s not in HOLIDAYS:
            out.append(s)
        d += timedelta(days=1)
    return out


def parse_watchlists():
    td = trading_days()
    out = []
    for line in open(os.path.join(DIR, "bb_watchlists_sep.txt")):
        p = [x.strip() for x in line.split("|")]
        post = p[0][:10]
        nxt = next((d for d in td if d > post), None)
        for seg in p[1:]:
            if ":" not in seg:
                continue
            cat, names = seg.split(":", 1)
            cat = cat.strip().upper().replace("WATCH", "").replace("TICKERS", "").strip()
            for s in names.split("-"):
                s = s.strip().upper()
                if s and s != "NONE":
                    out.append({"post": post, "day": nxt, "cat": cat, "sym": s})
    return out


# ---------------------------------------------------------------- fetch
def client():
    import ai_entry_watch as ew
    return ew._data_client()


def _bars(cl, syms, start, end, feed, tf="1m"):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    tfo = TimeFrame(1, TimeFrameUnit.Minute) if tf == "1m" else TimeFrame.Day
    fd = DataFeed.SIP if feed == "sip" else DataFeed.IEX
    for attempt in range(4):
        try:
            bs = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms, timeframe=tfo,
                                                    start=start.astimezone(timezone.utc),
                                                    end=end.astimezone(timezone.utc), feed=fd))
            time.sleep(SLEEP)
            return {s: [(r.timestamp.timestamp(), float(r.open), float(r.high), float(r.low), float(r.close),
                         float(r.volume), int(r.trade_count or 0)) for r in rows]
                    for s, rows in (bs.data or {}).items()}
        except Exception as e:  # noqa: BLE001
            print("bars err", feed, tf, type(e).__name__, str(e)[:120], flush=True)
            time.sleep(3 * (attempt + 1))
    return None


def _quotes(cl, sym, t0, secs=60):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    for attempt in range(4):
        try:
            q = cl.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=sym, start=t0.astimezone(timezone.utc),
                                                       end=(t0 + timedelta(seconds=secs)).astimezone(timezone.utc),
                                                       feed=DataFeed.SIP, limit=5000))
            time.sleep(SLEEP)
            rows = (q.data or {}).get(sym, [])
            return [(float(r.bid_price), float(r.ask_price)) for r in rows]
        except Exception as e:  # noqa: BLE001
            print("quotes err", sym, type(e).__name__, str(e)[:120], flush=True)
            time.sleep(3 * (attempt + 1))
    return None


def load_cache():
    if os.path.exists(CACHE):
        return pickle.load(open(CACHE, "rb"))
    return {"sip": {}, "pc": {}, "iex": {}, "q": {}, "ctrl": {}, "assets": None, "daily": {}}


def save_cache(C):
    pickle.dump(C, open(CACHE + ".tmp", "wb"))
    os.replace(CACHE + ".tmp", CACHE)


def fetch_namedays(cl, C, namedays, iex=False):
    byday = defaultdict(set)
    for d, s in namedays:
        if (s, d) not in C["sip"] or (iex and (s, d) not in C["iex"]):
            byday[d].add(s)
    for d, syms in sorted(byday.items()):
        syms = sorted(syms)
        dd = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET)
        for i in range(0, len(syms), 50):
            b = syms[i:i + 50]
            dly = _bars(cl, b, dd - timedelta(days=12), dd, "sip", "1d") or {}
            for s in b:
                rows = [r for r in dly.get(s, []) if datetime.fromtimestamp(r[0], ET).strftime("%Y-%m-%d") < d]
                C["pc"][(s, d)] = rows[-1][4] if rows else None
            got = _bars(cl, b, dd.replace(hour=4), dd.replace(hour=16, minute=1), "sip")
            if got is not None:
                for s in b:
                    C["sip"][(s, d)] = got.get(s, [])
            if iex:
                gi = _bars(cl, b, dd.replace(hour=6, minute=30), dd.replace(hour=9, minute=30), "iex")
                if gi is not None:
                    for s in b:
                        C["iex"][(s, d)] = gi.get(s, [])
        print("namedays", d, len(syms), flush=True)
        save_cache(C)


def fetch_quotes(cl, C, keys):
    n = 0
    for s, d, m in sorted(set(keys)):
        if (s, d, m) in C["q"] and C["q"][(s, d, m)] != []:
            continue
        dd = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET)
        t0 = dd.replace(hour=m // 60, minute=m % 60)
        q = _quotes(cl, s, t0)
        if q == []:   # NBBO unchanged in the minute: use the standing quotes from the prior 5 min
            q = _quotes(cl, s, t0 - timedelta(seconds=300), secs=360)
            C.setdefault("qfb", set()).add((s, d, m))
        C["q"][(s, d, m)] = q
        n += 1
        if n % 50 == 0:
            print("quotes", n, flush=True)
            save_cache(C)
    save_cache(C)


def fetch_control(cl, C, days, per_day=30):
    """Random common-stock control: prior close $1-10, prior-day volume >= 100k."""
    from ticker_filters import is_common, is_levered_etp
    if C["assets"] is None:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass, AssetStatus
        from config import load_config
        cfg = load_config() or {}
        tc = TradingClient(cfg.get("api_key"), cfg.get("secret_key"), paper=bool(cfg.get("paper", True)))
        A = tc.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY))
        C["assets"] = sorted(a.symbol for a in A if a.tradable and str(a.exchange).split(".")[-1] in
                             ("NASDAQ", "NYSE", "AMEX", "ARCA", "BATS")
                             and is_common(a.symbol) and not is_levered_etp(a.symbol, a.name or ""))
        save_cache(C)
    syms = C["assets"]
    if not C["daily"]:
        s0 = datetime(2026, 8, 25, tzinfo=ET)
        s1 = datetime(2026, 9, 26, tzinfo=ET)
        for i in range(0, len(syms), 200):
            got = _bars(cl, syms[i:i + 200], s0, s1, "sip", "1d") or {}
            for s, rows in got.items():
                C["daily"][s] = [(datetime.fromtimestamp(r[0], ET).strftime("%Y-%m-%d"), r[4], r[5]) for r in rows]
        save_cache(C)
    rng = random.Random(7)
    want = []
    for d in days:
        pool = []
        for s, rows in C["daily"].items():
            prev = [r for r in rows if r[0] < d]
            if prev and 1 <= prev[-1][1] < 10 and prev[-1][2] >= 100_000 and any(r[0] == d for r in rows):
                pool.append(s)
        pool.sort()
        for s in rng.sample(pool, min(per_day, len(pool))):
            want.append((d, s))
    C["ctrl"] = want
    fetch_namedays(cl, C, want)


# ---------------------------------------------------------------- helpers
def mod(ts):
    t = datetime.fromtimestamp(ts, ET)
    return t.hour * 60 + t.minute


def spread_of(qs):
    """Median quoted SIP spread ($) and mid over the sampled window; crossed/zero quotes dropped."""
    if not qs:
        return None, None
    v = [(a - b, (a + b) / 2) for b, a in qs if b > 0 and a > b]
    if not v:
        return None, None
    return statistics.median(x[0] for x in v), statistics.median(x[1] for x in v)


def next_idx(bars, minute):
    """Index of the first bar starting at or after minute."""
    for i, r in enumerate(bars):
        if mod(r[0]) >= minute:
            return i
    return None


def sim_bracket(bars, j, F, S, t1, t2, trail, stop):
    """Thirds: +t1, +t2, last third trails `trail` from the high once +t2 is reached; all start with stop.
    Limit targets fill when the bid (trade - S/2) reaches them; stops fill at level (or worse open) - S/2.
    Within a bar the stop is checked first (conservative). Remaining pieces exit at the close - S/2 after CAP_MIN."""
    h2 = S / 2
    t_end = mod(bars[j][0]) + CAP_MIN
    open_p = [True, True, True]
    px = [None, None, None]
    fixed = F - stop
    trail_on, hi = False, None
    last = j
    for k in range(j, len(bars)):
        ts, o, h, l, c = bars[k][:5]
        if mod(ts) > t_end:
            break
        last = k
        # stops first
        lv = [fixed, fixed, (max(fixed, hi - trail) if trail_on else fixed)]
        for p in range(3):
            if open_p[p] and l <= lv[p]:
                open_p[p], px[p] = False, min(lv[p], o) - h2
        if open_p[0] and h - h2 >= F + t1:
            open_p[0], px[0] = False, F + t1
        if open_p[1] and h - h2 >= F + t2:
            open_p[1], px[1] = False, F + t2
            trail_on, hi = True, h
        if trail_on:
            hi = max(hi, h)
        if not any(open_p):
            break
    for p in range(3):
        if open_p[p]:
            px[p] = bars[last][4] - h2
    return statistics.mean((x - F) / F for x in px) * 100


def hold(bars, j, F, S):
    t_end = mod(bars[j][0]) + HOLD_MIN - 1
    w = [r for r in bars[j:] if mod(r[0]) <= t_end]
    ex = w[-1][4] - S / 2
    mfe = (max(r[2] for r in w) / F - 1) * 100
    mae = (min(r[3] for r in w) / F - 1) * 100
    return (ex / F - 1) * 100, mfe, mae


# ---------------------------------------------------------------- entries
def entries_for(a, bars, rng):
    """Return list of (etype, entry bar index, raw fill price before costs)."""
    out = []
    am = a["min"]
    after = [i for i, r in enumerate(bars) if am <= mod(r[0]) <= END_MIN - 1]
    # E1 / E2: first 1-min close above level (bar starting >= alert minute), buy next bar open
    for tag, li in (("E1", 0), ("E2", 1)):
        if len(a["L"]) <= li:
            continue
        L = a["L"][li]
        for i in after:
            if bars[i][4] > L and i + 1 < len(bars) and mod(bars[i + 1][0]) <= END_MIN:
                out.append((tag, i + 1, bars[i + 1][1]))
                break
    # E3: $0.01 over the prior completed clock-aligned 5-min high, first break after the alert
    fives = defaultdict(lambda: [-1e9])
    for r in bars:
        fives[mod(r[0]) // 5][0] = max(fives[mod(r[0]) // 5][0], r[2])
    for i, r in enumerate(bars):
        m = mod(r[0])
        if m <= am or m > END_MIN:      # strictly after the alert minute
            continue
        prev5 = m // 5 - 1
        if prev5 not in fives:
            continue
        H = fives[prev5][0] + 0.01
        if r[2] >= H:
            out.append(("E3", i, max(H, r[1])))
            break
    # E4: next bar open after the alert minute
    j = next_idx(bars, am + 1)
    if j is not None and mod(bars[j][0]) <= END_MIN:
        out.append(("E4", j, bars[j][1]))
    # baseline: random bar opens after the alert
    cand = [i for i, r in enumerate(bars) if am + 1 <= mod(r[0]) <= END_MIN]
    for i in rng.sample(cand, min(NBASE, len(cand))):
        out.append(("BASE", i, bars[i][1]))
    return out


def plan(alerts, C):
    rng = random.Random(11)
    P = []
    for n, a in enumerate(alerts):
        bars = C["sip"].get((a["sym"], a["day"])) or []
        if not bars:
            continue
        for et, j, raw in entries_for(a, bars, rng):
            P.append({"aid": n, "et": et, "j": j, "raw": raw, "m": mod(bars[j][0])})
    return P


# ---------------------------------------------------------------- analysis
def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))] if v else float("nan")


def summ(v):
    if not v:
        return {"n": 0}
    return {"n": len(v), "win": sum(x > 0 for x in v) / len(v) * 100, "avg": statistics.mean(v),
            "med": statistics.median(v)}


def fmt(s):
    if not s.get("n"):
        return "| 0 | | | | |"
    return f"| {s['n']} | {s['win']:.0f}% | {s['avg']:+.2f}% | {s['med']:+.2f}% | ${s['avg']*10:+.1f} |"


def analyze(C):
    alerts = parse_alerts()
    days = sorted({a["day"] for a in alerts})
    half = {d: ("A" if i % 2 == 0 else "B") for i, d in enumerate(days)}
    L = []
    A = []
    # 1) alert context
    for n, a in enumerate(alerts):
        bars = C["sip"].get((a["sym"], a["day"])) or []
        pc = C["pc"].get((a["sym"], a["day"]))
        pre = [r for r in bars if mod(r[0]) < a["min"]]
        ctx = {"aid": n, "day": a["day"], "sym": a["sym"], "min": a["min"], "L1": a["L"][0], "pc": pc,
               "px": pre[-1][4] if pre else None, "pmh": max(r[2] for r in pre) if pre else None,
               "vol_pre": sum(r[5] for r in pre)}
        post = [r for r in bars if a["min"] <= mod(r[0]) < END_MIN]
        ctx["L1_vs_pmh"] = (a["L"][0] / ctx["pmh"] - 1) * 100 if ctx["pmh"] else None
        ctx["touch"] = any(r[2] >= a["L"][0] for r in post)
        ctx["close_over"] = any(r[4] > a["L"][0] for r in post)
        ctx["close_over2"] = len(a["L"]) > 1 and any(r[4] > a["L"][1] for r in post)
        ctx["gap"] = (ctx["px"] / pc - 1) * 100 if (pc and ctx["px"]) else None
        ctx["vsL1"] = (ctx["px"] / a["L"][0] - 1) * 100 if ctx["px"] else None
        ctx["above"] = ctx["px"] is not None and ctx["px"] > a["L"][0]
        ctx["tb"] = "<07:30" if a["min"] < 450 else ("07:30-08:30" if a["min"] <= 510 else ">08:30")
        A.append(ctx)
    # 2) trades
    P = plan(alerts, C)
    skipped = defaultdict(int)
    for e in P:
        a = alerts[e["aid"]]
        ctx = A[e["aid"]]
        bars = C["sip"][(a["sym"], a["day"])]
        S, mid = spread_of(C["q"].get((a["sym"], a["day"], e["m"])))
        if S is None:
            skipped[e["et"]] += 1
            continue
        raw = e["raw"]
        slip = 0.01 if raw < 5 else 0.0
        F = raw + S / 2 + slip
        r = {"et": e["et"], "day": a["day"], "half": half[a["day"]], "sym": a["sym"], "F": F, "raw": raw,
             "S": S, "Spct": S / raw * 100, "m": e["m"], "above": ctx["above"], "tb": ctx["tb"],
             "tier": "<$2" if raw < 2 else "$2-5" if raw < 5 else "$5-10" if raw < 10 else ">$10"}
        r["X1"] = sim_bracket(bars, e["j"], F, S, 0.05, 0.08, 0.10, 0.15)
        r["X2"] = sim_bracket(bars, e["j"], F, S, 0.02 * F, 0.03 * F, 0.02 * F, 0.05 * F)
        r["X3"], r["MFE"], r["MAE"] = hold(bars, e["j"], F, S)
        # zero-cost reference (fill at raw, no spread, no slippage)
        r["X1_0"] = sim_bracket(bars, e["j"], raw, 0.0, 0.05, 0.08, 0.10, 0.15)
        r["X3_0"] = hold(bars, e["j"], raw, 0.0)[0]
        L.append(r)
    return alerts, A, L, skipped, days, half


def iex_check(alerts, C):
    rows = []
    for key in sorted({(a["sym"], a["day"]) for a in alerts}):
        sip = [r for r in (C["sip"].get(key) or []) if 390 <= mod(r[0]) < 570]
        iex = C["iex"].get(key) or []
        iex = [r for r in iex if 390 <= mod(r[0]) < 570]
        sv, iv = sum(r[5] for r in sip), sum(r[5] for r in iex)
        rows.append({"k": key, "sip_bars": len(sip), "sip_vol": sv, "sip_tr": sum(r[6] for r in sip),
                     "iex_bars": len(iex), "iex_vol": iv, "iex_tr": sum(r[6] for r in iex),
                     "iex_pre8": sum(1 for r in iex if mod(r[0]) < 480),
                     "iex_bars_8_930": sum(1 for r in iex if mod(r[0]) >= 480),
                     "sip_bars_8_930": sum(1 for r in sip if mod(r[0]) >= 480),
                     "share": iv / sv * 100 if sv else None})
    return rows


def wl_metrics(C, key):
    bars = C["sip"].get(key) or []
    pc = C["pc"].get(key)
    if not bars or not pc:
        return None
    pm = [r for r in bars if mod(r[0]) < 570]
    rth = [r for r in bars if 570 <= mod(r[0]) < 660]
    day = [r for r in bars if 570 <= mod(r[0]) < 960]
    if not rth or not day:
        return None
    o = rth[0][1]
    return {"pm_hi": (max(r[2] for r in pm) / pc - 1) * 100 if pm else 0.0,
            "pm_rng": ((max(r[2] for r in pm) - min(r[3] for r in pm)) / pc * 100) if pm else 0.0,
            "pm_vol": sum(r[5] for r in pm),
            "gap": (o / pc - 1) * 100, "mfe": (max(r[2] for r in rth) / o - 1) * 100,
            "mae": (min(r[3] for r in rth) / o - 1) * 100,
            "c_o": (day[-1][4] / o - 1) * 100, "c_pc": (day[-1][4] / pc - 1) * 100}


def report(C):
    alerts, A, L, skipped, days, half = analyze(C)
    out = []
    w = out.append
    w(f"alerts={len(alerts)} days={len(days)} with_bars={sum(1 for a in alerts if C['sip'].get((a['sym'], a['day'])))}")
    w(f"skipped_no_quote={dict(skipped)}")
    # context
    ok = [c for c in A if c["px"]]
    w(f"context n={len(ok)} above_L1_at_alert={sum(c['above'] for c in ok)} "
      f"med_vsL1={statistics.median(c['vsL1'] for c in ok):+.1f}% "
      f"med_gap={statistics.median(c['gap'] for c in ok if c['gap'] is not None):+.1f}% "
      f"med_px={statistics.median(c['px'] for c in ok):.2f} "
      f"px_at_alert_below_pmh_med={statistics.median((c['px']/c['pmh']-1)*100 for c in ok):+.1f}% "
      f"med_pc={statistics.median(c['pc'] for c in ok if c['pc']):.2f}")
    lp = [c["L1_vs_pmh"] for c in ok if c["L1_vs_pmh"] is not None]
    w(f"L1 vs premarket-high-so-far: median {statistics.median(lp):+.1f}%, L1 within +/-2% of PMH: {sum(abs(x)<=2 for x in lp)}/{len(lp)}, L1 above PMH: {sum(x>0 for x in lp)}/{len(lp)}")
    w(f"after alert to 10:30: traded at/above L1 {sum(c['touch'] for c in ok)}/{len(ok)}; 1-min close > L1 {sum(c['close_over'] for c in ok)}/{len(ok)}; close > L2 {sum(c['close_over2'] for c in ok)}/{len(ok)}")
    w(f"fallback (prior-5-min standing) quotes used: {len(C.get('qfb', ()))}")
    w("tb counts " + str({k: sum(1 for c in A if c['tb'] == k) for k in ('<07:30', '07:30-08:30', '>08:30')}))
    # spreads
    for et in ("E1", "E2", "E3", "E4", "BASE"):
        v = [r["Spct"] for r in L if r["et"] == et]
        if v:
            w(f"spread {et} n={len(v)} med={pct(v,.5):.2f}% p25={pct(v,.25):.2f}% p75={pct(v,.75):.2f}% p90={pct(v,.9):.2f}%")
    w("")
    w("| entry | exit | n | win | avg | median | per $1k |")
    w("|---|---|---|---|---|---|---|")
    for et in ("E1", "E2", "E3", "E4", "BASE"):
        for x in ("X1", "X2", "X3", "X1_0", "X3_0"):
            w(f"| {et} | {x} " + fmt(summ([r[x] for r in L if r["et"] == et])))
    w("")
    w("MFE/MAE 30m (from cost-inclusive fill):")
    for et in ("E1", "E2", "E3", "E4", "BASE"):
        v = [r for r in L if r["et"] == et]
        if v:
            w(f"  {et} medMFE={statistics.median(r['MFE'] for r in v):+.2f}% medMAE={statistics.median(r['MAE'] for r in v):+.2f}% "
              f"share MFE>=+5%={sum(r['MFE']>=5 for r in v)/len(v)*100:.0f}% share MAE<=-5%={sum(r['MAE']<=-5 for r in v)/len(v)*100:.0f}%")
    for split, key, labels in (("price", "tier", ("<$2", "$2-5", "$5-10", ">$10")),
                               ("alert time", "tb", ("<07:30", "07:30-08:30", ">08:30")),
                               ("above L1 at alert", "above", (True, False)),
                               ("half (alternate days)", "half", ("A", "B"))):
        w("")
        w(f"### by {split}")
        w("| entry | exit | bucket | n | win | avg | median | per $1k |")
        w("|---|---|---|---|---|---|---|---|")
        for et in ("E1", "E2", "E3", "E4", "BASE"):
            for x in ("X1", "X2", "X3"):
                for lb in labels:
                    w(f"| {et} | {x} | {lb} " + fmt(summ([r[x] for r in L if r["et"] == et and r[key] == lb])))
    w("")
    w("### by-day totals ($ per $1k notional per trade, summed over trades that day)")
    combos = [(e, x) for e in ("E1", "E2", "E3", "E4") for x in ("X1", "X2", "X3")]
    w("| day | " + " | ".join(f"{e}{x}" for e, x in combos) + " |")
    w("|---|" + "---|" * len(combos))
    pos = defaultdict(int)
    for d in days:
        cells = []
        for e, x in combos:
            v = [r[x] * 10 for r in L if r["et"] == e and r["day"] == d]
            cells.append(f"{sum(v):+.0f} ({len(v)})")
            pos[(e, x)] += sum(v) > 0
        w(f"| {d} | " + " | ".join(cells) + " |")
    w("| days>0 | " + " | ".join(str(pos[c]) for c in combos) + " |")
    # IEX
    I = iex_check(alerts, C)
    w("")
    w(f"### IEX feasibility 06:30-09:30 (name-days={len(I)})")
    sh = [r["share"] for r in I if r["share"] is not None]
    w(f"name-days with any IEX bar 06:30-09:30: {sum(r['iex_bars']>0 for r in I)}/{len(I)}; with any IEX bar before 08:00: {sum(r['iex_pre8']>0 for r in I)}")
    w(f"median IEX bars={statistics.median(r['iex_bars'] for r in I)} vs SIP bars={statistics.median(r['sip_bars'] for r in I)}; "
      f"median IEX trades={statistics.median(r['iex_tr'] for r in I)} vs SIP trades={statistics.median(r['sip_tr'] for r in I)}")
    w(f"08:00-09:30 median bars IEX={statistics.median(r['iex_bars_8_930'] for r in I)} SIP={statistics.median(r['sip_bars_8_930'] for r in I)}")
    if sh:
        w(f"IEX share of SIP volume: median={pct(sh,.5):.2f}% p75={pct(sh,.75):.2f}% p90={pct(sh,.9):.2f}% max={max(sh):.2f}%; aggregate={sum(r['iex_vol'] for r in I)/max(1,sum(r['sip_vol'] for r in I))*100:.2f}%")
    # coverage around alert time: IEX bars in the 30 min after each alert
    cov = []
    for a in alerts:
        iex = C["iex"].get((a["sym"], a["day"])) or []
        sip = C["sip"].get((a["sym"], a["day"])) or []
        ni = sum(1 for r in iex if a["min"] <= mod(r[0]) < a["min"] + 30)
        ns = sum(1 for r in sip if a["min"] <= mod(r[0]) < min(a["min"] + 30, 570))
        if ns:
            cov.append((ni, ns))
    if cov:
        w(f"30 min after alert (pre-09:30): median IEX 1-min bars={statistics.median(c[0] for c in cov)} of SIP {statistics.median(c[1] for c in cov)}; "
          f"alerts with zero IEX bars in that window={sum(c[0]==0 for c in cov)}/{len(cov)}")
    # watchlists
    W = parse_watchlists()
    w("")
    w("### watchlists (next-day, SIP)")
    w("| group | n | med PM high vs pc | med PM range | med gap | med MFE 9:30-11 | med MAE 9:30-11 | med close/open | med close/pc | share close>open |")
    w("|---|---|---|---|---|---|---|---|---|---|")

    def wrow(nm, keys):
        M = [m for m in (wl_metrics(C, k) for k in keys) if m]
        if not M:
            w(f"| {nm} | 0 |" + " |" * 8)
            return
        md = lambda f: statistics.median(m[f] for m in M)
        w(f"| {nm} | {len(M)} | {md('pm_hi'):+.1f}% | {md('pm_rng'):.1f}% | {md('gap'):+.1f}% | {md('mfe'):+.1f}% | {md('mae'):+.1f}% | "
          f"{md('c_o'):+.1f}% | {md('c_pc'):+.1f}% | {sum(m['c_o']>0 for m in M)/len(M)*100:.0f}% |")
    wl = [x for x in W if x["day"] and x["day"] <= "2026-09-25"]
    wrow("all watchlist (unique name-days)", sorted({(x["sym"], x["day"]) for x in wl}))
    for cat in sorted({x["cat"] for x in wl}):
        wrow(cat, sorted({(x["sym"], x["day"]) for x in wl if x["cat"] == cat}))
    wrow("random $1-10 control", [(s, d) for d, s in C.get("ctrl") or []])
    return "\n".join(out), L, A


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    C = load_cache()
    alerts = parse_alerts()
    if mode in ("fetch", "all"):
        cl = client()
        fetch_namedays(cl, C, {(a["day"], a["sym"]) for a in alerts}, iex=True)
        P = plan(alerts, C)
        fetch_quotes(cl, C, [(alerts[e["aid"]]["sym"], alerts[e["aid"]]["day"], e["m"]) for e in P])
        W = [x for x in parse_watchlists() if x["day"] and x["day"] <= "2026-09-25"]
        fetch_namedays(cl, C, {(x["day"], x["sym"]) for x in W})
        try:
            fetch_control(cl, C, sorted({x["day"] for x in W}))
        except Exception as e:  # noqa: BLE001
            print("control failed", type(e).__name__, str(e)[:200])
        save_cache(C)
    if mode in ("analyze", "all"):
        txt, L, A = report(C)
        open(os.path.join(DIR, "report.md"), "w").write(txt)
        json.dump(L, open(os.path.join(DIR, "trades.json"), "w"), default=str)
        print(txt)


if __name__ == "__main__":
    main()
