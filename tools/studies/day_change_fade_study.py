#!/usr/bin/env python3
"""Day-change / rvol / RS-vs-SPY buckets at mid_rise −50 crosses: runners or fades?

Reuses mid_rise_runway_study cross detection and runner label (≥0.35R in 60m
before −1R). Adds close-to-close ret_30 / ret_60 and first-touch −1% before +1%.

Features at the cross (no look-ahead):
  day_chg_pct   (close/open − 1) * 100
  rs_spy        day_chg − SPY day_chg to the same minute
  rvol_pace     SIP volume so far / (20-day avg daily SIP volume × expected
                fraction of the session by now). Same definition as
                runway_target_study / morning_funnel.expected_fraction.
                Falls back to None when avg volume is unavailable.

Train days ≤ 2026-09-17; test ≥ 2026-09-18 (same split as mid_rise_runway_study).

Offline by default (local runway_bars_cache.pkl + supply_first_seen.json).
Pass --fetch-daily to pull 20-day daily SIP volumes for rvol_pace (1 multi-
symbol request/sec). Does not write the 1m bar cache unless --write-cache.
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
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

_HERE = os.path.abspath(__file__)
if _HERE.startswith("/tmp/") or os.path.basename(os.path.dirname(_HERE)) != "studies":
    ROOT = os.environ.get("TH_ROOT") or os.getcwd()
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
# Prefer sibling mid_rise from /tmp when present
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

# Allow supply / cache overrides when script lives in /tmp
SUPPLY_CANDIDATES = [
    os.environ.get("SUPPLY_PATH", ""),
    "/tmp/supply_first_seen.json",
    os.path.join(ROOT, "ai_reports", "supply_first_seen.json"),
]
CACHE_CANDIDATES = [
    os.environ.get("RUNWAY_CACHE", ""),
    "/tmp/runway_bars_cache.pkl",
    os.path.join(ROOT, "ai_reports", "runway_bars_cache.pkl"),
]

import bars  # noqa: E402
import mid_rise_runway_study as mr  # noqa: E402

try:
    from morning_funnel import expected_fraction
except Exception:  # noqa: BLE001
    # Minimal copy of the live curve if import path is odd under /tmp
    _CURVE = [(0, 0.05), (30, 0.18), (60, 0.28), (90, 0.36), (120, 0.44),
              (180, 0.58), (240, 0.72), (300, 0.86), (390, 1.0)]

    def expected_fraction(mins_since_open: float) -> float:
        if mins_since_open < 0:
            return max(0.01, 0.05 * (mins_since_open + 330) / 330)
        if mins_since_open >= _CURVE[-1][0]:
            return 1.0
        for (x0, y0), (x1, y1) in zip(_CURVE, _CURVE[1:]):
            if mins_since_open <= x1:
                return y0 + (y1 - y0) * (mins_since_open - x0) / (x1 - x0)
        return 1.0


DAY_CHG_BUCKETS = [
    ("<0", lambda x: x < 0),
    ("0-3", lambda x: 0 <= x < 3),
    ("3-5", lambda x: 3 <= x < 5),
    ("5-8", lambda x: 5 <= x < 8),
    ("8-15", lambda x: 8 <= x < 15),
    ("15+", lambda x: x >= 15),
]
RVOL_BUCKETS = [
    ("<0.8", lambda x: x < 0.8),
    ("0.8-1.2", lambda x: 0.8 <= x < 1.2),
    ("1.2-1.64", lambda x: 1.2 <= x < 1.64),
    ("1.64-2.5", lambda x: 1.64 <= x < 2.5),
    ("2.5-4", lambda x: 2.5 <= x < 4),
    ("4+", lambda x: x >= 4),
]
RS_BUCKETS = [
    ("<0", lambda x: x < 0),
    ("0-1", lambda x: 0 <= x < 1),
    ("1-3", lambda x: 1 <= x < 3),
    ("3-5", lambda x: 3 <= x < 5),
    ("5-8", lambda x: 5 <= x < 8),
    ("8+", lambda x: x >= 8),
]


def _first_path(cands):
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None


def ret_at(B, i0, px, mins):
    t, _o, _h, _l, c, _v = B
    j = bisect.bisect_right(t, t[i0] + mins * 60) - 1
    if j <= i0 or (t[j] - t[i0]) < mins * 60 * 0.5:
        return None
    return (c[j] / px - 1) * 100


def hit_dn_before_up(B, i0, px, up=1.0, dn=1.0, horizon=60):
    """1 if −dn% prints before +up% within horizon; 0 if up first; None if neither."""
    t, _o, h, l, _c, _v = B
    hi, lo = px * (1 + up / 100), px * (1 - dn / 100)
    end = t[i0] + horizon * 60
    for k in range(i0 + 1, len(t)):
        if t[k] > end:
            break
        if l[k] <= lo:
            return 1
        if h[k] >= hi:
            return 0
    return None


def avg_daily_volume(syms, days, sleep_s=1.0):
    """{(sym, day): 20-day avg daily SIP volume before that day}."""
    cl = bars.client()
    if cl is None:
        print("no Alpaca client — rvol_pace skipped", file=sys.stderr)
        return {}
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    start = datetime.strptime(min(days), "%Y-%m-%d") - timedelta(days=45)
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    out = {}
    syms = sorted(set(syms))
    for i in range(0, len(syms), 100):
        batch = syms[i:i + 100]
        try:
            data = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame.Day,
                start=start.replace(tzinfo=timezone.utc), end=end,
                feed=DataFeed.SIP)).data
        except Exception as e:  # noqa: BLE001
            print(f"avgvol fail: {str(e)[:100]}", file=sys.stderr)
            time.sleep(sleep_s)
            continue
        for s, seq in data.items():
            dlist = [bars.day_of(b.timestamp.timestamp()) for b in seq]
            vols = [float(b.volume) for b in seq]
            for d in days:
                if d in dlist:
                    k = dlist.index(d)
                    if k >= 20:
                        out[(str(s), d)] = sum(vols[k - 20:k]) / 20.0
        print(f"  daily vol batch {i // 100 + 1}: {len(batch)} syms, "
              f"have {len(out)} (sym,day) avgs", file=sys.stderr)
        time.sleep(sleep_s)
    return out


def bucketize(val, specs):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    for name, pred in specs:
        if pred(val):
            return name
    return None


def summarize(rows, label):
    if not rows:
        print(f"  {label:<10} n=   0")
        return
    n = len(rows)
    run = sum(e["runner"] for e in rows) / n
    r30 = [e["ret_30"] for e in rows if e.get("ret_30") is not None]
    r60 = [e["ret_60"] for e in rows if e.get("ret_60") is not None]
    fade = [e["fade"] for e in rows if e.get("fade") is not None]
    fade_pct = (sum(fade) / len(fade)) if fade else float("nan")
    m30 = statistics.mean(r30) if r30 else float("nan")
    m60 = statistics.mean(r60) if r60 else float("nan")
    print(f"  {label:<10} n={n:>4}  runner {run:5.1%}  "
          f"ret30 {m30:+6.3f}%  ret60 {m60:+6.3f}%  "
          f"hit-1%bef+1% {fade_pct:5.1%} (n_decided={len(fade)})")


def table(events, key, specs, train_days, test_days):
    print(f"\n=== {key} buckets ===")
    for half_name, days in (("TRAIN", train_days), ("TEST", test_days)):
        print(f"-- {half_name} {sorted(days)} --")
        sub = [e for e in events if e["day"] in days]
        summarize(sub, "ALL")
        by = defaultdict(list)
        missing = 0
        for e in sub:
            b = bucketize(e.get(key), specs)
            if b is None:
                missing += 1
                continue
            by[b].append(e)
        for name, _ in specs:
            summarize(by.get(name, []), name)
        if missing:
            print(f"  (missing {key}: {missing})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-price", type=float, default=20.0)
    ap.add_argument("--max-price", type=float, default=100.0)
    ap.add_argument("--fetch-daily", action="store_true",
                    help="Fetch 20-day daily SIP volumes for rvol_pace")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    cache_path = _first_path(CACHE_CANDIDATES)
    supply_path = _first_path(SUPPLY_CANDIDATES)
    if not cache_path:
        raise SystemExit("no runway_bars_cache.pkl found")
    print(f"cache={cache_path}", file=sys.stderr)
    print(f"supply={supply_path}", file=sys.stderr)

    with open(cache_path, "rb") as f:
        cache = pickle.load(f)
    supply = json.load(open(supply_path)) if supply_path else {}
    stops = mr.load_stop_map()

    events = []
    skipped = Counter()
    for (sym, day), B in cache.items():
        if B is None or not isinstance(B, tuple) or len(B) != 6:
            continue
        if day < "2026-09-01" or day > "2026-09-24":
            continue
        meta_src = None
        t0 = None
        if supply:
            if day in supply and sym not in supply[day]:
                skipped["not_in_supply"] += 1
                continue
            if day in supply and sym in supply[day]:
                meta_src = supply[day][sym]["src"]
                t0 = supply[day][sym]["t0"]
        t, o, h, l, c, v = B
        spy = cache.get(("SPY", day))
        for i, fast, slow, slow_rising in mr.find_crosses(B):
            ok, dt = mr.rth_ok(t[i])
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
            stop_pct = stops.get((sym, day), mr.DEFAULT_STOP_PCT)
            lab = mr.label_path(B, i, px, stop_pct)
            if lab.get("mfe_pct_60") is None:
                skipped["short_path"] += 1
                continue
            day_chg = (px / o[0] - 1) * 100 if o[0] else None
            rs = None
            if spy and day_chg is not None:
                j = bars.index_at(spy[0], t[i])
                if j >= 0 and spy[1][0]:
                    spy_chg = (spy[4][j] / spy[1][0] - 1) * 100
                    rs = day_chg - spy_chg
            vol_so_far = sum(v[:i + 1])
            mins_open = dt.hour * 60 + dt.minute - 570
            events.append({
                "symbol": sym, "day": day, "source": meta_src or "?",
                "ts": t[i], "price": px, "day_chg_pct": day_chg, "rs_spy": rs,
                "vol_so_far": vol_so_far, "mins_open": mins_open,
                "runner": lab["runner"],
                "ret_30": ret_at(B, i, px, 30),
                "ret_60": ret_at(B, i, px, 60),
                "fade": hit_dn_before_up(B, i, px, 1.0, 1.0, 60),
                "mfe_r_60": lab.get("mfe_r_60"),
                "i": i,
            })

    days = sorted({e["day"] for e in events})
    train_days = {d for d in days if d <= "2026-09-17"}
    test_days = {d for d in days if d >= "2026-09-18"}
    print(f"events={len(events)} days={days}", file=sys.stderr)
    print(f"skipped={dict(skipped)}", file=sys.stderr)
    print(f"train={sorted(train_days)} test={sorted(test_days)}", file=sys.stderr)

    # rvol_pace
    av = {}
    if args.fetch_daily:
        syms = {e["symbol"] for e in events}
        print(f"fetching daily volumes for {len(syms)} symbols…", file=sys.stderr)
        av = avg_daily_volume(syms, days, sleep_s=1.0)
    n_rvol = 0
    for e in events:
        avgv = av.get((e["symbol"], e["day"]))
        e["rvol_pace"] = None
        if avgv and avgv > 0:
            frac = expected_fraction(e["mins_open"])
            if frac > 0:
                e["rvol_pace"] = e["vol_so_far"] / (avgv * frac)
                n_rvol += 1
    print(f"rvol_pace available on {n_rvol}/{len(events)} events "
          f"(measure=SIP vol so far / (20d avg daily SIP × expected_fraction))",
          file=sys.stderr)

    print("\n=== BASE ===")
    summarize([e for e in events if e["day"] in train_days], "TRAIN")
    summarize([e for e in events if e["day"] in test_days], "TEST")

    table(events, "day_chg_pct", DAY_CHG_BUCKETS, train_days, test_days)
    table(events, "rvol_pace", RVOL_BUCKETS, train_days, test_days)
    table(events, "rs_spy", RS_BUCKETS, train_days, test_days)

    # Sweet-spot scan: contiguous day_chg bands that beat ALL on runner in BOTH halves
    print("\n=== SWEET-SPOT SCAN (day_chg): runner vs ALL, both halves ===")
    bands = [(0, 3), (0, 5), (3, 5), (3, 8), (3, 15), (5, 8), (5, 15),
             (8, 15), (8, 99), (15, 99)]
    for lo, hi in bands:
        def pred(e, lo=lo, hi=hi):
            x = e.get("day_chg_pct")
            return x is not None and lo <= x < hi

        for half_name, days_ in (("tr", train_days), ("te", test_days)):
            all_r = [e for e in events if e["day"] in days_]
            keep = [e for e in all_r if pred(e)]
            if not all_r:
                continue
            base = sum(e["runner"] for e in all_r) / len(all_r)
            rate = (sum(e["runner"] for e in keep) / len(keep)) if keep else float("nan")
            print(f"  day_chg [{lo},{hi}) {half_name}: n={len(keep):>4} "
                  f"runner {rate:5.1%} (base {base:5.1%} lift {rate - base:+.1%})")

    if args.json:
        out = {
            "n": len(events), "days": days,
            "train_days": sorted(train_days), "test_days": sorted(test_days),
            "rvol_n": n_rvol,
            "rvol_measure": "SIP vol so far / (20d avg daily SIP * expected_fraction)",
        }
        with open(args.json, "w") as f:
            json.dump(out, f, indent=2)
        print(f"wrote {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
