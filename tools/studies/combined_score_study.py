#!/usr/bin/env python3
"""combined_score_study.py — do volume, price, spread, %R and name strength,
scored together, pick moments that make money after the real spread?

Each factor was tested alone before; this tests them jointly, and then removes
one group at a time to see which ones earn their place.

Stage A — recorded days (tools/studies source_study_bars.pkl, 9/16-9/25):
  moments   every 15 min, 09:45-15:15 ET, $20-$100, every name in the cache
  factors   volume   vol_pace (cum volume vs 20-day avg x share of day elapsed),
                     vol_accel (last 15 min vs session mean), vol_burst (last 1m
                     vs prior 20m)
            price    log price
            spread   SIP (ask-bid)/mid median over the 5 s to the moment, bp
            %R       fast, slow (live: 21 EWM 7 / 112 EWM 3), fast_rising,
                     slow_rising (vs 2 bars ago), crossed -50 in the last 5 min
            strength day_chg, gap, pm_range, first15, range_pos, room_hod,
                     ret15, ret60, minutes since open
            source   one-hot: first source that nominated the name that day
  outcome   net30 = 30-min close-to-close return minus the spread at entry
            (a round trip crosses the spread once: half in, half out), bp
  model     ridge regression on winsorized, standardized factors (+ squares of
            day_chg, range_pos, fast/slow %R, minutes), leave-one-day-out: every
            moment is scored by a model that never saw its day
  report    the top 10% / 20% of held-out scores: net bp, success rate
            (+1% before -1% in 60 min), days positive; and the same with each
            factor group removed

Stage B — 60 earlier trading days, broad causal universe (--stage B):
  each day, the 600 most-traded $20-$100 names by the PRIOR day's dollar
  volume; the Stage A model (no source) scores them; the real spread is fetched
  for the moments it picks and subtracted.

USAGE (on the mini; Alpaca data keys; caches in ai_reports/)
  .venv/bin/python tools/studies/combined_score_study.py --stage A
  .venv/bin/python tools/studies/combined_score_study.py --stage B --days-back 60
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import pickle
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import mid_rise_runway_study as mr  # noqa: E402

ET = ZoneInfo("America/New_York")
BARS = os.path.join(ROOT, "ai_reports", "source_study_bars.pkl")
SPREADS = os.path.join(ROOT, "ai_reports", "combined_spreads.json")
DAILY = os.path.join(ROOT, "ai_reports", "combined_daily.pkl")
STAGE_B = os.path.join(ROOT, "ai_reports", "combined_stage_b_rows.pkl")
SOURCES = ("trending", "movers", "momentum", "research")

GROUPS = {
    "volume": ["vol_pace", "vol_accel", "vol_burst"],
    "price": ["log_price"],
    "spread": ["spread_bp"],
    "pctr": ["fast_r", "slow_r", "fast_rising", "slow_rising", "x50_5m", "fast_r2", "slow_r2"],
    "strength": ["day_chg", "gap", "pm_range", "first15", "range_pos", "room_hod", "ret15",
                 "ret60", "mins", "day_chg2", "range_pos2", "mins2"],
    "source": [f"src_{s}" for s in SOURCES],
}


def et_min(ts):
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def client():
    import ai_entry_watch as ew
    return ew._data_client()


# ── data ──────────────────────────────────────────────────────────────────

def daily_bars(cl, syms, end_day, n_days=45):
    """sym -> [(day, close, volume)] SIP daily, cached."""
    cache = pickle.load(open(DAILY, "rb")) if os.path.exists(DAILY) else {}
    key_end = end_day
    want = [s for s in syms if (s, key_end) not in cache]
    if want:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        d = datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=ET)
        for i in range(0, len(want), 200):
            got = {}
            try:
                bs = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=want[i:i + 200], timeframe=TimeFrame.Day,
                    start=(d - timedelta(days=n_days * 1.6)).astimezone(timezone.utc),
                    end=(d + timedelta(days=1)).astimezone(timezone.utc), feed=DataFeed.SIP))
                for sym, rows in (bs.data or {}).items():
                    got[sym] = [(r.timestamp.astimezone(ET).strftime("%Y-%m-%d"), float(r.close),
                                 float(r.volume)) for r in rows]
            except Exception as e:  # noqa: BLE001
                print(f"  daily: {e}"[:140])
            for s in want[i:i + 200]:
                cache[(s, key_end)] = got.get(s, [])
            time.sleep(0.5)
        pickle.dump(cache, open(DAILY, "wb"))
    return {s: cache.get((s, key_end), []) for s in syms}


class Spreads:
    """Batched SIP spreads: all symbols sampled at one minute in one request."""

    def __init__(self, cl):
        self.cl = cl
        try:
            self.d = json.load(open(SPREADS))
        except (OSError, ValueError):
            self.d = {}

    def fetch(self, t, syms):
        need = [s for s in syms if f"{s}|{int(t)}" not in self.d]
        if not need:
            return
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockQuotesRequest
        end = datetime.fromtimestamp(t, timezone.utc)
        for i in range(0, len(need), 100):
            batch = need[i:i + 100]
            got = {}
            try:
                q = self.cl.get_stock_quotes(StockQuotesRequest(
                    symbol_or_symbols=batch, start=end - timedelta(seconds=5), end=end,
                    feed=DataFeed.SIP))
                for sym, rows in (q.data or {}).items():
                    sp = sorted((float(r.ask_price) - float(r.bid_price)) /
                                ((float(r.ask_price) + float(r.bid_price)) / 2) * 1e4
                                for r in rows if r.bid_price and r.ask_price and r.ask_price >= r.bid_price)
                    if sp:
                        got[sym] = sp[len(sp) // 2]
            except Exception as e:  # noqa: BLE001
                print(f"  quotes: {e}"[:140])
            for s in batch:
                self.d[f"{s}|{int(t)}"] = got.get(s)

    def get(self, t, sym):
        return self.d.get(f"{sym}|{int(t)}")

    def save(self):
        json.dump(self.d, open(SPREADS, "w"))


def source_labels():
    """(sym, day) -> first source that nominated it (ledger + sources stream)."""
    out = {}
    base = os.path.join(ROOT, "ai_reports", "admit_ledger")
    for f in sorted(os.listdir(base)):
        day = f[:-6]
        for line in open(os.path.join(base, f)):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("stage") == "seed" and r.get("source") in SOURCES:
                out.setdefault((str(r["symbol"]).upper(), day), (float(r["ts"]), r["source"]))
    sess = os.path.join(ROOT, "ai_reports", "sessions")
    for day in os.listdir(sess):
        p = os.path.join(sess, day, "sources.jsonl.gz")
        if not os.path.exists(p):
            continue
        for line in gzip.open(p, "rt"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            src = "research" if r.get("source") in ("agy", "xai") else r.get("source")
            if r.get("event") == "enter" and src in SOURCES:
                k = (str(r["symbol"]).upper(), day)
                t = float(r["ts"])
                if k not in out or t < out[k][0]:
                    out[k] = (t, src)
    return {k: v[1] for k, v in out.items()}


# ── features ──────────────────────────────────────────────────────────────

def rows_for(sym, day, B, prev_close, daily, step=15):
    t, o, h, l, c, v = B
    n = len(c)
    rth = [i for i in range(n) if 570 <= et_min(t[i]) < 960]
    if len(rth) < 200 or not prev_close:
        return []
    prior = [x for x in daily if x[0] < day][-20:]
    avg_vol = statistics.mean(x[2] for x in prior) if len(prior) >= 10 else None
    i0 = rth[0]
    pm = list(range(i0))
    pm_range = ((max(h[j] for j in pm) / min(l[j] for j in pm) - 1) * 100) if pm else 0.0
    open_px = o[i0]
    gap = (open_px / prev_close - 1) * 100
    i15 = next((i for i in rth if et_min(t[i]) >= 585), None)
    first15 = (c[i15] / open_px - 1) * 100 if i15 is not None else 0.0
    fast = mr.percent_r_series(h, l, c, 21, 7.0)
    slow = mr.percent_r_series(h, l, c, 112, 3.0)
    out = []
    hi = lo = None
    cumv = 0.0
    for k, i in enumerate(rth):
        hi = h[i] if hi is None else max(hi, h[i])
        lo = l[i] if lo is None else min(lo, l[i])
        cumv += v[i]
        m = et_min(t[i]) + 1
        if not (585 <= m <= 915) or (m - 585) % step or not (20 <= c[i] <= 100) or i < 60:
            continue
        if fast[i] is None or slow[i] is None or fast[i - 2] is None or slow[i - 2] is None:
            continue
        px = c[i]
        frac = (m - 570) / 390.0
        vm20 = sum(v[i - 20:i]) / 20.0
        per_min = cumv / (k + 1)
        x50 = any(fast[j - 1] is not None and fast[j] is not None and fast[j - 1] <= -50 < fast[j]
                  for j in range(i - 4, i + 1))
        succ = 0
        for j in range(i + 1, min(n, i + 61)):
            if l[j] <= px * 0.99:
                break
            if h[j] >= px * 1.01:
                succ = 1
                break
        j30 = i + 30
        if j30 >= n or t[j30] - t[i] > 45 * 60:
            continue
        dchg = (px / prev_close - 1) * 100
        rpos = 100 * (px - lo) / (hi - lo) if hi > lo else 50.0
        out.append({
            "sym": sym, "day": day, "t": t[i] + 60,
            "vol_pace": (cumv / (avg_vol * frac)) if avg_vol else None,
            "vol_accel": (sum(v[i - 14:i + 1]) / 15) / per_min if per_min > 0 else None,
            "vol_burst": v[i] / vm20 if vm20 > 0 else None,
            "log_price": math.log(px),
            "fast_r": fast[i], "slow_r": slow[i],
            "fast_rising": float(fast[i] > fast[i - 2]), "slow_rising": float(slow[i] > slow[i - 2]),
            "x50_5m": float(x50),
            "fast_r2": (fast[i] + 50) ** 2 / 100, "slow_r2": (slow[i] + 50) ** 2 / 100,
            "day_chg": dchg, "gap": gap, "pm_range": pm_range, "first15": first15,
            "range_pos": rpos, "room_hod": (1 - px / hi) * 100,
            "ret15": (px / c[i - 15] - 1) * 100, "ret60": (px / c[i - 60] - 1) * 100,
            "mins": m - 570, "day_chg2": dchg ** 2 / 10, "range_pos2": (rpos - 50) ** 2 / 100,
            "mins2": (m - 570 - 195) ** 2 / 1000,
            "fwd30": (c[j30] / px - 1) * 1e4, "succ": succ,
        })
    return out


# ── model ─────────────────────────────────────────────────────────────────

def design(rows, feats, stats=None):
    X = np.array([[np.nan if r.get(f) is None else float(r[f]) for f in feats] for r in rows], float)
    if stats is None:
        lo = np.nanpercentile(X, 1, axis=0)
        hi = np.nanpercentile(X, 99, axis=0)
        Xc = np.clip(X, lo, hi)
        mu = np.nanmean(Xc, axis=0)
        sd = np.nanstd(Xc, axis=0)
        sd[sd == 0] = 1.0
        stats = (lo, hi, mu, sd)
    lo, hi, mu, sd = stats
    Xc = np.clip(X, lo, hi)
    Xc = np.where(np.isnan(Xc), mu, Xc)
    return (Xc - mu) / sd, stats


def fit(rows, feats, lam=50.0):
    X, st = design(rows, feats)
    y = np.array([r["net30"] for r in rows], float)
    y = np.clip(y, np.percentile(y, 1), np.percentile(y, 99))
    Xa = np.hstack([np.ones((len(X), 1)), X])
    reg = lam * np.eye(Xa.shape[1])
    reg[0, 0] = 0
    w = np.linalg.solve(Xa.T @ Xa + reg, Xa.T @ y)
    return w, st


def predict(model, rows, feats):
    w, st = model
    X, _ = design(rows, feats, st)
    return np.hstack([np.ones((len(X), 1)), X]) @ w


def lodo(rows, feats):
    """Leave-one-day-out scores for every row."""
    days = sorted({r["day"] for r in rows})
    score = np.zeros(len(rows))
    idx = defaultdict(list)
    for k, r in enumerate(rows):
        idx[r["day"]].append(k)
    for d in days:
        train = [rows[k] for dd in days if dd != d for k in idx[dd]]
        test = [rows[k] for k in idx[d]]
        m = fit(train, feats)
        score[idx[d]] = predict(m, test, feats)
    return score


def top_report(rows, score, frac):
    """Pick the top `frac` of scores WITHIN each day (a live desk ranks what it sees that day)."""
    by_day = defaultdict(list)
    for k, r in enumerate(rows):
        by_day[r["day"]].append(k)
    picked = []
    day_means = []
    for d, ks in by_day.items():
        ks = sorted(ks, key=lambda k: -score[k])
        top = ks[:max(1, int(len(ks) * frac))]
        picked += top
        day_means.append(statistics.mean(rows[k]["net30"] for k in top))
    net = [rows[k]["net30"] for k in picked]
    m = statistics.mean(net)
    se = statistics.pstdev(net) / math.sqrt(len(net))
    return {"n": len(net), "per_day": len(net) / len(by_day), "net": m, "t": m / se if se else None,
            "succ": statistics.mean(rows[k]["succ"] for k in picked),
            "win": statistics.mean(1.0 if rows[k]["net30"] > 0 else 0.0 for k in picked),
            "days_pos": sum(1 for x in day_means if x > 0), "days": len(day_means)}


def show(label, r):
    print(f"  {label:28} n {r['n']:6d} ({r['per_day']:5.0f}/day)  net {r['net']:+6.1f} bp  "
          f"t {r['t']:+5.1f}  success {r['succ']:5.1%}  win {r['win']:5.1%}  "
          f"days+ {r['days_pos']}/{r['days']}")


# ── stages ────────────────────────────────────────────────────────────────

def stage_a(args):
    cl = client()
    cache = pickle.load(open(BARS, "rb"))
    days = sorted({d for _s, d in cache})
    syms = sorted({s for s, _d in cache})
    daily = daily_bars(cl, syms, days[-1])
    src = source_labels()
    rows = []
    for (sym, day), rec in cache.items():
        if rec and rec[0]:
            rs = rows_for(sym, day, rec[0], rec[1], daily.get(sym, []), step=args.step)
            s_ = src.get((sym, day))
            for r in rs:
                for s in SOURCES:
                    r[f"src_{s}"] = float(s_ == s)
            rows += rs
    print(f"{len(rows)} moments, {len(days)} days; fetching SIP spreads (batched, cached)")
    sp = Spreads(cl)
    by_t = defaultdict(list)
    for r in rows:
        by_t[r["t"]].append(r["sym"])
    for k, (t, ss) in enumerate(sorted(by_t.items())):
        sp.fetch(t, ss)
        if k % 50 == 0:
            sp.save()
    sp.save()
    kept = []
    for r in rows:
        s = sp.get(r["t"], r["sym"])
        if s is None:
            continue
        r["spread_bp"] = s
        r["net30"] = r["fwd30"] - s
        kept.append(r)
    rows = kept
    base = statistics.mean(r["net30"] for r in rows)
    print(f"{len(rows)} moments with a spread; every moment: net {base:+.1f} bp, "
          f"spread median {statistics.median(r['spread_bp'] for r in rows):.1f} bp, "
          f"success {statistics.mean(r['succ'] for r in rows):.1%}\n")
    allf = [f for g in GROUPS.values() for f in g]
    print("Top of the score, leave-one-day-out (every score from a model that never saw that day):")
    full = lodo(rows, allf)
    for frac in (0.05, 0.10, 0.20):
        show(f"all factors, top {int(frac * 100)}%", top_report(rows, full, frac))
    print("\nRemove one group at a time (top 10%):")
    for g in GROUPS:
        feats = [f for f in allf if f not in GROUPS[g]]
        show(f"without {g}", top_report(rows, lodo(rows, feats), 0.10))
    print("\nOne group alone (top 10%):")
    for g in GROUPS:
        show(f"only {g}", top_report(rows, lodo(rows, GROUPS[g]), 0.10))
    m = fit(rows, allf)
    w = m[0][1:]
    order = np.argsort(-np.abs(w))
    print("\nWeights (standardized; + means higher -> more net bp):")
    print("  " + ", ".join(f"{allf[k]} {w[k]:+.1f}" for k in order[:14]))
    pickle.dump({"rows": rows, "feats": allf}, open(os.path.join(ROOT, "ai_reports", "combined_stage_a.pkl"), "wb"))


def stage_b(args):
    """Score 60 earlier days with the Stage A model (no source)."""
    a = pickle.load(open(os.path.join(ROOT, "ai_reports", "combined_stage_a.pkl"), "rb"))
    feats = [f for f in a["feats"] if not f.startswith("src_")]
    model = fit(a["rows"], feats)
    feats_nospread = [f for f in feats if f != "spread_bp"]
    model_ns = fit(a["rows"], feats_nospread)
    cl = client()
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetAssetsRequest
    import ai_entry_watch as ew
    key, sec = ew._alpaca_keys() if hasattr(ew, "_alpaca_keys") else (None, None)
    first = min(r["day"] for r in a["rows"])
    # trading days before the recorded window, from SPY daily bars
    spy = daily_bars(cl, ["SPY"], first, n_days=args.days_back + 5)["SPY"]
    tdays = [d for d, _c, _v in spy if d < first][-args.days_back:]
    # causal universe: all symbols seen in the cache + a broad set from daily bars
    universe = sorted({s for s, _d in pickle.load(open(BARS, "rb"))})
    extra = os.environ.get("COMBINED_UNIVERSE")
    if extra and os.path.exists(extra):
        universe = sorted(set(universe) | {x.strip().upper() for x in open(extra) if x.strip()})
    daily = daily_bars(cl, universe, first, n_days=args.days_back + 30)
    rows_out = pickle.load(open(STAGE_B, "rb")) if os.path.exists(STAGE_B) else {}
    for day in tdays:
        if day in rows_out:
            continue
        prevd = {}
        for s, rows in daily.items():
            pr = [x for x in rows if x[0] < day]
            if pr and 20 <= pr[-1][1] <= 100:
                prevd[s] = (pr[-1][1], pr[-1][1] * pr[-1][2])
        uni = [s for s, _ in sorted(prevd.items(), key=lambda kv: -kv[1][1])[:args.universe]]
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        day_rows = []
        for i in range(0, len(uni), 50):
            try:
                bs = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=uni[i:i + 50], timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=d.replace(hour=4).astimezone(timezone.utc),
                    end=d.replace(hour=16, minute=5).astimezone(timezone.utc), feed=DataFeed.SIP))
                for sym, br in (bs.data or {}).items():
                    B = ([r.timestamp.timestamp() for r in br], [float(r.open) for r in br],
                         [float(r.high) for r in br], [float(r.low) for r in br],
                         [float(r.close) for r in br], [float(r.volume) for r in br])
                    day_rows += rows_for(sym, day, B, prevd[sym][0], daily.get(sym, []), step=args.step)
            except Exception as e:  # noqa: BLE001
                print(f"  {day} bars: {e}"[:140])
            time.sleep(0.5)
        rows_out[day] = day_rows
        pickle.dump(rows_out, open(STAGE_B, "wb"))
        print(f"  {day}: {len(uni)} names, {len(day_rows)} moments")
    rows = [r for d in tdays for r in rows_out.get(d, [])]
    score = predict(model_ns, rows, feats_nospread)
    sp = Spreads(cl)
    by_day = defaultdict(list)
    for k, r in enumerate(rows):
        by_day[r["day"]].append(k)
    picked = []
    for d, ks in by_day.items():
        ks = sorted(ks, key=lambda k: -score[k])
        picked += ks[:max(1, int(len(ks) * args.top))]
    ctl = []
    import random
    rng = random.Random(3)
    for d, ks in by_day.items():
        ctl += rng.sample(ks, min(len(ks), max(1, int(len(ks) * args.top))))
    for group, ks in (("picked", picked), ("random", ctl)):
        by_t = defaultdict(list)
        for k in ks:
            by_t[rows[k]["t"]].append(rows[k]["sym"])
        for t, ss in by_t.items():
            sp.fetch(t, ss)
        sp.save()
        net = []
        dm = defaultdict(list)
        succ = []
        for k in ks:
            s = sp.get(rows[k]["t"], rows[k]["sym"])
            if s is None:
                continue
            x = rows[k]["fwd30"] - s
            net.append(x)
            dm[rows[k]["day"]].append(x)
            succ.append(rows[k]["succ"])
        m = statistics.mean(net)
        se = statistics.pstdev(net) / math.sqrt(len(net))
        print(f"Stage B {group:7} top {args.top:.0%}: {len(tdays)} days, n {len(net)}, net {m:+.1f} bp "
              f"(t {m / se:+.1f}), success {statistics.mean(succ):.1%}, "
              f"win {sum(1 for x in net if x > 0) / len(net):.1%}, "
              f"days+ {sum(1 for v in dm.values() if statistics.mean(v) > 0)}/{len(dm)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("A", "B"), default="A")
    ap.add_argument("--step", type=int, default=15, help="minutes between sampled moments")
    ap.add_argument("--days-back", type=int, default=60)
    ap.add_argument("--universe", type=int, default=600)
    ap.add_argument("--top", type=float, default=0.10)
    args = ap.parse_args()
    stage_a(args) if args.stage == "A" else stage_b(args)


if __name__ == "__main__":
    main()
