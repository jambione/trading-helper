#!/usr/bin/env python3
"""rsi_buy_low_study.py — does RSI help us buy low into runway?

60 trading days before 2026-09-14, Stage B symbols with prior close >= $2,
SIP 1m RTH bars. RSI is Wilder's RSI(14) on 1m closes and on 5m closes
(5m bars built from the 1m bars; a 5m reading is known only when its bar
closes).

Events, each at the minute it becomes known (from 09:50, until 15:15):
  os1      1m RSI < 30                      (buy while oversold)
  x30_1    1m RSI crosses up through 30      (buy the bounce)
  x30_5    5m RSI crosses up through 30      (slower bounce)
  x50_1    1m RSI crosses up through 50      (momentum, for contrast)
  mgap     1m MACD(12,26,9) crosses above its signal with the gap (MACD -
           signal) >= 0.02% of price, the removed ai_watch_macd_gap_arm's
           ai_watch_macd_gap_min_pct
  mgap_rsi mgap with 1m RSI <= 60 (that arm's ai_watch_macd_gap_rsi_max)
Control: every 5th minute of the same names on the same days, same hours.

Outcomes from that minute's close:
  up2 / dn2   +2% before -1% / -2% before +1% within 60 min
  sup / sdn   steady +5% climb / fall (steady_runway_study: dip 1%, pull 2%)
  fwd15, fwd30  close-to-close, bp gross
Spread is not netted per event (quotes at every event minute would be tens of
thousands of requests). Events and control are the same names on the same
days, so their gross difference is what RSI adds. The cached spread medians
by price band are printed for scale.

A result counts if the event beats the control on up2-dn2 and fwd30 in BOTH
halves and on more than half the days.

USAGE (on the mini, off hours)
  .venv/bin/python tools/studies/rsi_buy_low_study.py [--days-back 60] [--end 2026-09-14]
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
import combined_score_study as cs  # noqa: E402
import steady_runway_study as sr  # noqa: E402
import vol_clue_study as vc  # noqa: E402

ROWS = os.path.join(ROOT, "ai_reports", "rsi_buy_low_rows.pkl")
STEADY = (5.0, 1.0, 2.0)
EVENTS = ("os1", "x30_1", "x30_5", "x50_1", "mgap", "mgap_rsi")


def wilder_rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for k in range(1, n + 1):
        d = closes[k] - closes[k - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for k in range(n + 1, len(closes)):
        d = closes[k] - closes[k - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[k] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def ema(xs, n):
    out, a, e = [], 2 / (n + 1), None
    for x in xs:
        e = x if e is None else a * x + (1 - a) * e
        out.append(e)
    return out


def rows_for(sym, day, B, prev_close):
    t, _o, _h, _l, c, _v = B
    rth = [k for k in range(len(t)) if 570 <= cs.et_min(t[k]) < 960]
    if len(rth) < 300 or not prev_close:
        return []
    i0, last = rth[0], rth[-1]
    idx = rth
    cl = [c[k] for k in idx]
    r1 = wilder_rsi(cl)
    macd = [a - b for a, b in zip(ema(cl, 12), ema(cl, 26))]
    sig = ema(macd, 9)
    # 5m bars: close of each 5-minute block, known at the block's last minute
    blocks = defaultdict(list)
    for pos, k in enumerate(idx):
        blocks[(cs.et_min(t[k]) - 570) // 5].append(pos)
    bkeys = sorted(blocks)
    c5 = [cl[blocks[b][-1]] for b in bkeys]
    r5 = wilder_rsi(c5)
    r5_at = {}                           # position -> (prev 5m rsi, this 5m rsi) at block close
    for j, b in enumerate(bkeys):
        if j >= 1 and r5[j] is not None and r5[j - 1] is not None:
            r5_at[blocks[b][-1]] = (r5[j - 1], r5[j])
    out = []
    for pos, k in enumerate(idx):
        m = cs.et_min(t[k]) + 1
        if m < 590 or m > 915 or pos < 20 or r1[pos] is None or r1[pos - 1] is None:
            continue
        ev = []
        if r1[pos] < 30:
            ev.append("os1")
        if r1[pos - 1] < 30 <= r1[pos]:
            ev.append("x30_1")
        if r1[pos - 1] < 50 <= r1[pos]:
            ev.append("x50_1")
        if pos in r5_at and r5_at[pos][0] < 30 <= r5_at[pos][1]:
            ev.append("x30_5")
        if pos >= 35 and macd[pos - 1] <= sig[pos - 1] and macd[pos] > sig[pos] \
                and (macd[pos] - sig[pos]) / cl[pos] * 100 >= 0.02:
            ev.append("mgap")
            if r1[pos] <= 60:
                ev.append("mgap_rsi")
        ctrl = (m - 590) % 5 == 0
        if not ev and not ctrl:
            continue
        px = c[k]
        j30, j15 = k + 30, k + 15
        up = sr.steady(B, k, px, last)
        dn = sr.steady(B, k, px, last, up=False)
        out.append({
            "sym": sym, "day": day, "mins": m - 570, "price": px, "ev": ev, "ctrl": ctrl,
            "day_chg": (px / prev_close - 1) * 100,
            "up2": vc.touch(B, k, px, 2.0, 1.0), "dn2": vc.touch_dn(B, k, px, 2.0, 1.0),
            "sup": up[STEADY] is not None, "sdn": dn[STEADY] is not None,
            "fwd15": (c[j15] / px - 1) * 1e4 if j15 <= last and t[j15] - t[k] <= 20 * 60 else None,
            "fwd30": (c[j30] / px - 1) * 1e4 if j30 <= last and t[j30] - t[k] <= 40 * 60 else None,
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
                    day_rows += rows_for(sym, day, B, prevd[sym])
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"  {day} bars: {e}"[:160], flush=True)
            time.sleep(args.pace)
        if ok:
            store[day] = day_rows
            pickle.dump(store, open(ROWS, "wb"))
        print(f"  {day}: {len(uni)} names, {len(day_rows)} rows{'' if ok else ' (NOT SAVED)'}", flush=True)
    return [r for d in tdays for r in store.get(d, [])], tdays


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else float("nan")


def stats(g):
    return (mean(r["up2"] - r["dn2"] for r in g), mean(float(r["sup"]) - float(r["sdn"]) for r in g),
            mean(r["fwd15"] for r in g), mean(r["fwd30"] for r in g))


def compare(label, rows, pick, half):
    """Each event vs control inside the rows that pass *pick*."""
    base = [r for r in rows if r["ctrl"] and pick(r)]
    if len(base) < 500:
        print(f"\n  [{label}] control n={len(base)} (too few)")
        return
    e0, s0, f15, f30 = stats(base)
    print(f"\n  [{label}]  CONTROL n={len(base):<7} up2 {mean(r['up2'] for r in base):5.1%} "
          f"dn2 {mean(r['dn2'] for r in base):5.1%} edge {e0:+5.1%} | steady5 edge {s0:+5.2%} | "
          f"fwd15 {f15:+5.1f} fwd30 {f30:+5.1f}bp")
    bday = defaultdict(list)
    for r in base:
        bday[r["day"]].append(r)
    for ev in EVENTS:
        g = [r for r in rows if ev in r["ev"] and pick(r)]
        if len(g) < 200:
            print(f"    {ev:<7} n={len(g)} (too few)")
            continue
        e, s, a15, a30 = stats(g)
        h = []
        for hs in (True, False):
            gh = [r for r in g if (r["day"] in half) == hs]
            bh = [r for r in base if (r["day"] in half) == hs]
            h.append((stats(gh)[0] - stats(bh)[0], stats(gh)[3] - stats(bh)[3]))
        eday = defaultdict(list)
        for r in g:
            eday[r["day"]].append(r)
        days = [d for d in eday if len(eday[d]) >= 10 and d in bday]
        wins = sum(stats(eday[d])[3] > stats(bday[d])[3] for d in days)
        ok = (e > e0 and a30 > f30 and all(x[0] > 0 and x[1] > 0 for x in h)
              and days and wins / len(days) > 0.5)
        print(f"    {ev:<7} n={len(g):<7} up2 {mean(r['up2'] for r in g):5.1%} dn2 {mean(r['dn2'] for r in g):5.1%} "
              f"edge {e:+5.1%} ({e - e0:+5.1%} vs ctrl) | steady5 {s:+5.2%} | fwd15 {a15:+6.1f} "
              f"fwd30 {a30:+6.1f}bp ({a30 - f30:+5.1f} vs ctrl) | H1 {h[0][1]:+5.1f} H2 {h[1][1]:+5.1f}bp "
              f"days {wins}/{len(days)}{'  HOLDS' if ok else ''}")


def report(rows, tdays):
    half = set(tdays[:len(tdays) // 2])
    print(f"\nRSI BUY-LOW STUDY {tdays[0]}..{tdays[-1]} ({len(tdays)} days, "
          f"{len({(r['sym'], r['day']) for r in rows})} name-days)")
    for ev in EVENTS:
        print(f"  {ev}: {sum(ev in r['ev'] for r in rows)} events")
    try:
        sp = json.load(open(cs.SPREADS))
        v = [x for x in sp.values() if x is not None]
        print(f"  for scale, cached SIP spread median {statistics.median(v):.1f}bp, mean {statistics.mean(v):.1f}bp")
    except (OSError, ValueError):
        pass
    tests = [
        ("ALL DAY", lambda r: True),
        ("OPEN 09:50-10:45", lambda r: r["mins"] < 75),
        ("MIDDAY 10:45-14:00", lambda r: 75 <= r["mins"] < 270),
        ("LATE 14:00-15:15", lambda r: r["mins"] >= 270),
        ("UP >2.35% ON THE DAY, 10:45-11:56", lambda r: r["day_chg"] > 2.35 and 75 <= r["mins"] <= 146),
        ("DOWN on the day (< -0.62%)", lambda r: r["day_chg"] < -0.62),
        ("$2-10", lambda r: r["price"] < 10),
        ("$10-50", lambda r: 10 <= r["price"] < 50),
        ("$50+", lambda r: r["price"] >= 50),
    ]
    print("\n  edge = up2 - dn2; each event is compared with CONTROL minutes in the same slice. "
          "HOLDS = beats control on runway and fwd30 in both halves and on most days")
    for label, pick in tests:
        compare(label, rows, pick, half)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=60)
    ap.add_argument("--end", default="2026-09-14")
    ap.add_argument("--pace", type=float, default=1.0, help="seconds between bar requests")
    args = ap.parse_args()
    rows, tdays = build(args)
    report(rows, tdays)


if __name__ == "__main__":
    main()
