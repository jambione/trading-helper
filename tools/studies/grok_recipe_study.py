#!/usr/bin/env python3
"""grok_recipe_study.py — test the standard momentum-scalp recipe (as Grok
described it, 2026-09-30) on 60 out-of-period days.

Recipe, long side, 1m bars, all readings from COMPLETED bars:
  in play   volume pace >= 2 (RTH volume so far vs 20-day average x expected
            share of the day), close above session VWAP, EMA9 > EMA21, both
            EMAs higher than 3 bars ago
  pullback  within the last 5 bars, a bar's low touched EMA9 while its close
            held at or above EMA21, and the pullback's low is above the lowest
            low of the 10 bars before it (a higher low)
  trigger   a green bar closes back above EMA9 on volume above its 20-bar
            average, with RSI(7) > 50, still in play
  entry     the NEXT bar's open (the trigger is only known when its bar closes)
One entry per symbol per 10 minutes.

Compared with, entered the same way (next bar's open):
  in-play   every 5th in-play minute with no trigger (the filter without the timing)
  control   every 5th minute of the same names and days (no filter)
Outcomes from the entry: up2/dn2 (+2% before -1% / mirror, 60 min), steady
+5% climb/fall (dip 1%, pull 2%), fwd15/fwd30 gross bp. Net subtracts the SIP
spread at the entry minute, fetched for a sample of each group.

Windows: 09:40-11:00 (Grok's best window), 11:00-13:30 (Grok's chop),
13:30-15:15, and the lean that held out of period (up > 2.35% on the day,
10:45-11:56). Both halves and days.

USAGE (on the mini, off hours)
  .venv/bin/python tools/studies/grok_recipe_study.py [--days-back 60] [--end 2026-09-14]
"""
from __future__ import annotations

import argparse
import os
import pickle
import random
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
import combined_score_study as cs  # noqa: E402
import morning_funnel as mf  # noqa: E402
import rsi_buy_low_study as rb  # noqa: E402
import steady_runway_study as sr  # noqa: E402
import vol_clue_study as vc  # noqa: E402

ROWS = os.path.join(ROOT, "ai_reports", "grok_recipe_rows.pkl")
STEADY = (5.0, 1.0, 2.0)
COOL = 10


def outcome(B, j, last, prev_close):
    """Enter at bar j+1's open; score from there."""
    t, o, _h, _l, c, _v = B
    e = j + 1
    if e > last:
        return None
    px = o[e]
    if px <= 0:
        return None
    # scan from the entry bar itself: its whole range trades after the open fill
    up2 = vc.touch(B, e - 1, px, 2.0, 1.0)
    dn2 = vc.touch_dn(B, e - 1, px, 2.0, 1.0)
    up = sr.steady(B, e - 1, px, last)
    dn = sr.steady(B, e - 1, px, last, up=False)
    j15, j30 = e + 14, e + 29
    return {
        "t": t[e], "mins": cs.et_min(t[e]) - 570, "price": px,
        "day_chg": (c[j] / prev_close - 1) * 100,
        "up2": up2, "dn2": dn2, "sup": up[STEADY] is not None, "sdn": dn[STEADY] is not None,
        "fwd15": (c[j15] / px - 1) * 1e4 if j15 <= last and t[j15] - t[e] <= 20 * 60 else None,
        "fwd30": (c[j30] / px - 1) * 1e4 if j30 <= last and t[j30] - t[e] <= 40 * 60 else None,
    }


def rows_for(sym, day, B, prev_close, daily):
    t, o, h, l, c, v = B
    rth = [k for k in range(len(t)) if 570 <= cs.et_min(t[k]) < 960]
    if len(rth) < 300 or not prev_close:
        return []
    prior = [x for x in daily if x[0] < day][-20:]
    if len(prior) < 10:
        return []
    avg = statistics.mean(x[2] for x in prior)
    if avg <= 0:
        return []
    i0, last = rth[0], rth[-1]
    cl = [c[k] for k in range(len(c))]
    e9, e21 = rb.ema(cl, 9), rb.ema(cl, 21)
    rsi7 = rb.wilder_rsi(cl, 7)
    pv = cv = 0.0
    vwap = [None] * len(c)
    cumv = [0.0] * len(c)
    for k in range(i0, last + 1):
        tp = (h[k] + l[k] + c[k]) / 3
        pv += tp * v[k]
        cv += v[k]
        vwap[k] = pv / cv if cv > 0 else None
        cumv[k] = cv
    out = []
    last_ev = -10 ** 9
    for j in rth:
        m = cs.et_min(t[j]) + 1
        if m < 580 or m > 915 or j - i0 < 30:
            continue
        frac = mf.expected_fraction(m - 570)
        pace = cumv[j] / (avg * frac) if frac > 0 else 0.0
        in_play = (pace >= 2 and vwap[j] is not None and c[j] > vwap[j]
                   and e9[j] > e21[j] and e9[j] > e9[j - 3] and e21[j] > e21[j - 3])
        trig = False
        if in_play and j - last_ev >= COOL:
            pb = None
            for q in range(j - 5, j):
                if l[q] <= e9[q] and c[q] >= e21[q]:
                    pb = q
            if pb is not None:
                pl = min(l[pb:j])
                hl = pl > min(l[pb - 10:pb])
                avgv = sum(v[j - 20:j]) / 20
                trig = (hl and c[j] > o[j] and c[j] > e9[j] and v[j] > avgv
                        and rsi7[j] is not None and rsi7[j] > 50)
        grp = None
        if trig:
            grp = "grok"
            last_ev = j
        elif in_play and (m - 580) % 5 == 0:
            grp = "inplay"
        elif (m - 580) % 5 == 0:
            grp = "control"
        if grp is None:
            continue
        r = outcome(B, j, last, prev_close)
        if r is None:
            continue
        r.update(sym=sym, day=day, grp=grp)
        out.append(r)
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
                    start=d.replace(hour=9, minute=30).astimezone(timezone.utc),
                    end=d.replace(hour=16, minute=5).astimezone(timezone.utc), feed=DataFeed.SIP))
                for sym, br in (bs.data or {}).items():
                    B = ([r.timestamp.timestamp() for r in br], [float(r.open) for r in br],
                         [float(r.high) for r in br], [float(r.low) for r in br],
                         [float(r.close) for r in br], [float(r.volume) for r in br])
                    day_rows += rows_for(sym, day, B, prevd[sym], daily.get(sym, []))
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"  {day} bars: {e}"[:160], flush=True)
            time.sleep(args.pace)
        if ok:
            store[day] = day_rows
            pickle.dump(store, open(ROWS, "wb"))
        n = sum(r["grp"] == "grok" for r in day_rows)
        print(f"  {day}: {len(uni)} names, {n} recipe entries{'' if ok else ' (NOT SAVED)'}", flush=True)
    return [r for d in tdays for r in store.get(d, [])], tdays, cl


def attach_spreads(cl, rows, n_each, pace):
    sp = cs.Spreads(cl)
    rng = random.Random(11)
    by = defaultdict(list)
    for r in rows:
        by[r["grp"]].append(r)
    want = []
    for g, rs in by.items():
        want += rng.sample(rs, min(n_each, len(rs)))
    by_t = defaultdict(list)
    for r in want:
        by_t[int(r["t"])].append(r["sym"])
    k = 0
    for t, syms in sorted(by_t.items()):
        before = len(sp.d)
        sp.fetch(t, syms)
        if len(sp.d) != before:
            k += 1
            time.sleep(pace)
            if k % 250 == 0:
                sp.save()
                print(f"  spreads: {k} fetches", flush=True)
    sp.save()
    for r in rows:
        r["spread"] = sp.get(int(r["t"]), r["sym"])


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else float("nan")


def line(label, g):
    if not g:
        return f"    {label:<9} n=0"
    w = [r for r in g if r.get("spread") is not None and r["fwd30"] is not None]
    net = (f" | spread {mean(r['spread'] for r in w):5.1f}bp net30 {mean(r['fwd30'] - r['spread'] for r in w):+6.1f}bp"
           f" (n={len(w)})") if w else ""
    return (f"    {label:<9} n={len(g):<7} up2 {mean(r['up2'] for r in g):5.1%} dn2 {mean(r['dn2'] for r in g):5.1%} "
            f"edge {mean(r['up2'] - r['dn2'] for r in g):+5.1%} | steady5 up {mean(r['sup'] for r in g):5.2%} "
            f"dn {mean(r['sdn'] for r in g):5.2%} | fwd15 {mean(r['fwd15'] for r in g):+6.1f} "
            f"fwd30 {mean(r['fwd30'] for r in g):+6.1f}bp" + net)


def verdict(g, base, half):
    if len(g) < 100 or len(base) < 100:
        return ""
    e = lambda G: mean(r["up2"] - r["dn2"] for r in G)  # noqa: E731
    f = lambda G: mean(r["fwd30"] for r in G)  # noqa: E731
    h = []
    for hs in (True, False):
        gh = [r for r in g if (r["day"] in half) == hs]
        bh = [r for r in base if (r["day"] in half) == hs]
        h.append((f(gh) - f(bh), e(gh) - e(bh)))
    gd, bd = defaultdict(list), defaultdict(list)
    for r in g:
        gd[r["day"]].append(r)
    for r in base:
        bd[r["day"]].append(r)
    days = [d for d in gd if len(gd[d]) >= 5 and d in bd]
    wins = sum(f(gd[d]) > f(bd[d]) for d in days)
    ok = all(x[0] > 0 and x[1] > 0 for x in h) and days and wins / len(days) > 0.5
    return (f"      recipe vs control fwd30: H1 {h[0][0]:+.1f} H2 {h[1][0]:+.1f}bp, runway edge H1 {h[0][1]:+.1%} "
            f"H2 {h[1][1]:+.1%}, days {wins}/{len(days)}{'  HOLDS' if ok else '  does not hold'}")


def report(rows, tdays):
    half = set(tdays[:len(tdays) // 2])
    print(f"\nGROK RECIPE TEST {tdays[0]}..{tdays[-1]} ({len(tdays)} days, "
          f"{len({(r['sym'], r['day']) for r in rows})} name-days)")
    print("  all entries at the next bar's open; edge = up2 - dn2; net30 = fwd30 - SIP spread at entry")
    windows = [
        ("ALL DAY 09:40-15:15", lambda r: True),
        ("GROK'S WINDOW 09:40-11:00", lambda r: r["mins"] < 90),
        ("MIDDAY 11:00-13:30", lambda r: 90 <= r["mins"] < 240),
        ("AFTERNOON 13:30-15:15", lambda r: r["mins"] >= 240),
        ("UP >2.35% ON DAY, 10:45-11:56", lambda r: r["day_chg"] > 2.35 and 75 <= r["mins"] <= 146),
        ("$2-10", lambda r: r["price"] < 10),
        ("$10-50", lambda r: 10 <= r["price"] < 50),
        ("$50+", lambda r: r["price"] >= 50),
    ]
    for label, pick in windows:
        gs = {g: [r for r in rows if r["grp"] == g and pick(r)] for g in ("grok", "inplay", "control")}
        print(f"\n  [{label}]")
        print(line("RECIPE", gs["grok"]))
        print(line("in-play", gs["inplay"]))
        print(line("control", gs["control"]))
        v = verdict(gs["grok"], gs["control"], half)
        if v:
            print(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=60)
    ap.add_argument("--end", default="2026-09-14")
    ap.add_argument("--pace", type=float, default=1.0)
    ap.add_argument("--spread-sample", type=int, default=1500)
    ap.add_argument("--no-spreads", action="store_true")
    args = ap.parse_args()
    rows, tdays, cl = build(args)
    if not args.no_spreads:
        attach_spreads(cl, rows, args.spread_sample, 0.4)
    report(rows, tdays)


if __name__ == "__main__":
    main()
