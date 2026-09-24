#!/usr/bin/env python3
"""Runway after fast %R -50 upward crosses on quality supply names.

OFFLINE by default: reads ai_reports/runway_bars_cache.pkl,
ai_reports/supply_first_seen.json, ai_reports/outcomes.jsonl.
Does NOT call Alpaca/Finnhub unless --fetch is passed (forbidden while
the live desk is open).

Event = first minute where smoothed fast %R crosses up through -50
(with optional slow-rising filter as a FEATURE, not a hard filter on events).
Population = names that appeared in Movers/Trending/Research that day
(supply_first_seen), RTH 09:35-15:30, days present in the bar cache.

Labels (from SIP 1m highs/lows after the cross minute, no look-ahead):
  mfe_pct_H / mae_pct_H for H in 15,30,60
  mfe_r_H / mae_r_H using stop_pct = max(1.5%, entry-to-stop if known)
  runner = mfe_r_60 >= 0.35 OR mfe_pct_60 >= 0.6 before mae hits -stop

Train days <= 2026-09-17; test >= 2026-09-18 (within available cache).
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import pickle
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ET = ZoneInfo("America/New_York")
CACHE = os.path.join(ROOT, "ai_reports", "runway_bars_cache.pkl")
SUPPLY = os.path.join(ROOT, "ai_reports", "supply_first_seen.json")
OUTCOMES = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")

FAST_LEN = 21
FAST_SPAN = 7.0
SLOW_RESAMPLE = 15  # minutes → slow %R on 15m bars, length 21
SLOW_LEN = 21
SLOW_SPAN = 3.0
LEVEL = -50.0
HORIZONS = (15, 30, 60)
DEFAULT_STOP_PCT = 1.5  # desk ai_watch_min_stop_pct ballpark when unknown


def _raw_wr(hh, ll, close):
    span = hh - ll
    if span <= 0:
        return None
    return -100.0 * (hh - close) / span


def _ewm(series, span):
    if not series:
        return []
    a = 2.0 / (span + 1.0)
    out = [series[0]]
    for v in series[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out


def percent_r_series(h, l, c, length, span):
    """Smoothed Williams %R for each bar index (None until warm)."""
    raw = []
    for i in range(len(c)):
        if i + 1 < length:
            raw.append(None)
            continue
        hh = max(h[i - length + 1:i + 1])
        ll = min(l[i - length + 1:i + 1])
        raw.append(_raw_wr(hh, ll, c[i]))
    # ewm only over defined values, propagate
    vals = [v for v in raw if v is not None]
    if not vals:
        return [None] * len(c)
    sm = _ewm(vals, span)
    out = [None] * len(c)
    j = 0
    for i, v in enumerate(raw):
        if v is None:
            continue
        out[i] = sm[j]
        j += 1
    return out


def slow_percent_r_on_1m(t, h, l, c):
    """Approximate TV slow line: %R(21) on 15m bars, forward-filled to 1m."""
    if len(c) < SLOW_RESAMPLE * SLOW_LEN:
        return [None] * len(c)
    # build 15m OHLC
    buckets = []
    i = 0
    while i < len(c):
        t0 = t[i]
        j = i
        while j < len(c) and t[j] < t0 + SLOW_RESAMPLE * 60:
            j += 1
        hh = max(h[i:j]); ll = min(l[i:j]); cc = c[j - 1]
        buckets.append((j - 1, hh, ll, cc))  # last 1m index covered
        i = j
    bh = [b[1] for b in buckets]
    bl = [b[2] for b in buckets]
    bc = [b[3] for b in buckets]
    slow_b = percent_r_series(bh, bl, bc, SLOW_LEN, SLOW_SPAN)
    out = [None] * len(c)
    prev = None
    bi = 0
    for i in range(len(c)):
        while bi < len(buckets) and buckets[bi][0] < i:
            bi += 1
        if bi < len(buckets) and buckets[bi][0] == i and slow_b[bi] is not None:
            prev = slow_b[bi]
        out[i] = prev
    return out


def ema(series, span):
    a = 2.0 / (span + 1.0)
    out = []
    e = None
    for v in series:
        e = v if e is None else a * v + (1 - a) * e
        out.append(e)
    return out


def find_crosses(B, level=LEVEL):
    """Yield (index, fast, slow, slow_rising) for upward -50 crosses."""
    t, o, h, l, c, v = B
    fast = percent_r_series(h, l, c, FAST_LEN, FAST_SPAN)
    slow = slow_percent_r_on_1m(t, h, l, c)
    out = []
    for i in range(1, len(c)):
        a, b = fast[i - 1], fast[i]
        if a is None or b is None:
            continue
        if a <= level < b:
            s0, s1 = slow[i - 1], slow[i]
            slow_rising = (s0 is not None and s1 is not None and s1 > s0)
            out.append((i, b, s1, slow_rising))
    return out


def rth_ok(ts):
    dt = datetime.fromtimestamp(ts, ET)
    mins = dt.hour * 60 + dt.minute
    return (9 * 60 + 35) <= mins <= (15 * 60 + 30), dt


def label_path(B, i0, px, stop_pct):
    t, o, h, l, c, v = B
    out = {}
    t0 = t[i0]
    stop_frac = stop_pct / 100.0
    for H in HORIZONS:
        j = bisect.bisect_right(t, t0 + H * 60) - 1
        if j <= i0 or (t[j] - t0) < H * 60 * 0.5:
            for k in (f"mfe_pct_{H}", f"mae_pct_{H}", f"mfe_r_{H}", f"mae_r_{H}"):
                out[k] = None
            continue
        up = (max(h[i0 + 1:j + 1]) / px - 1) * 100
        dn = (min(l[i0 + 1:j + 1]) / px - 1) * 100
        out[f"mfe_pct_{H}"] = up
        out[f"mae_pct_{H}"] = dn
        out[f"mfe_r_{H}"] = up / stop_pct
        out[f"mae_r_{H}"] = dn / stop_pct
    # runner: walk bar by bar 60m
    jmax = bisect.bisect_right(t, t0 + 60 * 60) - 1
    runner = 0
    mfe_r = 0.0
    for k in range(i0 + 1, jmax + 1):
        up = (h[k] / px - 1) * 100 / stop_pct
        dn = (l[k] / px - 1) * 100 / stop_pct
        if up > mfe_r:
            mfe_r = up
        if dn <= -1.0:  # hit 1R stop (~stop_pct)
            break
        if mfe_r >= 0.35 or (h[k] / px - 1) * 100 >= 0.6:
            runner = 1
            break
    out["runner"] = runner
    out["mfe_r_path"] = mfe_r
    return out


def features_at(B, i, px, meta):
    t, o, h, l, c, v = B
    f = dict(meta)
    k = i  # bar of cross — features use bars up to and including i (close=cross bar)
    # distance to HOD / swing
    hi = max(h[:k + 1]); lo = min(l[:k + 1])
    f["dist_hod_pct"] = (px / hi - 1) * 100 if hi else None
    # recent swing high: max high last 30m
    j0 = bisect.bisect_left(t, t[k] - 30 * 60)
    swing = max(h[j0:k + 1]) if k >= j0 else hi
    f["dist_swing30_pct"] = (px / swing - 1) * 100 if swing else None
    # VWAP approx
    if k >= 0:
        num = sum(((h[m] + l[m] + c[m]) / 3) * v[m] for m in range(k + 1))
        den = sum(v[:k + 1]) or 1
        vwap = num / den
        f["vs_vwap_pct"] = (px / vwap - 1) * 100
        f["above_vwap"] = 1 if px >= vwap else 0
    # EMA trend 5m / 15m on closes
    if k >= 20:
        e9 = ema(c[:k + 1], 9)
        e20 = ema(c[:k + 1], 20)
        f["ema9_gt_ema20"] = 1 if e9[-1] > e20[-1] else 0
        f["ema9_slope"] = (e9[-1] - e9[-5]) / e9[-5] * 100 if e9[-5] else 0
    # day change / used range
    f["day_chg_pct"] = (px / o[0] - 1) * 100 if o[0] else None
    day_range = (hi - lo) / o[0] * 100 if o[0] and hi > lo else None
    f["used_range_pct"] = day_range
    # rvol pace proxy: volume so far / equal-time prior day not available —
    # use today's minute volume vs session median so far
    if k >= 10:
        med = statistics.median(v[:k + 1])
        f["rvol_proxy"] = (v[k] / med) if med else None
        f["vol_sum"] = sum(v[:k + 1])
    f["price"] = px
    dt = datetime.fromtimestamp(t[k], ET)
    f["tod_bucket"] = f"{dt.hour:02d}:{(dt.minute // 30) * 30:02d}"
    f["mins_open"] = dt.hour * 60 + dt.minute - 570
    return f


def load_stop_map():
    """symbol-day -> median stop_pct from outcomes."""
    m = {}
    if not os.path.exists(OUTCOMES):
        return m
    buckets = defaultdict(list)
    with open(OUTCOMES) as f:
        for line in f:
            e = json.loads(line)
            try:
                ep = float(e.get("entry_price") or 0)
                sp = float(e.get("stop_price") or 0)
                ts = float(e.get("entry_time") or e.get("ts") or 0)
                if ep <= 0 or sp <= 0 or sp >= ep:
                    continue
                day = datetime.fromtimestamp(ts, ET).date().isoformat()
                buckets[(e.get("symbol"), day)].append((ep - sp) / ep * 100)
            except Exception:
                continue
    for k, vals in buckets.items():
        m[k] = statistics.median(vals)
    return m


def quantile_buckets(vals, xs, n=3):
    """Assign each x to tercile by vals; return list of bucket ids."""
    clean = sorted(v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v)))
    if len(clean) < n * 3:
        return [None] * len(xs)
    qs = [clean[int(len(clean) * i / n)] for i in range(1, n)]
    out = []
    for v in xs:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            out.append(None)
            continue
        b = 0
        for q in qs:
            if v >= q:
                b += 1
        out.append(b)
    return out


def summarize_feature(events, key, train_days, test_days):
    def subset(days):
        return [e for e in events if e["day"] in days and e.get(key) is not None]

    def rate(rows):
        if not rows:
            return None, 0, None
        r = sum(e["runner"] for e in rows) / len(rows)
        mfe = statistics.mean(e["mfe_r_60"] for e in rows if e.get("mfe_r_60") is not None) if any(e.get("mfe_r_60") is not None for e in rows) else None
        return r, len(rows), mfe

    tr, te = subset(train_days), subset(test_days)
    # categorical?
    sample = [e[key] for e in tr[:50]]
    cat = all(isinstance(x, (str, bool, int)) and not isinstance(x, bool) or isinstance(x, (str, bool)) for x in sample if x is not None)
    # treat tod_bucket, source, above_vwap, ema9_gt_ema20, slow_rising as cat
    if key in ("tod_bucket", "source", "above_vwap", "ema9_gt_ema20", "slow_rising") or isinstance(sample[0] if sample else None, str):
        def by_val(rows):
            g = defaultdict(list)
            for e in rows:
                g[e[key]].append(e)
            return {k: rate(v) for k, v in sorted(g.items(), key=lambda kv: -len(kv[1]))}
        return {"kind": "cat", "train": by_val(tr), "test": by_val(te)}
    # numeric terciles
    vals = [e[key] for e in tr]
    buckets = quantile_buckets(vals, [e[key] for e in tr])
    # apply train cuts to test
    clean = sorted(v for v in vals if v is not None)
    if len(clean) < 9:
        return {"kind": "num", "error": "thin"}
    cuts = [clean[len(clean) // 3], clean[2 * len(clean) // 3]]

    def assign(v):
        if v is None:
            return None
        if v < cuts[0]:
            return 0
        if v < cuts[1]:
            return 1
        return 2

    def terc(rows):
        g = defaultdict(list)
        for e in rows:
            b = assign(e[key])
            if b is not None:
                g[b].append(e)
        return {b: rate(g[b]) for b in sorted(g)}

    return {"kind": "num", "cuts": cuts, "train": terc(tr), "test": terc(te)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--json", default=os.path.join(ROOT, "ai_reports", "mid_rise_runway_study.json"))
    ap.add_argument("--min-price", type=float, default=20.0)
    ap.add_argument("--max-price", type=float, default=100.0)
    args = ap.parse_args()

    with open(args.cache, "rb") as f:
        cache = pickle.load(f)
    supply = json.load(open(SUPPLY)) if os.path.exists(SUPPLY) else {}
    stops = load_stop_map()

    events = []
    skipped = Counter()
    for (sym, day), B in cache.items():
        if B is None or not isinstance(B, tuple) or len(B) != 6:
            continue
        if day < "2026-09-01" or day > "2026-09-24":
            continue
        # supply filter: must appear in quality sources that day (if we have supply file)
        meta_src = None
        t0 = None
        if supply:
            if day not in supply or sym not in supply[day]:
                # also allow if any outcomes source quality — skip strict for days without supply file
                if day in supply:
                    skipped["not_in_supply"] += 1
                    continue
            else:
                meta_src = supply[day][sym]["src"]
                t0 = supply[day][sym]["t0"]
        t, o, h, l, c, v = B
        for i, fast, slow, slow_rising in find_crosses(B):
            ok, dt = rth_ok(t[i])
            if not ok:
                skipped["outside_rth"] += 1
                continue
            px = c[i]
            if not (args.min_price <= px <= args.max_price):
                skipped["price_band"] += 1
                continue
            if t0 is not None and t[i] < t0:
                skipped["before_source"] += 1
                continue
            stop_pct = stops.get((sym, day), DEFAULT_STOP_PCT)
            lab = label_path(B, i, px, stop_pct)
            if lab.get("mfe_pct_60") is None:
                skipped["short_path"] += 1
                continue
            feat = features_at(B, i, px, {
                "symbol": sym, "day": day, "source": meta_src or "?",
                "fast": fast, "slow": slow, "slow_rising": 1 if slow_rising else 0,
            })
            if t0 is not None:
                feat["mins_since_source"] = max(0.0, (t[i] - t0) / 60.0)
            else:
                feat["mins_since_source"] = None
            feat["gap_pct"] = None  # needs prior close — not in 1m day file alone
            ev = {**feat, **lab, "ts": t[i], "stop_pct": stop_pct}
            events.append(ev)

    days = sorted({e["day"] for e in events})
    train_days = {d for d in days if d <= "2026-09-17"}
    test_days = {d for d in days if d >= "2026-09-18"}
    print(f"events={len(events)} days={days}", file=sys.stderr)
    print(f"skipped={dict(skipped)}", file=sys.stderr)
    print(f"train_days={sorted(train_days)} test_days={sorted(test_days)}", file=sys.stderr)
    base_tr = [e for e in events if e["day"] in train_days]
    base_te = [e for e in events if e["day"] in test_days]
    def base_rate(rows):
        return (sum(e["runner"] for e in rows) / len(rows) if rows else None, len(rows),
                statistics.mean(e["mfe_r_60"] for e in rows if e["mfe_r_60"] is not None) if rows else None)
    print("TRAIN base", base_rate(base_tr), file=sys.stderr)
    print("TEST  base", base_rate(base_te), file=sys.stderr)

    feat_keys = [
        "dist_hod_pct", "dist_swing30_pct", "vs_vwap_pct", "above_vwap",
        "ema9_gt_ema20", "ema9_slope", "day_chg_pct", "used_range_pct",
        "rvol_proxy", "price", "tod_bucket", "mins_open", "source",
        "slow", "slow_rising", "mins_since_source", "fast",
    ]
    reports = {}
    for key in feat_keys:
        reports[key] = summarize_feature(events, key, train_days, test_days)

    # simple 1-2 feature rules from train lift
    rules = []
    # rule helpers
    def apply_rule(rows, pred):
        kept = [e for e in rows if pred(e)]
        return base_rate(kept)

    # candidates from known directions
    candidates = [
        ("above_vwap==1", lambda e: e.get("above_vwap") == 1),
        ("ema9_gt_ema20==1", lambda e: e.get("ema9_gt_ema20") == 1),
        ("slow_rising==1", lambda e: e.get("slow_rising") == 1),
        ("dist_hod_pct > -2", lambda e: e.get("dist_hod_pct") is not None and e["dist_hod_pct"] > -2),
        ("dist_hod_pct > -1", lambda e: e.get("dist_hod_pct") is not None and e["dist_hod_pct"] > -1),
        ("day_chg_pct >= 3", lambda e: e.get("day_chg_pct") is not None and e["day_chg_pct"] >= 3),
        ("day_chg_pct >= 5", lambda e: e.get("day_chg_pct") is not None and e["day_chg_pct"] >= 5),
        ("mins_open < 90", lambda e: e.get("mins_open") is not None and e["mins_open"] < 90),
        ("mins_open >= 90", lambda e: e.get("mins_open") is not None and e["mins_open"] >= 90),
        ("source movers", lambda e: e.get("source") == "movers"),
        ("source trending", lambda e: e.get("source") == "trending"),
        ("price 20-50", lambda e: e.get("price") is not None and 20 <= e["price"] < 50),
        ("vwap+ema", lambda e: e.get("above_vwap") == 1 and e.get("ema9_gt_ema20") == 1),
        ("vwap+slow_rising", lambda e: e.get("above_vwap") == 1 and e.get("slow_rising") == 1),
        ("hod_near+vwap", lambda e: e.get("dist_hod_pct") is not None and e["dist_hod_pct"] > -2 and e.get("above_vwap") == 1),
        ("hod_near+ema", lambda e: e.get("dist_hod_pct") is not None and e["dist_hod_pct"] > -2 and e.get("ema9_gt_ema20") == 1),
    ]
    for name, pred in candidates:
        tr = apply_rule(base_tr, pred)
        te = apply_rule(base_te, pred)
        rules.append({"rule": name, "train": tr, "test": te})

    # ranking score proposal pieces
    result = {
        "n_events": len(events),
        "days": days,
        "skipped": dict(skipped),
        "train_base": base_rate(base_tr),
        "test_base": base_rate(base_te),
        "features": reports,
        "rules": rules,
        "events_sample": [
            {k: e.get(k) for k in ("symbol", "day", "source", "price", "fast", "slow",
                                   "slow_rising", "dist_hod_pct", "above_vwap", "ema9_gt_ema20",
                                   "day_chg_pct", "runner", "mfe_r_60", "mfe_pct_60", "mae_pct_60",
                                   "tod_bucket", "mins_since_source")}
            for e in events[:20]
        ],
    }
    with open(args.json, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"wrote {args.json}", file=sys.stderr)

    # human summary
    print("\n=== BASE ===")
    print("train", result["train_base"])
    print("test ", result["test_base"])
    print("\n=== RULES (train rate, n | test rate, n) ===")
    br_tr = result["train_base"][0] or 0
    br_te = result["test_base"][0] or 0
    for r in rules:
        tr, te = r["train"], r["test"]
        if not tr[1] or not te[1]:
            continue
        print(f"{r['rule']:22s}  tr {tr[0]:.3f} n={tr[1]:4d} (lift {tr[0]-br_tr:+.3f})  "
              f"te {te[0]:.3f} n={te[1]:4d} (lift {te[0]-br_te:+.3f})")


if __name__ == "__main__":
    main()
