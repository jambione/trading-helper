#!/usr/bin/env python3
"""Is the fade tradable short? The desk's supply names, shorted at listing/admission.

The one large, stable effect in this lab is that names on the desk's lists fade
(the-universe-fades-at-every-horizon: -0.32% at 30m to -1.23% to the close,
41% long winners; admission bleed -1.81%). A long-only desk fights it. This
asks whether the other side pays after costs — and what the squeeze tail costs.

Events (one per name-day per anchor, no look-ahead):
  listed    first admit-ledger row for the name that day (any stage)
  admitted  first admit_ts per name-day in shadow.jsonl (the admission
            instant; the admit ledger logs only refusals)
Anchor = max(event, 09:35 ET). Entry = open of the 2nd bar after the anchor
minute (the obtainable fill, as the fade studies used). Short P&L % =
(entry - exit) / entry * 100 - round-trip cost by price tier ($10+: 0.20%,
$5-10: 0.40%, <$5: 1.00% — tick size). Exits: close of the bar H minutes on
(30/60/120) or 15:55. Squeeze = max high after entry vs entry. Stops: buy-stop
at +S%, filled at max(stop, that bar's open) + 0.10% slippage.

Not modelled: borrow availability/fees (small caps are often hard to borrow),
short-sale restriction after a -10% day (uptick rule), locate delays. Cheap
names are reported separately for that reason.

Split: train <= 2026-09-18, test >= 2026-09-21; per-day sign; symbol-day
weighting is inherent (one event per name-day per anchor).

Usage (on the mini)::

    .venv/bin/python tools/studies/short_fade_study.py
"""
from __future__ import annotations

import json
import os
import pickle
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
ET = ZoneInfo("America/New_York")
CACHE = os.path.join(ROOT, "ai_reports", "runway_bars_cache.pkl")      # read only
OWN_CACHE = os.path.join(ROOT, "ai_reports", "short_fade_bars.pkl")   # our fetches
DAYS = ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22",
        "2026-09-23", "2026-09-24", "2026-09-25"]
TRAIN_LAST = "2026-09-18"
HORIZONS = (30, 60, 120, "close")


def at(day: str, hh: int, mm: int) -> float:
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=ET).timestamp()


def cost(px: float) -> float:
    return 0.20 if px >= 10 else 0.40 if px >= 5 else 1.00


def listed_events(day: str) -> list[dict]:
    p = os.path.join(ROOT, "ai_reports", "admit_ledger", f"{day}.jsonl")
    first = {}
    with open(p) as f:
        for line in f:
            if '"symbol"' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            s = str(r.get("symbol") or "").upper()
            if s and "." not in s and s not in first:
                first[s] = (float(r.get("ts") or 0), str(r.get("source") or ""))
    return [{"day": day, "sym": s, "anchor": "listed", "t": ts, "src": src}
            for s, (ts, src) in first.items()]


def admitted_events(days: list[str]) -> list[dict]:
    """First admit_ts per name-day from shadow.jsonl (streamed; 800 MB)."""
    import re
    t0 = at(days[0], 0, 0)
    t1 = at(days[-1], 23, 59)
    pat = re.compile(r'^\{"ts": ([0-9.]+)')
    first: dict[tuple[str, str], tuple[float, str]] = {}
    with open(os.path.join(ROOT, "ai_reports", "shadow.jsonl")) as f:
        for line in f:
            m = pat.match(line)
            if not m or not (t0 <= float(m.group(1)) <= t1):
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            s = str(r.get("symbol") or "").upper()
            a = r.get("admit_ts")
            if not s or "." in s or not isinstance(a, (int, float)):
                continue
            d = datetime.fromtimestamp(float(a), ET).strftime("%Y-%m-%d")
            if d not in days:
                continue
            k = (s, d)
            if k not in first or float(a) < first[k][0]:
                first[k] = (float(a), str(r.get("source") or ""))
    return [{"day": d, "sym": s, "anchor": "admitted", "t": ts, "src": src}
            for (s, d), (ts, src) in first.items()]


def load_bars(events: list[dict]) -> dict:
    cache = pickle.load(open(CACHE, "rb")) if os.path.exists(CACHE) else {}
    own = pickle.load(open(OWN_CACHE, "rb")) if os.path.exists(OWN_CACHE) else {}
    cache = {**cache, **own}
    need = sorted({(e["sym"], e["day"]) for e in events} - set(cache))
    if need:
        import ai_entry_watch as ew
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        cl = ew._data_client()
        by_day = defaultdict(list)
        for s, d in need:
            by_day[d].append(s)
        for d, syms in by_day.items():
            for i in range(0, len(syms), 100):
                batch = syms[i:i + 100]
                data = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=batch, timeframe=TimeFrame.Minute,
                    start=datetime.fromtimestamp(at(d, 9, 30), timezone.utc),
                    end=datetime.fromtimestamp(at(d, 16, 0), timezone.utc),
                    feed=DataFeed.SIP)).data
                for s in batch:
                    b = data.get(s) or []
                    own[(s, d)] = cache[(s, d)] = ([x.timestamp.timestamp() for x in b], [float(x.open) for x in b],
                                     [float(x.high) for x in b], [float(x.low) for x in b],
                                     [float(x.close) for x in b], [float(x.volume) for x in b])
                time.sleep(0.4)
        pickle.dump(own, open(OWN_CACHE, "wb"))
    return cache


def score(e: dict, B, stops=(3.0, 5.0, 10.0)) -> dict | None:
    t, o, h, l, c, _v = B
    if len(t) < 60:
        return None
    a = max(e["t"], at(e["day"], 9, 35))
    if a > at(e["day"], 15, 0):
        return None
    # anchor minute = bar containing a; entry = open of the 2nd bar after it
    idx = next((k for k, ts in enumerate(t) if ts > a), None)
    if idx is None or idx + 1 >= len(t):
        return None
    i = idx + 1
    px = o[i]
    if not px or px <= 0 or px < 1.0:
        return None
    day_open = o[0]
    close_i = max(k for k, ts in enumerate(t) if ts <= at(e["day"], 15, 55))
    if close_i <= i:
        return None
    row = {**e, "px": px, "cost": cost(px),
           "since_open": (px / day_open - 1) * 100 if day_open else None,
           "rng_pos": None}
    pre_h, pre_l = max(h[:i]) if i else px, min(l[:i]) if i else px
    if pre_h > pre_l:
        row["rng_pos"] = (px - pre_l) / (pre_h - pre_l) * 100
    for H in HORIZONS:
        j = close_i if H == "close" else min(i + H - 1, close_i)
        gross = (px - c[j]) / px * 100
        row[f"r{H}"] = gross - row["cost"]
        row[f"sq{H}"] = (max(h[i:j + 1]) / px - 1) * 100
        for S in stops:
            stop = px * (1 + S / 100)
            res = None
            for k in range(i, j + 1):
                if h[k] >= stop:
                    fill = max(stop, o[k]) * 1.001
                    res = (px - fill) / px * 100 - row["cost"]
                    break
            row[f"r{H}_s{int(S)}"] = res if res is not None else row[f"r{H}"]
    return row


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))]


def line(rows, key, label):
    v = [r[key] for r in rows if r.get(key) is not None]
    if not v:
        return f"  {label:34s} n=0"
    se = statistics.pstdev(v) / len(v) ** 0.5 if len(v) > 1 else 0
    return (f"  {label:34s} n={len(v):4d}  mean {statistics.mean(v):+6.2f}%  (t {statistics.mean(v) / se if se else 0:+4.1f})"
            f"  median {statistics.median(v):+6.2f}%  win {sum(1 for x in v if x > 0) / len(v):4.0%}"
            f"  worst5% {pct(v, 0.05):+6.2f}%")


def main() -> None:
    events = [e for d in DAYS for e in listed_events(d)] + admitted_events(DAYS)
    bars = load_bars(events)
    rows = [r for e in events if (B := bars.get((e["sym"], e["day"]))) and (r := score(e, B))]
    print(f"short-the-fade: {len(rows)} events over {len({r['day'] for r in rows})} days "
          f"({sum(r['anchor'] == 'listed' for r in rows)} listed, "
          f"{sum(r['anchor'] == 'admitted' for r in rows)} admitted)\n")
    for anchor in ("listed", "admitted"):
        R = [r for r in rows if r["anchor"] == anchor]
        if not R:
            print(f"=== {anchor.upper()}: no events ===\n")
            continue
        print(f"=== {anchor.upper()} (short at the 2nd bar open after the anchor; net of cost) ===")
        for H in HORIZONS:
            print(line(R, f"r{H}", f"hold {H}"))
        print("  squeeze against the short (max high after entry):")
        for H in (60, "close"):
            v = [r[f"sq{H}"] for r in R]
            if not v:
                continue
            print(f"    to {H}: p50 {pct(v, .5):+.1f}%  p90 {pct(v, .9):+.1f}%  p95 {pct(v, .95):+.1f}%  "
                  f"p99 {pct(v, .99):+.1f}%  hit +5% {sum(x >= 5 for x in v) / len(v):.0%}  "
                  f"+10% {sum(x >= 10 for x in v) / len(v):.0%}  +20% {sum(x >= 20 for x in v) / len(v):.0%}")
        print("  with a hard buy-stop (fill at stop or gap-through open, +0.1%):")
        for S in (3, 5, 10):
            print(line(R, f"rclose_s{S}", f"to close, stop +{S}%"))
        print("  held out:")
        for H in (60, "close"):
            print(line([r for r in R if r["day"] <= TRAIN_LAST], f"r{H}", f"train <=09-18 hold {H}"))
            print(line([r for r in R if r["day"] > TRAIN_LAST], f"r{H}", f"test  >=09-21 hold {H}"))
        per_day = defaultdict(list)
        for r in R:
            per_day[r["day"]].append(r["rclose_s10"])
        print("  per day (to close, stop +10%): " + "  ".join(
            f"{d[5:]} {statistics.mean(v):+.2f}%(n{len(v)})" for d, v in sorted(per_day.items())))
        print("  by price (to close, stop +10%; <$5 is where borrow is hardest):")
        for lo, hi, lab in ((1, 5, "$1-5"), (5, 10, "$5-10"), (10, 20, "$10-20"),
                            (20, 100, "$20-100"), (100, 1e9, "$100+")):
            print(line([r for r in R if lo <= r["px"] < hi], "rclose_s10", lab))
        print("  by move since the open at entry (to close, stop +10%):")
        for lo, hi, lab in ((-1e9, 0, "below open"), (0, 5, "0 to +5%"), (5, 15, "+5 to +15%"),
                            (15, 30, "+15 to +30%"), (30, 1e9, "+30%+")):
            print(line([r for r in R if r["since_open"] is not None and lo <= r["since_open"] < hi],
                       "rclose_s10", lab))
        print("  by range position at entry (to close, stop +10%):")
        for lo, hi, lab in ((0, 50, "bottom half"), (50, 90, "50-90"), (90, 101, "at the highs 90+")):
            print(line([r for r in R if r["rng_pos"] is not None and lo <= r["rng_pos"] < hi],
                       "rclose_s10", lab))
        print()


if __name__ == "__main__":
    main()
