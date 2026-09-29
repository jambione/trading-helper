#!/usr/bin/env python3
"""vol_pace_60d_study.py — does volume pace still find runway out of period?

runway_target_study (2026-09-23, 8 days 09-14..23, book names >= $20) found
that rvol_pace, volume so far vs the stock's own normal pace, lifted
+2%-before--1% runway from 4% to 16%. vol_trail_book_study then found that
rvol_pace >= 1.64 ("S7") was the only config positive in both halves. Both
came from the same 8 days. This re-tests them on 60 earlier trading days that
neither study saw, ending 2026-09-11.

Universe: Stage B's (combined_score_study). The 759 symbols from the recorded
days, $20-$100 by the PRIOR day's close. Those names were chosen because they
traded on our book in September, so the names carry some hindsight. The
moments and outcomes do not.

Moments: every 15 min, 09:45-15:15 ET, SIP 1m bars. Features, known at the moment:
  vol_pace   RTH volume so far / (20-day avg daily volume x expected share of
             the day by now), as morning_funnel.expected_fraction defines it
             (same definition as runway_target_study)
  vol_1m     stdev of the 15 one-minute returns ending at the moment, %
  day_chg, room_hod, price
Labels, from the moment's close:
  run2     +2% before -1% within 60 min (neither = 0)
  run15    +1.5% before -1%
  succ1    +1% before -1%
  runk     +1 sigma60 before -0.5 sigma60, sigma60 = vol_1m x sqrt(60). The
           same 2:1 shape in the name's own units. A name that runs only
           because it is jumpy does not win this one.
  symk     +0.75 sigma60 before -0.75 sigma60. Symmetric control: if pace
           lifts runk but not symk, pace carries direction, not just size.
  fwd30, fwd60   close-to-close gross, bp
  ex_flat  exit sim: 1% seed, arm +0.3%, trail 0.35% from the high, 120 min
  ex_vol   same with trail max(0.35%, 4 x vol_1m)
Net = gross minus the SIP spread at the moment (Stage B's cache, same keys).

Tests: vol_pace terciles and the S7 cut (>= 1.64) vs the rest, each half of
the 60 days, days positive, and inside vol_1m terciles (does pace add
anything once volatility is known?).

USAGE (on the mini, after the close; Alpaca data keys; caches in ai_reports/)
  .venv/bin/python tools/studies/vol_pace_60d_study.py [--days-back 60] [--end 2026-09-14]
"""
from __future__ import annotations

import argparse
import math
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
import exit_study as ex  # noqa: E402
import morning_funnel as mf  # noqa: E402

ROWS = os.path.join(ROOT, "ai_reports", "vol_pace_60d_rows.pkl")
S7 = 1.64
STEP = 15


def first_touch(B, i, px, up, dn, horizon=60):
    t, _o, h, l, _c, _v = B
    end = t[i] + horizon * 60
    for k in range(i + 1, len(t)):
        if t[k] > end:
            break
        if l[k] <= px * (1 - dn / 100):
            return 0
        if h[k] >= px * (1 + up / 100):
            return 1
    return 0


def rows_for(sym, day, B, prev_close, daily):
    t, o, h, l, c, v = B
    rth = [i for i in range(len(c)) if 570 <= cs.et_min(t[i]) < 960]
    if len(rth) < 200 or not prev_close:
        return []
    prior = [x for x in daily if x[0] < day][-20:]
    if len(prior) < 10:
        return []
    avg_vol = statistics.mean(x[2] for x in prior)
    i0 = rth[0]
    out, hi, cumv = [], None, 0.0
    for i in rth:
        hi = h[i] if hi is None else max(hi, h[i])
        cumv += v[i]
        m = cs.et_min(t[i]) + 1
        if not (585 <= m <= 915) or (m - 585) % STEP or i - i0 < 15:
            continue
        px = c[i]
        if not (20 <= px <= 100):
            continue
        j60 = i + 60
        if j60 >= len(t) or t[j60] - t[i] > 75 * 60:
            continue
        rets = [(c[j] / c[j - 1] - 1) * 100 for j in range(i - 14, i + 1)]
        vol_1m = statistics.stdev(rets)
        frac = mf.expected_fraction(m - 570)
        sig = vol_1m * math.sqrt(60)
        j30 = i + 30
        g_flat, _ = ex.walk(B, i, px, stop_pct=1.0, arm_pct=0.3, trail_pct=0.35, horizon=120)
        g_vol, _ = ex.walk(B, i, px, stop_pct=1.0, arm_pct=0.3,
                           trail_pct=max(0.35, 4 * vol_1m), horizon=120)
        out.append({
            "sym": sym, "day": day, "t": t[i] + 60,
            "vol_pace": cumv / (avg_vol * frac) if avg_vol > 0 and frac > 0 else None,
            "vol_1m": vol_1m, "price": px,
            "day_chg": (px / prev_close - 1) * 100, "room_hod": (1 - px / hi) * 100,
            "run2": first_touch(B, i, px, 2.0, 1.0),
            "run15": first_touch(B, i, px, 1.5, 1.0),
            "succ1": first_touch(B, i, px, 1.0, 1.0),
            "runk": first_touch(B, i, px, sig, sig / 2) if sig > 0 else 0,
            "symk": first_touch(B, i, px, 0.75 * sig, 0.75 * sig) if sig > 0 else 0,
            "fwd30": (c[j30] / px - 1) * 1e4, "fwd60": (c[j60] / px - 1) * 1e4,
            "ex_flat": g_flat * 100, "ex_vol": g_vol * 100,
        })
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
            if pr and 20 <= pr[-1][1] <= 100:
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
            time.sleep(0.5)
        if ok:
            store[day] = day_rows
            pickle.dump(store, open(ROWS, "wb"))
        print(f"  {day}: {len(uni)} names, {len(day_rows)} moments{'' if ok else ' (NOT SAVED)'}",
              flush=True)
    return [r for d in tdays for r in store.get(d, [])], tdays, cl


def attach_spreads(cl, rows, args):
    """SIP spread for every S7 moment plus a random 25% of the rest."""
    sp = cs.Spreads(cl)
    rng = random.Random(7)
    want = [r for r in rows if (r["vol_pace"] or 0) >= S7 or rng.random() < args.ctrl_frac]
    by_t = defaultdict(list)
    for r in want:
        by_t[r["t"]].append(r["sym"])
    n = 0
    for t, syms in sorted(by_t.items()):
        before = len(sp.d)
        sp.fetch(t, syms)
        if len(sp.d) != before:
            time.sleep(0.4)
            n += 1
            if n % 200 == 0:
                sp.save()
                print(f"  spreads: {n} fetches", flush=True)
    sp.save()
    for r in rows:
        r["spread"] = sp.get(r["t"], r["sym"])


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else float("nan")


def tstat(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 3:
        return float("nan")
    sd = statistics.pstdev(xs)
    return statistics.mean(xs) / (sd / math.sqrt(len(xs))) if sd > 0 else float("nan")


def z2(a, b, key):
    pa, pb = mean(r[key] for r in a), mean(r[key] for r in b)
    p = (pa * len(a) + pb * len(b)) / (len(a) + len(b))
    se = math.sqrt(max(p * (1 - p), 1e-9) * (1 / len(a) + 1 / len(b)))
    return (pa - pb) / se


def line(label, g):
    if not g:
        return f"  {label:<26} n=0"
    net = [r for g_ in [g] for r in g_ if r.get("spread") is not None]
    nf = mean(r["ex_flat"] - r["spread"] for r in net)
    nv = mean(r["ex_vol"] - r["spread"] for r in net)
    n60 = mean(r["fwd60"] - r["spread"] for r in net)
    return (f"  {label:<26} n={len(g):<7} run2 {mean(r['run2'] for r in g):5.1%}  "
            f"run15 {mean(r['run15'] for r in g):5.1%}  succ1 {mean(r['succ1'] for r in g):5.1%}  "
            f"runk {mean(r['runk'] for r in g):5.1%}  symk {mean(r['symk'] for r in g):5.1%}  "
            f"fwd60 {mean(r['fwd60'] for r in g):+6.1f}bp  exFlat {mean(r['ex_flat'] for r in g):+6.1f}  "
            f"exVol {mean(r['ex_vol'] for r in g):+6.1f}  | spread {mean(r['spread'] for r in net):5.1f}bp "
            f"net: fwd60 {n60:+6.1f} exFlat {nf:+6.1f} exVol {nv:+6.1f} (n={len(net)})")


def report(rows, tdays):
    rows = [r for r in rows if r["vol_pace"] is not None]
    half = set(tdays[:len(tdays) // 2])
    print(f"\nVOL PACE 60-DAY OUT-OF-PERIOD TEST  {tdays[0]}..{tdays[-1]}  ({len(tdays)} days, "
          f"{len({(r['sym'], r['day']) for r in rows})} name-days, {len(rows)} moments)")
    print("  gross in bp of price; exFlat/exVol are the exit sims; net subtracts the SIP spread\n")
    xs = sorted(r["vol_pace"] for r in rows)
    a, b = xs[len(xs) // 3], xs[2 * len(xs) // 3]
    for scope, pick in (("ALL", lambda r: True), ("H1", lambda r: r["day"] in half),
                        ("H2", lambda r: r["day"] not in half)):
        g = [r for r in rows if pick(r)]
        lo = [r for r in g if r["vol_pace"] <= a]
        mid = [r for r in g if a < r["vol_pace"] < b]
        hi = [r for r in g if r["vol_pace"] >= b]
        s7 = [r for r in g if r["vol_pace"] >= S7]
        rest = [r for r in g if r["vol_pace"] < S7]
        print(f"[{scope}]  pace terciles cut at {a:.2f} | {b:.2f}")
        print(line("all", g))
        print(line("pace low", lo))
        print(line("pace mid", mid))
        print(line("pace high", hi))
        print(line(f"S7 pace >= {S7}", s7))
        print(line("rest", rest))
        if lo and hi:
            print(f"  z high-vs-low: run2 {z2(hi, lo, 'run2'):+.1f}  runk {z2(hi, lo, 'runk'):+.1f}  "
                  f"symk {z2(hi, lo, 'symk'):+.1f}")
        print()

    print("[INSIDE vol_1m terciles] does pace add anything once volatility is known?")
    vs = sorted(r["vol_1m"] for r in rows)
    va, vb = vs[len(vs) // 3], vs[2 * len(vs) // 3]
    for lab, g in (("calm", [r for r in rows if r["vol_1m"] <= va]),
                   ("mid", [r for r in rows if va < r["vol_1m"] < vb]),
                   ("jumpy", [r for r in rows if r["vol_1m"] >= vb])):
        p = sorted(r["vol_pace"] for r in g)
        pa, pb = p[len(p) // 3], p[2 * len(p) // 3]
        lo = [r for r in g if r["vol_pace"] <= pa]
        hi = [r for r in g if r["vol_pace"] >= pb]
        print(f" vol {lab} (pace cuts {pa:.2f}|{pb:.2f}), z run2 {z2(hi, lo, 'run2'):+.1f}, "
              f"runk {z2(hi, lo, 'runk'):+.1f}")
        print(line("  pace low", lo))
        print(line("  pace high", hi))
    print()

    print("[S7 by day] net exVol per moment after spread")
    dm = defaultdict(list)
    for r in rows:
        if r["vol_pace"] >= S7 and r.get("spread") is not None:
            dm[r["day"]].append(r["ex_vol"] - r["spread"])
    ctl = defaultdict(list)
    for r in rows:
        if r["vol_pace"] < S7 and r.get("spread") is not None:
            ctl[r["day"]].append(r["ex_vol"] - r["spread"])
    beat = sum(1 for d in dm if d in ctl and mean(dm[d]) > mean(ctl[d]))
    pos = sum(1 for d in dm if mean(dm[d]) > 0)
    daily = [mean(v) for v in dm.values()]
    print(f"  S7 positive on {pos}/{len(dm)} days; beats the rest on {beat}/{len(dm)} days; "
          f"mean of daily means {mean(daily):+.1f} bp (t {tstat(daily):+.1f} across days)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=60)
    ap.add_argument("--end", default="2026-09-14", help="first day NOT included (in-sample start)")
    ap.add_argument("--ctrl-frac", type=float, default=0.25)
    ap.add_argument("--no-spreads", action="store_true")
    args = ap.parse_args()
    rows, tdays, cl = build(args)
    if not args.no_spreads:
        attach_spreads(cl, rows, args)
    else:
        for r in rows:
            r["spread"] = None
    report(rows, tdays)


if __name__ == "__main__":
    main()
