#!/usr/bin/env python3
"""steady_60d_study.py — do the steady-climb leans from 9/16-9/25 hold on 60
earlier days?

steady_runway_study found, on 8 days, that a steady climb to +5% (never 1%
under the entry, never 2% off the running high) beat the mirror steady fall
most for names above $50, early minutes, names up > 2.4% on the day, and the
trending source. This re-tests them out of period.

Part A, name facts (6/17-9/11, the 60 trading days before 2026-09-14):
  universe  the Stage B symbols (combined_score_study), prior close >= $2,
            with no $100 cap so the $50+ lean can show
  moments   every 5 min, 09:45-15:30 ET, SIP 1m bars incl. premarket
  score     steady_runway_study.steady() up and down, and its features
Part B, source (desk fills 2026-08-04..09-11 from outcomes.jsonl, bars from
  tools/runway_study's cache): steady up vs down by the fill's source. The
  nomination logs start 9/16, so fills are the only source history.

A lean counts if up minus down is higher in the group than in the rest, in
BOTH halves of the 60 days, and on more than half of the days.

USAGE (on the mini, after the close)
  .venv/bin/python tools/studies/steady_60d_study.py [--days-back 60] [--end 2026-09-14]
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import combined_score_study as cs  # noqa: E402
import steady_runway_study as sr  # noqa: E402

ROWS = os.path.join(ROOT, "ai_reports", "steady_60d_rows.pkl")
FILL_BARS = os.path.join(ROOT, "ai_reports", "runway_bars_cache.pkl")
KEYS = ((5.0, 1.0, 2.0), (5.0, 1.0, 1.0), (3.0, 1.0, 2.0), (2.0, 1.0, 2.0))
MAIN = (5.0, 1.0, 2.0)
STEP = 5


def rows_for(sym, day, B, prev_close, daily):
    t = B[0]
    rth = [k for k in range(len(t)) if 570 <= cs.et_min(t[k]) < 960]
    if len(rth) < 200 or not prev_close:
        return []
    out = []
    for i in rth:
        m = cs.et_min(t[i]) + 1
        if m < 585 or m > 930 or (m - 585) % STEP or i < rth[0] + 15:
            continue
        up = sr.steady(B, i, B[4][i], rth[-1])
        dn = sr.steady(B, i, B[4][i], rth[-1], up=False)
        f = sr.feats(B, i, prev_close, daily, day, rth[0])
        out.append({"sym": sym, "day": day, "f": f,
                    "up": {k: up[k] is not None for k in KEYS},
                    "dn": {k: dn[k] is not None for k in KEYS}})
    return out


def build(args):
    cl = cs.client()
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    spy = cs.daily_bars(cl, ["SPY"], args.end, n_days=args.days_back + 5)["SPY"]
    tdays = [d for d, _c, _v in spy if d < args.end][-args.days_back:]
    universe = sorted({s for s, _d in pickle.load(open(cs.BARS, "rb"))})
    daily = cs.daily_bars(cl, universe, args.end, n_days=args.days_back + 30)
    store = pickle.load(open(ROWS, "rb")) if os.path.exists(ROWS) else {}
    for day in tdays:
        if day in store:
            continue
        prevd = {}
        for s, rows in daily.items():
            pr = [x for x in rows if x[0] < day]
            if pr and pr[-1][1] >= 2:
                prevd[s] = pr[-1][1]
        uni = sorted(prevd)
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=cs.ET)
        day_rows, ok = [], True
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
                    day_rows += rows_for(sym, day, B, prevd[sym], daily.get(sym, []))
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"  {day} bars: {e}"[:160], flush=True)
            time.sleep(0.5)
        if ok:
            store[day] = day_rows
            pickle.dump(store, open(ROWS, "wb"))
        print(f"  {day}: {len(uni)} names, {len(day_rows)} moments{'' if ok else ' (NOT SAVED)'}",
              flush=True)
    return [r for d in tdays for r in store.get(d, [])], tdays


def ud(g, key=MAIN):
    if not g:
        return float("nan"), float("nan")
    return (statistics.mean(1.0 if r["up"][key] else 0.0 for r in g),
            statistics.mean(1.0 if r["dn"][key] else 0.0 for r in g))


def lean(label, rows, pick, half):
    """Group vs rest on up-minus-down, overall, by half, and by day."""
    g = [r for r in rows if pick(r)]
    rest = [r for r in rows if not pick(r)]
    if len(g) < 200:
        print(f"  {label:<28} n={len(g)} (too few)")
        return
    gu, gd = ud(g)
    ru, rd = ud(rest)
    e_g, e_r = gu - gd, ru - rd
    halves = []
    for hs in (True, False):
        a = [r for r in g if (r["day"] in half) == hs]
        b = [r for r in rest if (r["day"] in half) == hs]
        au, ad = ud(a)
        bu, bd = ud(b)
        halves.append((au - ad) - (bu - bd))
    byday = defaultdict(lambda: ([], []))
    for r in g:
        byday[r["day"]][0].append(r)
    for r in rest:
        byday[r["day"]][1].append(r)
    wins = n = 0
    for _d, (a, b) in byday.items():
        if len(a) >= 20 and b:
            au, ad = ud(a)
            bu, bd = ud(b)
            n += 1
            wins += (au - ad) > (bu - bd)
    ok = e_g > e_r and all(h > 0 for h in halves) and n and wins / n > 0.5
    print(f"  {label:<28} n={len(g):<7} up {gu:6.2%} dn {gd:6.2%} edge {e_g:+6.2%} "
          f"(rest {e_r:+6.2%}) | H1 {halves[0]:+6.2%} H2 {halves[1]:+6.2%} | "
          f"days {wins}/{n}{'  HOLDS' if ok else ''}")


def part_a(rows, tdays):
    half = set(tdays[:len(tdays) // 2])
    print(f"\nPART A  name facts, {tdays[0]}..{tdays[-1]} ({len(tdays)} days, "
          f"{len({(r['sym'], r['day']) for r in rows})} name-days, {len(rows)} moments)")
    print("  steady climb vs steady fall, all moments:")
    for k in KEYS:
        u, d = ud(rows, k)
        print(f"    +{k[0]:g}% dip {k[1]:g}% pull {k[2]:g}%: up {u:6.2%}  dn {d:6.2%}")
    print("\n  LEANS FROM 9/16-9/25 (main rule: +5%, dip 1%, pull 2%). edge = up - dn; "
          "H1/H2/days = group edge minus rest edge")
    f = lambda r, k: r["f"].get(k)  # noqa: E731
    tests = [
        ("price >= $50", lambda r: f(r, "price") >= 50),
        ("price $20-50", lambda r: 20 <= f(r, "price") < 50),
        ("price $10-20", lambda r: 10 <= f(r, "price") < 20),
        ("price < $10", lambda r: f(r, "price") < 10),
        ("first 76 min (mins <= 146)", lambda r: f(r, "mins") <= 146),
        ("after 14:49 (mins >= 259)", lambda r: f(r, "mins") >= 259),
        ("day_chg > +2.35%", lambda r: f(r, "day_chg") > 2.35),
        ("day_chg < -0.62%", lambda r: f(r, "day_chg") < -0.62),
        ("pm_range > 3.31%", lambda r: (f(r, "pm_range") or 0) > 3.31),
        ("vol_pace > 1.12", lambda r: (f(r, "vol_pace") or 0) > 1.12),
        ("$50+ and first 76 min", lambda r: f(r, "price") >= 50 and f(r, "mins") <= 146),
        ("day_chg > 2.35 and early", lambda r: f(r, "day_chg") > 2.35 and f(r, "mins") <= 146),
        ("$50+ and day_chg > 2.35", lambda r: f(r, "price") >= 50 and f(r, "day_chg") > 2.35),
    ]
    for label, pick in tests:
        lean(label, rows, pick, half)


def part_b(end):
    if not os.path.exists(FILL_BARS):
        print("\nPART B skipped: no runway_bars_cache.pkl")
        return
    cache = pickle.load(open(FILL_BARS, "rb"))
    fills = []
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        et, px, sym = r.get("entry_time"), r.get("entry_price"), str(r.get("symbol") or "")
        if not isinstance(et, (int, float)) or not px:
            continue
        day = bars.day_of(et)
        if day < "2026-08-04" or day >= end:
            continue
        B = cache.get((sym, day))
        if not B:
            continue
        rth = [k for k in range(len(B[0])) if 570 <= cs.et_min(B[0][k]) < 960]
        i = bars.index_at(B[0], et)
        if not rth or i < rth[0] or i >= rth[-1]:
            continue
        src = str((r.get("features") or {}).get("source") or r.get("source") or "?")
        src = "research" if src in ("agy", "xai", "claude") else src
        up = sr.steady(B, i, float(px), rth[-1])
        dn = sr.steady(B, i, float(px), rth[-1], up=False)
        fills.append({"src": src, "day": day, "up": {k: up[k] is not None for k in KEYS},
                      "dn": {k: dn[k] is not None for k in KEYS}})
    days = sorted({x["day"] for x in fills})
    print(f"\nPART B  desk fills by source, {days[0] if days else '-'}..{days[-1] if days else '-'} "
          f"({len(fills)} fills with bars)")
    by = defaultdict(list)
    for x in fills:
        by[x["src"]].append(x)
    for s, g in sorted(by.items(), key=lambda kv: -len(kv[1])):
        cells = []
        for k in ((5.0, 1.0, 2.0), (3.0, 1.0, 2.0), (2.0, 1.0, 2.0)):
            u, d = ud(g, k)
            cells.append(f"+{k[0]:g}% up {u:5.1%} dn {d:5.1%}")
        print(f"  {s:<10} n={len(g):<5} " + " | ".join(cells))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=60)
    ap.add_argument("--end", default="2026-09-14")
    args = ap.parse_args()
    rows, tdays = build(args)
    part_a(rows, tdays)
    part_b(args.end)


if __name__ == "__main__":
    main()
