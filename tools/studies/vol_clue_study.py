#!/usr/bin/env python3
"""vol_clue_study.py — can we see volume coming on a name, and is coming
volume a clue to UP runway from the entry?

Moments every 15 min, 09:45-15:15 ET, 60 trading days before 2026-09-14,
Stage B symbols with prior close >= $2, SIP 1m bars.

Relative volume, all against the name's own normal for that time of day
(20-day average daily volume x morning_funnel.expected_fraction):
  rv_past   the last 15 minutes
  rv_next   the next 15 minutes (hindsight; only used to ask what surges do)
  build     last 5 minutes vs the 10 before (volume accelerating)
  ret15     price change over the last 15 minutes, %
Outcomes from the moment's close:
  up2 / dn2      +2% before -1% / -2% before +1%, within 60 min
  sup / sdn      steady +5% climb / fall (steady_runway_study rules: dip 1%, pull 2%)
  fwd30          close-to-close, bp; net subtracts the cached SIP spread where
                 combined_score_study has one for that minute (not all do)

Questions:
  1 persistence  rv_next by rv_past quintile, and rank correlation
  2 cost         SIP spread by rv_past quintile (cached moments only)
  3 hindsight    when rv_next surges, is the move up or down?
  4 the clue     up-minus-down for busy / building / building-while-flat,
                 by time of day, both halves and days positive

USAGE (on the mini, after the close)
  .venv/bin/python tools/studies/vol_clue_study.py [--days-back 60] [--end 2026-09-14]
"""
from __future__ import annotations

import argparse
import math
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
import combined_score_study as cs  # noqa: E402
import morning_funnel as mf  # noqa: E402
import steady_runway_study as sr  # noqa: E402

ROWS = os.path.join(ROOT, "ai_reports", "vol_clue_rows.pkl")
STEADY = (5.0, 1.0, 2.0)


def touch(B, i, px, up, dn, horizon=60):
    t, _o, h, l, _c, _v = B
    for k in range(i + 1, len(t)):
        if t[k] > t[i] + horizon * 60:
            break
        if l[k] <= px * (1 - dn / 100):
            return 0
        if h[k] >= px * (1 + up / 100):
            return 1
    return 0


def touch_dn(B, i, px, dn, up, horizon=60):
    """1 if -dn% prints before +up%."""
    t, _o, h, l, _c, _v = B
    for k in range(i + 1, len(t)):
        if t[k] > t[i] + horizon * 60:
            break
        if h[k] >= px * (1 + up / 100):
            return 0
        if l[k] <= px * (1 - dn / 100):
            return 1
    return 0


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
    out = []
    for i in rth:
        m = cs.et_min(t[i]) + 1
        if not (585 <= m <= 915) or (m - 585) % 15 or i - i0 < 15:
            continue
        j = i + 15
        if j > last or t[j] - t[i] > 20 * 60:
            continue
        mo = m - 570

        def expect(a, b):
            return avg * (mf.expected_fraction(b) - mf.expected_fraction(a))
        e_past, e_next = expect(mo - 15, mo), expect(mo, mo + 15)
        if e_past <= 0 or e_next <= 0:
            continue
        v_past = sum(v[i - 14:i + 1])
        v5, v10 = sum(v[i - 4:i + 1]), sum(v[i - 14:i - 4])
        px = c[i]
        j30 = i + 30
        up = sr.steady(B, i, px, last)
        dn = sr.steady(B, i, px, last, up=False)
        out.append({
            "sym": sym, "day": day, "t": t[i] + 60, "mins": mo, "price": px,
            "rv_past": v_past / e_past, "rv_next": sum(v[i + 1:j + 1]) / e_next,
            "build": (v5 / 5) / (v10 / 10) if v10 > 0 else None,
            "ret15": (px / c[i - 15] - 1) * 100,
            "day_chg": (px / prev_close - 1) * 100,
            "ret_next15": (c[j] / px - 1) * 100,
            "up2": touch(B, i, px, 2.0, 1.0), "dn2": touch_dn(B, i, px, 2.0, 1.0),
            "sup": up[STEADY] is not None, "sdn": dn[STEADY] is not None,
            "fwd30": (c[j30] / px - 1) * 1e4 if j30 <= last and t[j30] - t[i] <= 40 * 60 else None,
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
            time.sleep(0.5)
        if ok:
            store[day] = day_rows
            pickle.dump(store, open(ROWS, "wb"))
        print(f"  {day}: {len(uni)} names, {len(day_rows)} moments{'' if ok else ' (NOT SAVED)'}",
              flush=True)
    return [r for d in tdays for r in store.get(d, [])], tdays


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else float("nan")


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda k: xs[k])
    r = [0.0] * len(xs)
    for pos, k in enumerate(order):
        r[k] = pos
    return r


def spearman(a, b):
    ra, rb = ranks(a), ranks(b)
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else float("nan")


def quintiles(rows, key):
    xs = sorted(r[key] for r in rows if r.get(key) is not None)
    cuts = [xs[int(len(xs) * q)] for q in (0.2, 0.4, 0.6, 0.8)]

    def qi(x):
        return sum(x > c for c in cuts)
    out = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            out[qi(r[key])].append(r)
    return out, cuts


def spk(r):
    return f"{r['sym']}|{int(r['t'])}"


def outcome(g, sp=None):
    base = (f"n={len(g):<7} up2 {mean(r['up2'] for r in g):5.1%} dn2 {mean(r['dn2'] for r in g):5.1%} "
            f"edge {mean(r['up2'] - r['dn2'] for r in g):+5.1%} | steady5 up {mean(r['sup'] for r in g):5.2%} "
            f"dn {mean(r['sdn'] for r in g):5.2%} | fwd30 {mean(r['fwd30'] for r in g):+6.1f}bp")
    if sp is not None:
        wn = [r for r in g if sp.get(spk(r)) is not None and r["fwd30"] is not None]
        if wn:
            s_ = mean(sp[spk(r)] for r in wn)
            n_ = mean(r["fwd30"] - sp[spk(r)] for r in wn)
            base += f"  spread {s_:5.1f}bp net {n_:+6.1f}bp (n={len(wn)})"
    return base


def clue(label, rows, pick, half):
    g = [r for r in rows if pick(r)]
    rest = [r for r in rows if not pick(r)]
    if len(g) < 300:
        print(f"  {label:<40} n={len(g)} (too few)")
        return
    e = lambda G: mean(r["up2"] - r["dn2"] for r in G)  # noqa: E731
    es = lambda G: mean(float(r["sup"]) - float(r["sdn"]) for r in G)  # noqa: E731
    h = [(e([r for r in g if (r["day"] in half) == hs]) - e([r for r in rest if (r["day"] in half) == hs]))
         for hs in (True, False)]
    dg, dr = defaultdict(list), defaultdict(list)
    for r in g:
        dg[r["day"]].append(r)
    for r in rest:
        dr[r["day"]].append(r)
    days = [d for d in dg if len(dg[d]) >= 20]
    wins = sum(e(dg[d]) > e(dr[d]) for d in days)
    ok = e(g) > e(rest) and h[0] > 0 and h[1] > 0 and days and wins / len(days) > 0.5
    print(f"  {label:<40} n={len(g):<7} up2 {mean(r['up2'] for r in g):5.1%} dn2 {mean(r['dn2'] for r in g):5.1%} "
          f"edge {e(g):+5.1%} (rest {e(rest):+5.1%}) steady5 edge {es(g):+5.2%} (rest {es(rest):+5.2%}) "
          f"fwd30 {mean(r['fwd30'] for r in g):+5.1f}bp | H1 {h[0]:+5.1%} H2 {h[1]:+5.1%} days {wins}/{len(days)}"
          f"{'  HOLDS' if ok else ''}")


def report(rows, tdays):
    import json
    try:
        sp = json.load(open(cs.SPREADS))
    except (OSError, ValueError):
        sp = {}
    half = set(tdays[:len(tdays) // 2])
    print(f"\nVOLUME CLUE STUDY {tdays[0]}..{tdays[-1]} ({len(tdays)} days, "
          f"{len({(r['sym'], r['day']) for r in rows})} name-days, {len(rows)} moments)")

    print("\n1  PERSISTENCE: does the last 15 min of relative volume predict the next 15?")
    rho = spearman([r["rv_past"] for r in rows], [r["rv_next"] for r in rows])
    print(f"  rank correlation rv_past vs rv_next: {rho:+.2f}")
    qs, cuts = quintiles(rows, "rv_past")
    print(f"  rv_past quintile cuts {' | '.join(f'{c:.2f}' for c in cuts)}")
    for k in sorted(qs):
        g = qs[k]
        nx = sorted(r["rv_next"] for r in g)
        print(f"    Q{k + 1}  rv_past med {statistics.median(r['rv_past'] for r in g):5.2f}  ->  rv_next med "
              f"{nx[len(nx) // 2]:5.2f}  p75 {nx[3 * len(nx) // 4]:5.2f}  next >= 2x normal "
              f"{sum(x >= 2 for x in nx) / len(nx):5.1%}")

    print("\n2  COST: SIP spread by rv_past quintile (moments with a cached spread)")
    for k in sorted(qs):
        g = [r for r in qs[k] if sp.get(spk(r)) is not None]
        if g:
            v = [sp[spk(r)] for r in g]
            print(f"    Q{k + 1}  spread median {statistics.median(v):5.1f}bp  mean {statistics.mean(v):5.1f}bp  (n={len(g)})")
    for lab, lo, hi in (("$2-10", 2, 10), ("$10-20", 10, 20), ("$20-50", 20, 50), ("$50+", 50, 1e9)):
        g = [r for r in rows if lo <= r["price"] < hi and sp.get(spk(r)) is not None]
        if len(g) < 100:
            continue
        busy = [sp[spk(r)] for r in g if r["rv_past"] >= cuts[3]]
        calm = [sp[spk(r)] for r in g if r["rv_past"] <= cuts[0]]
        if busy and calm:
            print(f"    {lab:<7} quietest fifth {statistics.median(calm):5.1f}bp  vs busiest fifth "
                  f"{statistics.median(busy):5.1f}bp")

    print("\n3  HINDSIGHT: when volume DOES surge in the next 15 min, which way does price go?")
    qn, cn = quintiles(rows, "rv_next")
    for k in sorted(qn):
        g = qn[k]
        rn = [r["ret_next15"] for r in g]
        print(f"    rv_next Q{k + 1} (>{cn[k - 1]:.2f})" if k else f"    rv_next Q1 (<={cn[0]:.2f})", end="")
        print(f"  next-15 move: up>0.5% {sum(x > 0.5 for x in rn) / len(rn):5.1%}  "
              f"down<-0.5% {sum(x < -0.5 for x in rn) / len(rn):5.1%}  mean {statistics.mean(rn):+.2f}%")

    print("\n4  THE CLUE: known now, does it lean the runway UP? (edge = up2 - dn2; HOLDS = beats the rest "
          "in both halves and on most days)")
    top = cuts[3]
    tests = [
        ("busy (rv_past top fifth)", lambda r: r["rv_past"] >= top),
        ("quiet (rv_past bottom fifth)", lambda r: r["rv_past"] <= cuts[0]),
        ("building (last 5m >= 2x prior 10m)", lambda r: (r["build"] or 0) >= 2),
        ("busy + building", lambda r: r["rv_past"] >= top and (r["build"] or 0) >= 2),
        ("busy + flat price (|ret15| < 0.3%)", lambda r: r["rv_past"] >= top and abs(r["ret15"]) < 0.3),
        ("building + flat price", lambda r: (r["build"] or 0) >= 2 and abs(r["ret15"]) < 0.3),
        ("busy + rising price (ret15 > +0.5%)", lambda r: r["rv_past"] >= top and r["ret15"] > 0.5),
        ("busy + falling price (ret15 < -0.5%)", lambda r: r["rv_past"] >= top and r["ret15"] < -0.5),
        ("busy + up on day > 2.35%", lambda r: r["rv_past"] >= top and r["day_chg"] > 2.35),
        ("busy + up on day + first 76 min", lambda r: r["rv_past"] >= top and r["day_chg"] > 2.35
         and r["mins"] <= 146),
    ]
    for tod, lo, hi in (("ALL DAY", 0, 999), ("OPEN 09:45-10:45", 15, 75),
                        ("MIDDAY 10:45-14:00", 75, 270), ("LATE 14:00-15:15", 270, 999)):
        g = [r for r in rows if lo <= r["mins"] < hi]
        print(f"\n  [{tod}]  all: {outcome(g, sp)}")
        for label, pick in tests:
            clue(label, g, pick, half)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=60)
    ap.add_argument("--end", default="2026-09-14")
    args = ap.parse_args()
    rows, tdays = build(args)
    report(rows, tdays)


if __name__ == "__main__":
    main()
