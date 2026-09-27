#!/usr/bin/env python3
"""offbook_momentum_study.py — do momentum-panel names that never reached the book offer capturable runway? ($1 floor)

Question (Jonathan): test the momentum names that are NOT on the book to see whether they'd produce a runway
we could capture if they were on the book, with a $1 floor.
Findings: docs/studies/OFFBOOK_MOMENTUM_2026-09-27.md.  Read-only on the desk; writes only $OFFBOOK_OUT (/tmp/offbook).

Universe  every name-day with a momentum-monitor journal mention event (kind new/burst; book-echo-free) 04:00-15:30,
          9/1-9/25 (18 sessions), common stock only (ticker_filters.is_common, is_levered_etp with Alpaca asset names),
          SIP price at the first mention >= $1 (last 1m close before the mention; prev close if none).
Groups    OFF = never seeded (proposal_ledger seed/kept, any proposer) nor seated (shadow.jsonl, any source) that day.
          ON  = seated that day (entries from the mention like OFF; ON_post = only after the seat time).
          SEED_ONLY = seeded but never seated (reported, not the focus).
Entries   decision at a 1m bar close >= max(first mention, 09:31) and <= 15:30, entry at the next bar open
          + half the SIP quoted spread (nearest of 5 samples 09:36/09:46/10:15/12:30/15:15, median of a 10 s window):
          arm = desk arm (fast %R21/EWM7 up through -50 with slow %R rising; momentum_runway_study.crosses_on,
          premarket-warmed); rand = up to 20 random eligible minutes per name-day; first = first eligible minute.
Metrics   MFE30; +20/+35/+50 bp before -35 bp within 30 min (lows first); rev50 = -50 before +35 (highs first);
          end30 net = close 30 min later minus the full spread (half in, half out at the exit-time sample);
          hold = exit at the 15:49 bar close (15:50) net of the full spread; gross = next-open to exit, no costs.
Blockers  (OFF, ledger days 9/16-9/25) from admit_ledger + proposal_ledger refusals for the sym-day (any source/stage).
USAGE (mini, repo root, niced)
  PYTHONPATH=$PWD:$PWD/tools:$PWD/tools/studies nice -n 15 .venv/bin/python tools/studies/offbook_momentum_study.py fetch
  PYTHONPATH=$PWD:$PWD/tools:$PWD/tools/studies nice -n 15 .venv/bin/python tools/studies/offbook_momentum_study.py analyze
"""
import bisect, collections, json, math, os, pickle, random, statistics, sys, time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv
import momentum_runway_study as S
from ticker_filters import is_common, is_levered_etp

ET = ZoneInfo("America/New_York")
ROOT = os.getcwd()
AR = os.path.join(ROOT, "ai_reports")
JR = os.path.join(ROOT, "momentum-monitor", "journal")
OUT = os.environ.get("OFFBOOK_OUT", "/tmp/offbook")
PRIOR = os.environ.get("MOMRUN_PRIOR", "/tmp/momrun")   # reuse the prior study's SIP caches when present
os.makedirs(OUT, exist_ok=True)
D0, D1 = "2026-09-01", "2026-09-25"
SAMPLES = ((9, 36), (9, 46), (10, 15), (12, 30), (15, 15))
TIERS = ((1, 5, "$1-5"), (5, 10, "$5-10"), (10, 20, "$10-20"), (20, 100, "$20-100"), (100, 1e12, ">$100"))
WINS = ((571, 660, "09:30-11:00"), (661, 900, "11:00-15:00"), (901, 930, "15:00-15:30"))
BAND = {"below_min_price", "price_cap", "above_max_price"}
I_EXIT = 15 * 60 + 49 - 240          # grid index of the 15:49 bar (close = 15:50)
NRAND = 20


def tier(p):
    return next((nm for a, b, nm in TIERS if p is not None and a <= p < b), None)


def win(m):
    return next((nm for a, b, nm in WINS if a <= m <= b), None)


def dayof(ts):
    return datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")


def days_list():
    out = []
    for f in sorted(os.listdir(JR)):
        d = f[:10]
        if D0 <= d <= D1 and datetime.strptime(d, "%Y-%m-%d").weekday() < 5 and d != "2026-09-07":
            out.append(d)
    return out


def universe(days):
    """(day, sym) -> first new/burst mention 04:00-15:30 (journal = book-echo-free)."""
    U = {}
    for d in days:
        for line in open(os.path.join(JR, f"{d}.jsonl")):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") not in ("new", "burst") or not r.get("ts"):
                continue
            t = datetime.fromtimestamp(r["ts"], ET)
            if not (240 <= t.hour * 60 + t.minute <= 930):
                continue
            k = (d, str(r.get("sym") or "").upper().strip())
            if k not in U or r["ts"] < U[k]["ts"]:
                U[k] = {"ts": float(r["ts"]), "kind": r["kind"], "jprice": r.get("price"), "age": r.get("price_age_sec"),
                        "rvol": r.get("rvol"), "pct": r.get("pct_change")}
    return U


def book(days):
    ds = set(days)
    seeded, seated = collections.defaultdict(dict), {}
    for line in open(os.path.join(AR, "shadow.jsonl")):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ts = r.get("ts")
        if not ts:
            continue
        d = dayof(ts)
        if d not in ds:
            continue
        k = (d, str(r.get("symbol") or "").upper())
        if k not in seated or ts < seated[k][0]:
            seated[k] = (float(ts), r.get("source"))
    ldays = set()
    pdir = os.path.join(AR, "proposal_ledger")
    for f in sorted(os.listdir(pdir)):
        d = f[:10]
        if d not in ds:
            continue
        ldays.add(d)
        for line in open(os.path.join(pdir, f)):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("stage") == "seed" and r.get("decision") == "kept":
                k = (d, str(r.get("symbol") or "").upper())
                src = r.get("proposer_norm") or r.get("proposer")
                ts = float(r["ts"])
                if src not in seeded[k] or ts < seeded[k][src]:
                    seeded[k][src] = ts
    return dict(seeded), seated, sorted(ldays)


def refusals(keys):
    """(day, sym) -> Counter(reason) over admit_ledger + proposal_ledger refusals, any source/stage."""
    want = collections.defaultdict(set)
    for d, s in keys:
        want[d].add(s)
    R = collections.defaultdict(collections.Counter)
    for sub, dec_ok in (("admit_ledger", None), ("proposal_ledger", "kept")):
        p = os.path.join(AR, sub)
        for f in sorted(os.listdir(p)):
            d = f[:10]
            if d not in want:
                continue
            W = want[d]
            for line in open(os.path.join(p, f)):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                s = str(r.get("symbol") or "").upper()
                if s not in W:
                    continue
                if (dec_ok and r.get("decision") == dec_ok) or r.get("kept") is True:
                    continue
                if r.get("reason"):
                    R[(d, s)][r["reason"]] += 1
    return dict(R)


def asset_names():
    p = os.path.join(OUT, "assets.json")
    if os.path.exists(p):
        return json.load(open(p))
    names = {}
    try:
        import ai_catalyst
        from alpaca.trading.enums import AssetClass
        from alpaca.trading.requests import GetAssetsRequest
        tc = ai_catalyst._trading_client()
        for a in tc.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY)):
            names[a.symbol] = a.name or ""
    except Exception as e:  # noqa: BLE001
        print("assets unavailable:", type(e).__name__)
    json.dump(names, open(p, "w"))
    return names


def fetch_spreads(keys, sp):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    cl = S.client()
    byday = collections.defaultdict(set)
    for d, s in keys:
        if any(f"{s}|{d}|{hh:02d}{mm:02d}" not in sp for hh, mm in SAMPLES):
            byday[d].add(s)
    for d, syms in sorted(byday.items()):
        syms = sorted(syms)
        dd = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET)
        for hh, mm in SAMPLES:
            t0 = dd.replace(hour=hh, minute=mm)
            acc = collections.defaultdict(list)
            for i in range(0, len(syms), 10):
                try:
                    q = cl.get_stock_quotes(StockQuotesRequest(
                        symbol_or_symbols=syms[i:i + 10], start=t0.astimezone(timezone.utc),
                        end=(t0 + timedelta(seconds=10)).astimezone(timezone.utc), feed=DataFeed.SIP, limit=20000))
                    for s, rows in (q.data or {}).items():
                        for r in rows:
                            b, a = float(r.bid_price), float(r.ask_price)
                            if b > 0 and a >= b:
                                acc[s].append((a - b) / ((a + b) / 2) * 1e4)
                except Exception as e:  # noqa: BLE001
                    print("quotes", d, type(e).__name__, str(e)[:80])
                time.sleep(0.3)
            for s in syms:
                v = sorted(acc.get(s) or [])
                sp[f"{s}|{d}|{hh:02d}{mm:02d}"] = v[len(v) // 2] if v else None
        print("spreads", d, len(syms), flush=True)
        json.dump(sp, open(os.path.join(OUT, "spreads.json"), "w"))
    return sp


def px_at(B, pc, ts):
    i = bisect.bisect_right(B[0], ts - 60) - 1   # last bar closed by ts
    return B[4][i] if i >= 0 else pc


def main_fetch():
    days = days_list()
    U = universe(days)
    names = asset_names()
    keep = {k: v for k, v in U.items() if is_common(k[1]) and not is_levered_etp(k[1], names.get(k[1], ""))}
    print("days", len(days), "mention name-days", len(U), "common/non-levered", len(keep), "asset names", len(names), flush=True)
    cp = os.path.join(OUT, "bars.pkl")
    cache = pickle.load(open(cp, "rb")) if os.path.exists(cp) else {}
    pp = os.path.join(PRIOR, "bars.pkl")
    if os.path.exists(pp):
        old = pickle.load(open(pp, "rb"))
        for d, s in keep:
            if (s, d) not in cache and (s, d) in old:
                cache[(s, d)] = old[(s, d)]
        del old
    print("bars cached", sum((s, d) in cache for d, s in keep), flush=True)
    for d in days:
        S.fetch_bars([k for k in keep if k[0] == d], cache)
        pickle.dump(cache, open(cp, "wb"))
    cand = []
    for (d, s), u in keep.items():
        B, pc = cache.get((s, d)) or (None, None)
        if not B or not B[0]:
            continue
        p = px_at(B, pc, u["ts"])
        if p is not None and p >= 1.0:
            cand.append((d, s))
    spp = os.path.join(OUT, "spreads.json")
    sp = json.load(open(spp)) if os.path.exists(spp) else {}
    ps = os.path.join(PRIOR, "pm_spreads.json")
    if os.path.exists(ps):
        for kk, vv in json.load(open(ps)).items():
            sp.setdefault(kk, vv)
    print("price>=1 with bars", len(cand), flush=True)
    fetch_spreads(cand, sp)
    json.dump(sp, open(spp, "w"))
    seeded, seated, ldays = book(days)
    REF = refusals(list(keep))
    pickle.dump({"U": keep, "seeded": seeded, "seated": seated, "ldays": ldays, "REF": REF, "days": days},
                open(os.path.join(OUT, "meta.pkl"), "wb"))
    print("done", flush=True)


def spread_vec(sp, s, d):
    pts = [(hh * 60 + mm, sp.get(f"{s}|{d}|{hh:02d}{mm:02d}")) for hh, mm in SAMPLES]
    pts = [(m, v) for m, v in pts if v is not None and v > 0]
    if not pts:
        return None
    mins = np.arange(12 * 60 + 5) + 241
    return np.array([min(pts, key=lambda p: abs(p[0] - m))[1] for m in mins], dtype=float)


def metrics(G, spv):
    o, h, l, c, v, real = G
    n = G.shape[1]
    half = spv / 2.0
    E = np.full(n, np.nan)
    E[:-1] = o[1:] * (1 + half[:-1] / 1e4)
    O1 = np.concatenate([o[1:], [np.nan]])
    W = 31
    pad = lambda x: np.concatenate([x, np.full(W, np.nan)])
    hw = swv(pad(h)[1:], W)[:n, :30]; lw = swv(pad(l)[1:], W)[:n, :30]; cw = swv(pad(c)[1:], W)[:n, :30]
    hx = swv(pad(half)[1:], W)[:n, :30]
    R = {}
    with np.errstate(invalid="ignore", divide="ignore"):
        R["mfe30"] = (np.nanmax(hw, axis=1) / E - 1) * 1e4
        sb = lw <= (E * (1 - 35 / 1e4))[:, None]
        fs = np.where(sb.any(axis=1), sb.argmax(axis=1), 999)
        for X in (20, 35, 50):
            tb = hw >= (E * (1 + X / 1e4))[:, None]
            ft = np.where(tb.any(axis=1), tb.argmax(axis=1), 999)
            R[f"h{X}"] = (ft < 999) & (ft < fs)
        tb = lw <= (E * (1 - 50 / 1e4))[:, None]
        ft = np.where(tb.any(axis=1), tb.argmax(axis=1), 999)
        sb2 = hw >= (E * (1 + 35 / 1e4))[:, None]
        fs2 = np.where(sb2.any(axis=1), sb2.argmax(axis=1), 999)
        R["rev50"] = (ft < 999) & (ft < fs2)
        R["end30"] = (cw[:, 29] / E - 1 - hx[:, 29] / 1e4) * 1e4
        R["g30"] = (cw[:, 29] / O1 - 1) * 1e4
        R["hold"] = (c[I_EXIT] / E - 1 - half[I_EXIT] / 1e4) * 1e4
        R["holdg"] = (c[I_EXIT] / O1 - 1) * 1e4
    R["spread"] = spv
    nxt_real = np.concatenate([real[1:], [0]]) == 1
    ok = nxt_real & ~np.isnan(E) & ~np.isnan(R["mfe30"]) & ~np.isnan(R["end30"]) & ~np.isnan(R["hold"])
    return R, ok


def blocker(k, px, u, ldays, REF):
    d = k[0]
    band = "<$20" if px < 20 else (">$100" if px > 100 else "in")
    if d not in ldays:
        return "pre-ledger (9/1-9/15), " + ("in band" if band == "in" else f"out of band {band}")
    rs = REF.get(k) or collections.Counter()
    if not rs:
        if band != "in":
            return f"no refusal logged, {band}"
        stale = u.get("jprice") is None or u.get("age") is None or float(u["age"]) > 300
        return "silent: in band, no refusal, " + ("stale mention px" if stale else "fresh mention px")
    nb = {r: c for r, c in rs.items() if r not in BAND}
    if not nb:
        if set(rs) == {"below_min_price"}:
            return "$20 floor only"
        if set(rs) <= {"price_cap", "above_max_price"}:
            return "$100 cap only"
        return "band only (mixed)"
    if "thin_rvol" in nb:
        return "thin_rvol" + (" + band" if set(rs) & BAND else " (no band)")
    top = max(nb, key=nb.get)
    return "other: " + top


def summ(ev):
    if not ev:
        return None
    n = len(ev)
    col = lambda k: [e[k] for e in ev]
    def tday(key, sub=None):
        bd = collections.defaultdict(list)
        for e in (sub if sub is not None else ev):
            bd[e["day"]].append(e[key])
        dm = [statistics.mean(x) for x in bd.values()]
        if len(dm) < 3:
            return None
        se = statistics.stdev(dm) / math.sqrt(len(dm))
        return round(statistics.mean(dm) / se, 2) if se > 0 else None
    A = [e for e in ev if e["half"] == "A"]; B = [e for e in ev if e["half"] == "B"]
    mean = lambda xs: round(statistics.mean(xs), 1) if xs else None

    def trim(xs, q=0.025):   # mean after dropping the top and bottom 2.5% (outlier check)
        xs = sorted(xs); k = int(len(xs) * q)
        return round(statistics.mean(xs[k:len(xs) - k]), 1) if len(xs) - 2 * k > 0 else None
    nd = len({(e["day"], e["sym"]) for e in ev}); days = len({e["day"] for e in ev})
    return {"n": n, "nd": nd, "days": days, "spread": round(statistics.median(col("spread")), 1),
            "mfe30": round(statistics.median(col("mfe30")), 1),
            "h20": round(100 * sum(col("h20")) / n, 1), "h35": round(100 * sum(col("h35")) / n, 1),
            "h50": round(100 * sum(col("h50")) / n, 1), "rev50": round(100 * sum(col("rev50")) / n, 1),
            "end_mean": mean(col("end30")), "end_med": round(statistics.median(col("end30")), 1), "g30": mean(col("g30")),
            "hold_mean": mean(col("hold")), "hold_trim": trim(col("hold")), "hold_med": round(statistics.median(col("hold")), 1), "holdg": mean(col("holdg")), "spy": mean([x for x in col("spy") if not math.isnan(x)]),
            "t_end": tday("end30"), "t_hold": tday("hold"),
            "A_end": mean([e["end30"] for e in A]), "B_end": mean([e["end30"] for e in B]),
            "A_hold": mean([e["hold"] for e in A]), "B_hold": mean([e["hold"] for e in B]),
            "small": bool(n < 30 or nd < 10 or days < 5)}


def fmt(tag, s):
    if not s:
        return f"{tag:58s} n=0"
    return (f"{tag:58s} n={s['n']:5d} nd={s['nd']:4d} d={s['days']:2d} spr={s['spread']:6.1f} mfe30={s['mfe30']:6.1f} "
            f"+20/35/50={s['h20']:4.1f}/{s['h35']:4.1f}/{s['h50']:4.1f} rev50={s['rev50']:4.1f} "
            f"end30 net mean/med={s['end_mean']:7.1f}/{s['end_med']:6.1f} g30={s['g30']:6.1f} t={s['t_end']} "
            f"| hold net mean/med/trim={s['hold_mean']:7.1f}/{s['hold_med']:7.1f}/{s['hold_trim']} gross={s['holdg']:7.1f} spy={s['spy']} t={s['t_hold']} "
            f"| A/B end {s['A_end']}/{s['B_end']} hold {s['A_hold']}/{s['B_hold']}{'  SMALL' if s['small'] else ''}")


def main_analyze():
    M = pickle.load(open(os.path.join(OUT, "meta.pkl"), "rb"))
    cache = pickle.load(open(os.path.join(OUT, "bars.pkl"), "rb"))
    sp = json.load(open(os.path.join(OUT, "spreads.json")))
    U, seeded, seated, ldays, REF, days = M["U"], M["seeded"], M["seated"], set(M["ldays"]), M["REF"], M["days"]
    A = set(days[0::2])
    rng = random.Random(11)
    # market control: SPY gross return from the same entry minute to 15:50 (is hold-to-close just beta?)
    if any(("SPY", d) not in cache for d in days):
        S.fetch_bars([(d, "SPY") for d in days], cache)
        pickle.dump(cache, open(os.path.join(OUT, "bars.pkl"), "wb"))
    SPY = {}
    for d in days:
        Gs = S.grid(cache[("SPY", d)][0], d)
        o_, c_ = Gs[0], Gs[3]
        SPY[d] = (c_[I_EXIT] / np.concatenate([o_[1:], [np.nan]]) - 1) * 1e4
    ev = []
    info = {}
    drop = collections.Counter()
    for k in sorted(U):
        d, s = k
        u = U[k]
        B, pc = cache.get((s, d)) or (None, None)
        if not B or not B[0]:
            drop["no SIP bars"] += 1
            continue
        px = px_at(B, pc, u["ts"])
        if px is None or px < 1.0:
            drop["price < $1 at mention"] += 1
            continue
        grp = "ON" if k in seated else ("SEED_ONLY" if k in seeded else "OFF")
        spv = spread_vec(sp, s, d)
        if spv is None:
            drop["no SIP spread sample"] += 1
            continue
        G = S.grid(B, d)
        R, ok = metrics(G, spv)
        d0 = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET, hour=4).timestamp()
        n = G.shape[1]
        dec_ts = d0 + (np.arange(n) + 1) * 60
        mod = np.arange(n) + 241
        elig = ok & (dec_ts >= u["ts"]) & (mod >= 571) & (mod <= 930)
        idx = np.where(elig)[0]
        tr = tier(px)
        blk = blocker(k, px, u, ldays, REF) if grp in ("OFF", "SEED_ONLY") else None
        seat_ts = seated[k][0] if k in seated else None
        info[k] = {"grp": grp, "px": px, "tier": tr, "blk": blk, "n_elig": len(idx), "mention_min": int((u["ts"] - d0) // 60) + 240,
                   "seat_src": seated[k][1] if k in seated else None, "seed_src": sorted(seeded.get(k, {}))}
        if not len(idx):
            drop["no eligible RTH minute after mention"] += 1
            continue
        cx = set(S.crosses_on(G))
        picks = {"arm": [i for i in idx if i in cx], "rand": sorted(rng.sample(list(idx), min(NRAND, len(idx)))),
                 "first": [int(idx[0])]}
        for et, L in picks.items():
            for i in L:
                e = {f: (bool(R[f][i]) if f in ("h20", "h35", "h50", "rev50") else float(R[f][i])) for f in R}
                e.update(day=d, sym=s, half="A" if d in A else "B", grp=grp, tier=tr, win=win(int(mod[i])), et=et, blk=blk,
                         post=bool(seat_ts is not None and dec_ts[i] >= seat_ts), spy=float(SPY[d][i]))
                ev.append(e)
    pickle.dump({"ev": ev, "info": info, "drop": dict(drop)}, open(os.path.join(OUT, "ev.pkl"), "wb"))
    res = {"drop": dict(drop), "namedays": dict(collections.Counter(v["grp"] for v in info.values())),
           "namedays_by_tier": {g: dict(collections.Counter(v["tier"] for v in info.values() if v["grp"] == g)) for g in ("OFF", "ON", "SEED_ONLY")},
           "tables": {}}
    print("name-days kept:", res["namedays"], "dropped:", dict(drop))
    print("by tier:", res["namedays_by_tier"])
    tiers = [t for _, _, t in TIERS]; wins = [w for _, _, w in WINS]

    def sel(**kw):
        out = ev
        for f, val in kw.items():
            if f == "grp" and val == "ON_post":
                out = [e for e in out if e["grp"] == "ON" and e["post"]]
            elif f == "grp" and val == "NOT_ON":
                out = [e for e in out if e["grp"] in ("OFF", "SEED_ONLY")]
            else:
                out = [e for e in out if e[f] == val]
        return out

    def put(tag, rows):
        s = summ(rows)
        res["tables"][tag] = s
        print(fmt(tag, s))
    for et in ("arm", "rand", "first"):
        print(f"\n== {et}: group overall / by tier / by window")
        for g in ("OFF", "ON", "ON_post", "SEED_ONLY", "NOT_ON"):
            base = sel(et=et, grp=g)
            put(f"{g} {et} ALL", base)
            for t in tiers:
                put(f"{g} {et} {t}", [e for e in base if e["tier"] == t])
            for w in wins:
                put(f"{g} {et} {w}", [e for e in base if e["win"] == w])
    print("\n== tier x window (arm + rand), OFF vs ON")
    for et in ("arm", "rand"):
        for t in tiers:
            for w in wins:
                for g in ("OFF", "ON"):
                    put(f"{g} {et} {t} {w}", [e for e in sel(et=et, grp=g) if e["tier"] == t and e["win"] == w])
    ledger_d = set(M["ldays"])
    res["ledger_split"] = {g: {"ledger_days": sum(1 for kk, v in info.items() if v["grp"] == g and kk[0] in ledger_d),
                               "pre_ledger": sum(1 for kk, v in info.items() if v["grp"] == g and kk[0] not in ledger_d)}
                           for g in ("OFF", "ON", "SEED_ONLY")}
    res["tier_ledger_split"] = {g: {t: [sum(1 for kk, v in info.items() if v["grp"] == g and v["tier"] == t and kk[0] not in ledger_d),
                                        sum(1 for kk, v in info.items() if v["grp"] == g and v["tier"] == t and kk[0] in ledger_d)]
                                    for t in tiers} for g in ("OFF", "ON", "SEED_ONLY")}
    print("\nname-days pre-ledger/ledger:", res["ledger_split"], "\nby tier [pre, ledger]:", res["tier_ledger_split"])
    res["blocker_namedays"], res["blocker_by_tier"] = {}, {}
    for G_ in ("OFF", "SEED_ONLY"):
        print(f"\n== {G_} by blocker (arm / rand / first; then by tier where >= 10 name-days)")
        blks = collections.Counter(v["blk"] for v in info.values() if v["grp"] == G_)
        res["blocker_namedays"][G_] = dict(blks)
        res["blocker_by_tier"][G_] = {b: dict(collections.Counter(v["tier"] for v in info.values() if v["grp"] == G_ and v["blk"] == b)) for b in blks}
        for b, c in blks.most_common():
            print(f"-- {b}: {c} name-days, tiers {res['blocker_by_tier'][G_][b]}")
            for et in ("arm", "rand", "first"):
                put(f"{G_}[{b}] {et}", [e for e in sel(et=et, grp=G_) if e["blk"] == b])
            for t, ct in res["blocker_by_tier"][G_][b].items():
                if ct >= 10 and len(res["blocker_by_tier"][G_][b]) > 1:
                    for et in ("arm", "rand"):
                        put(f"{G_}[{b}] {et} {t}", [e for e in sel(et=et, grp=G_) if e["blk"] == b and e["tier"] == t])
    json.dump(res, open(os.path.join(OUT, "results.json"), "w"), indent=1, default=str)


if __name__ == "__main__":
    {"fetch": main_fetch, "analyze": main_analyze}[sys.argv[1] if len(sys.argv) > 1 else "analyze"]()
