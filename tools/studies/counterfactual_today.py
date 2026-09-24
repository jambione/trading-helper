#!/usr/bin/env python3
"""Counterfactual for one session: actual vs the 2026-09-24 configuration.

A  ACTUAL          outcomes.jsonl for the day
B  OLD IN / NEW OUT actual fills >= $20, exits replaced (arm +0.3%, 0.35% from
                    peak, 1% seed stop, no 60s rule, flat 15:50)
C  SQUARE / NEW OUT the old square arm on the day's admitted names >= $20
D  ONE ARM / NEW OUT fast %R -50 cross (slow rising) on the same names

C and D run the live book constraints: events only after the name was
admitted, 09:40-15:30, max 5 open, one per symbol, 120s re-entry cooldown,
daily loss brake at 3R (1R = 1% of equity), ~$460 per trade, flat at 15:50.
Exits walk SIP 1m bars (conservative order: low tested before the high raises
the stop). Costs per round trip: 0.11% (measured $20+ entry+exit) and 0.20%.
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import entry_screen as es  # noqa: E402

DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-09-23"
EQUITY = 2326.0
NOTIONAL = EQUITY / 5
BRAKE_USD = 3 * 0.01 * EQUITY
MIN_PX = 20.0
EOD_MIN = 15 * 60 + 50


def walk_eod(B, i0, px, arm=0.3, give=0.35, seed=1.0):
    """Exit return % and exit index; trail from peak after arming; flat 15:50."""
    t, o, h, l, c, _v = B
    stop = px * (1 - seed / 100)
    hi, armed = px, False
    last = i0
    for k in range(i0 + 1, len(t)):
        if bars.et_minutes(t[k]) >= EOD_MIN:
            return (o[k] / px - 1) * 100, k
        last = k
        if l[k] <= stop:
            return (min(stop, o[k]) / px - 1) * 100, k
        hi = max(hi, h[k])
        if not armed and hi >= px * (1 + arm / 100):
            armed = True
        if armed:
            stop = max(stop, hi * (1 - give / 100))
    return (c[last] / px - 1) * 100, last


def book_sim(events, cache, cost):
    """events: [(ts, sym, i)] sorted; cost = float or {(sym, i): cost%}."""
    open_until: dict[str, float] = {}
    cool_until: dict[str, float] = {}
    trades, realized = [], 0.0
    for ts, sym, i in events:
        if realized <= -BRAKE_USD:
            break
        if open_until.get(sym, 0) > ts or cool_until.get(sym, 0) > ts:
            continue
        if sum(1 for v in open_until.values() if v > ts) >= 5:
            continue
        B = cache[sym]
        px = B[4][i]
        ret, j = walk_eod(B, i, px)
        c = cost.get((sym, i), 0.11) if isinstance(cost, dict) else cost
        net = ret - c
        usd = NOTIONAL * net / 100
        realized += usd
        open_until[sym] = B[0][j]
        cool_until[sym] = B[0][j] + 120
        trades.append({"sym": sym, "ts": ts, "px": px, "ret": ret, "net": net, "cost": c,
                       "usd": usd, "hold_min": (B[0][j] - B[0][i]) / 60})
    return trades


def summarize(label, trades):
    if not trades:
        print(f"  {label:<44} no trades")
        return
    n = len(trades)
    usd = sum(t["usd"] for t in trades)
    win = sum(t["net"] > 0 for t in trades) / n
    hrs = sorted({int(bars.et_minutes(t["ts"]) // 60) for t in trades})
    med_hold = sorted(t["hold_min"] for t in trades)[n // 2]
    print(f"  {label:<44}{n:>4} trades  {win:>4.0%} win  "
          f"{sum(t['net'] for t in trades) / n:>+7.3f}%/trade  ${usd:>+8.2f}  "
          f"hours {hrs[0]}-{hrs[-1]}  med hold {med_hold:.0f}m")


def main():
    from config import load_config
    cfg = load_config()
    first: dict = {}
    want = es.admitted([DAY], first)
    want[DAY].add("SPY")
    ext = es.fetch_ext(want)
    spy = ext.get(("SPY", DAY))
    cache, ev = {}, {"square": [], "mid_rise": []}
    for s in want[DAY]:
        if s == "SPY":
            continue
        df = ext.get((s, DAY))
        if df is None or len(df) < 150:
            continue
        ind = es.indicators(df, cfg, spy)
        B = es.to_B(df)
        cache[s] = B
        t_on = first.get((s, DAY))
        for i in range(2, len(B[0]) - 1):
            m = ind["mins"][i]
            if m < 9 * 60 + 40 or m > 15 * 60 + 30 or B[4][i] < MIN_PX:
                continue
            if t_on is None or ind["ts"][i] < t_on:
                continue
            f = es.fire(ind, i)
            for k in ("square", "mid_rise"):
                if k in f:
                    ev[k].append((ind["ts"][i], s, i))

    # A — actual
    rows = [json.loads(l) for l in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl"))
            if l.strip()]
    act = [r for r in rows if isinstance(r.get("entry_time"), (int, float))
           and bars.day_of(r["entry_time"]) == DAY]
    usd = sum(r.get("realized_pl_usd") or 0 for r in act)
    wins = sum((r.get("realized_pl_usd") or 0) > 0 for r in act)
    print(f"COUNTERFACTUAL {DAY}   admitted names: {len(cache)} at any price, "
          f"events >= ${MIN_PX:.0f}: square {len(ev['square'])}, one-arm {len(ev['mid_rise'])}\n")
    print(f"  {'A  ACTUAL (as traded)':<44}{len(act):>4} trades  {wins / max(1, len(act)):>4.0%} win"
          f"  {'':>15}${usd:>+8.2f}")

    for cost in (0.11, 0.20):
        print(f"\n  -- cost {cost:.2f}% per round trip --")
        # B — actual fills >= $20 with new exits
        bt = []
        for r in act:
            if float(r.get("entry_price") or 0) < MIN_PX:
                continue
            B = cache.get(r["symbol"]) or (es.to_B(ext[(r["symbol"], DAY)])
                                          if ext.get((r["symbol"], DAY)) is not None else None)
            if not B:
                continue
            i = bars.index_at(B[0], r["entry_time"])
            if i < 1:
                continue
            ret, j = walk_eod(B, i, float(r["entry_price"]))
            bt.append({"sym": r["symbol"], "ts": r["entry_time"], "ret": ret, "net": ret - cost,
                       "usd": NOTIONAL * (ret - cost) / 100, "hold_min": (B[0][j] - B[0][i]) / 60})
        summarize("B  actual entries >=$20, new exits", bt)
        summarize("C  square arm, new exits, book rules", book_sim(sorted(ev["square"]), cache, cost))
        d = book_sim(sorted(ev["mid_rise"]), cache, cost)
        summarize("D  ONE ARM, new exits, book rules", d)
        if cost == 0.11 and d:
            print("\n     D trades:", ", ".join(
                f"{t['sym']} {bars.et_minutes(t['ts']) // 60}:{bars.et_minutes(t['ts']) % 60:02d} {t['net']:+.2f}%"
                for t in d[:40]))


def gated(day=DAY):
    """Tomorrow's full setup on the day: one arm + $20 + new exits, each trade
    paying its TRUE SIP spread at entry, with the spread gate (16-min-old SIP
    spread <= 0.20%) and the gap-down block (official open < -1%) applied."""
    from datetime import datetime, timedelta, timezone
    from config import load_config
    import exec_report as xr
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    cfg = load_config()
    first: dict = {}
    want = es.admitted([day], first)
    want[day].add("SPY")
    ext = es.fetch_ext(want)
    spy = ext.get(("SPY", day))
    cl = bars.client()
    syms = sorted(s for s in want[day] if s != "SPY")
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET)
    db = cl.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=syms, timeframe=TimeFrame.Day,
        start=(d0 - timedelta(days=7)).astimezone(timezone.utc),
        end=(d0 - timedelta(minutes=1)).astimezone(timezone.utc), feed=DataFeed.SIP)).data
    prev_close = {}
    for s in syms:
        prior = [b for b in (db.get(s) or []) if b.timestamp.astimezone(bars.ET).date() < d0.date()]
        if prior:
            prev_close[s] = float(prior[-1].close)
    cache, events, gap = {}, [], {}
    for s in syms:
        df = ext.get((s, day))
        if df is None or len(df) < 150:
            continue
        ind = es.indicators(df, cfg, spy)
        B = es.to_B(df)
        cache[s] = B
        f0 = ind["first"]
        if s in prev_close and f0 < len(B[0]):
            gap[s] = (B[1][f0] / prev_close[s] - 1) * 100
        t_on = first.get((s, day))
        for i in range(2, len(B[0]) - 1):
            m = ind["mins"][i]
            if m < 9 * 60 + 40 or m > 15 * 60 + 30 or B[4][i] < MIN_PX:
                continue
            if t_on is None or ind["ts"][i] < t_on:
                continue
            if "mid_rise" in es.fire(ind, i):
                events.append((ind["ts"][i], s, i))
    events.sort()
    spread_now, spread_seen = {}, {}
    sp = lambda q: (q[1] - q[0]) / ((q[1] + q[0]) / 2) * 100
    for ts, s, i in events:
        t_close = datetime.fromtimestamp(cache[s][0][i] + 59, timezone.utc)
        qn = xr.nbbo_at(cl, s, t_close)
        time.sleep(0.2)
        qs = xr.nbbo_at(cl, s, t_close - timedelta(minutes=16))
        time.sleep(0.2)
        if qn:
            spread_now[(s, i)] = sp(qn)
        if qs:
            spread_seen[(s, i)] = sp(qs)
    cost = {k: v for k, v in spread_now.items()}          # true round trip ~= spread
    spread_ok = lambda e: spread_seen.get((e[1], e[2])) is not None and spread_seen[(e[1], e[2])] <= 0.20
    gap_ok = lambda e: gap.get(e[1]) is not None and gap[e[1]] >= -1.0
    print(f"\n== FULL SETUP, TRUE SPREADS ({day}) ==  one-arm events >= $20: {len(events)}")
    blocked_gap = sorted({s for s in cache if s in gap and gap[s] < -1.0})
    wide = sorted({e[1] for e in events if spread_seen.get((e[1], e[2]), 0) > 0.20})
    print(f"  gap-down >1% names: {blocked_gap}")
    print(f"  names ever over the 0.20% spread gate: {wide}\n")
    runs = [("D  one arm + new exits (no gates)", events),
            ("E  D + gap-down block", [e for e in events if gap_ok(e)]),
            ("F  D + spread gate", [e for e in events if spread_ok(e)]),
            ("G  D + BOTH gates  <- tomorrow", [e for e in events if gap_ok(e) and spread_ok(e)])]
    for label, ev in runs:
        tr = book_sim(ev, cache, cost)
        summarize(label, tr)
        if tr:
            print(f"     avg spread paid {sum(t['cost'] for t in tr) / len(tr):.3f}%   "
                  f"gross {sum(t['ret'] for t in tr) / len(tr):+.3f}%/trade")
    tr = book_sim([e for e in events if gap_ok(e) and spread_ok(e)], cache, cost)
    print("\n  G trades:", ", ".join(
        f"{t['sym']} {bars.et_minutes(t['ts']) // 60}:{bars.et_minutes(t['ts']) % 60:02d} "
        f"{t['net']:+.2f}%" for t in tr))


if __name__ == "__main__":
    main()
    gated()
