#!/usr/bin/env python3
"""Does a sector filter survive the book? (the test that rejected 50ma+news)

Same population, gates and book rules as name_rank_study.py (spread gate
0.20% on the name-day SIP spread, gap-down block 1%, 5 slots, one per
symbol, 120s cooldown, 3R brake, flat 15:50, tomorrow's exits, cost = the
name-day's median SIP spread). Sector strength at each event = the name's
sector ETF move since the open minus SPY's (sector_study.py mapping).

  S1  one arm, all names                 (as deployed)
  S5  one arm, sector leading SPY > +0.5%
  S6  one arm, sector not lagging (>= -0.5%)
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import counterfactual_today as cf  # noqa: E402
import daily_filter_screen as dfs  # noqa: E402
import entry_screen as es  # noqa: E402
import name_rank_study as nr  # noqa: E402
import sector_study as ss  # noqa: E402

DAYS = ss.DAYS
H1 = set(DAYS[:4])


def main():
    from config import load_config
    cfg = load_config()
    first: dict = {}
    want = es.admitted(DAYS, first)
    syms_all = sorted({s for v in want.values() for s in v})
    ind = ss.industries(syms_all)
    for d in DAYS:
        want[d] |= set(ss.ETFS) | {"SPY"}
    ext = es.fetch_ext(want)
    dfeat = dfs.daily_features(set(syms_all))
    cl = bars.client()
    per_day = {}
    for d in DAYS:
        spy = ext.get(("SPY", d))
        if spy is None:
            continue
        SB = es.to_B(spy)
        eB = {e: es.to_B(ext[(e, d)]) for e in ss.ETFS if ext.get((e, d)) is not None}
        cache, cost = {}, {}
        ev = {"S1": [], "S5": [], "S6": []}
        for s in sorted(want[d]):
            if s in ss.ETFS or s == "SPY":
                continue
            df = ext.get((s, d))
            f = dfeat.get((s, d)) or {}
            if df is None or len(df) < 150 or f.get("gap") is None or f["gap"] < -1.0:
                continue
            sp = nr.name_day_spread(cl, s, d)
            time.sleep(0.2)
            if sp is None or sp > 0.20:
                continue
            etf = ss.etf_for(ind.get(s))
            E = eB.get(etf)
            indi = es.indicators(df, cfg, spy)
            B = es.to_B(df)
            cache[s] = B
            t_on = first.get((s, d))
            for i in range(2, len(B[0]) - 1):
                m = indi["mins"][i]
                if m < 9 * 60 + 40 or m > 15 * 60 + 30 or B[4][i] < 20:
                    continue
                if t_on is None or indi["ts"][i] < t_on:
                    continue
                if "mid_rise" not in es.fire(indi, i):
                    continue
                e = (indi["ts"][i], s, i)
                cost[(s, i)] = sp
                ev["S1"].append(e)
                if E is None:
                    continue
                ke, ks = bars.index_at(E[0], B[0][i]), bars.index_at(SB[0], B[0][i])
                me, msp = ss.move_since_open(E, ke), ss.move_since_open(SB, ks)
                if me is None or msp is None:
                    continue
                rs_ = me - msp
                if rs_ > 0.5:
                    ev["S5"].append(e)
                if rs_ >= -0.5:
                    ev["S6"].append(e)
        per_day[d] = (cache, {k: sorted(v) for k, v in ev.items()}, cost)
        print(f"  {d}: {len(cache)} names", file=sys.stderr)

    labels = {"S1": "S1 one arm, all names (deployed)",
              "S5": "S5 one arm, sector leading > +0.5%",
              "S6": "S6 one arm, sector not lagging"}
    print(f"SECTOR BOOK STUDY {DAYS[0]}..{DAYS[-1]}  (gates on, true name-day spreads, "
          f"book rules; halves {DAYS[0]}..{DAYS[3]} | {DAYS[4]}..{DAYS[-1]})\n")
    print(f"  {'strategy':<36}{'trades':>7}{'win':>6}{'gross':>9}{'cost':>8}{'net':>9}"
          f"{'±':>7}{'P/L':>10}{'h1 net':>9}{'h2 net':>9}  per-day P/L")
    for k in ("S1", "S5", "S6"):
        trades, daypl = [], []
        for d in DAYS:
            if d not in per_day:
                continue
            cache, ev, cost = per_day[d]
            tr = cf.book_sim(ev[k], cache, cost)
            for t in tr:
                t["day"] = d
            trades += tr
            daypl.append(sum(t["usd"] for t in tr))
        if not trades:
            print(f"  {labels[k]:<36} no trades")
            continue
        net = [t["net"] for t in trades]
        a = [t["net"] for t in trades if t["day"] in H1]
        b = [t["net"] for t in trades if t["day"] not in H1]
        print(f"  {labels[k]:<36}{len(trades):>7}{sum(x > 0 for x in net) / len(net):>6.0%}"
              f"{statistics.mean(t['ret'] for t in trades):>+8.3f}%"
              f"{statistics.mean(t['cost'] for t in trades):>7.3f}%{statistics.mean(net):>+8.3f}%"
              f"{statistics.stdev(net) / len(net) ** .5:>7.3f}{sum(daypl):>+10.2f}"
              f"{(statistics.mean(a) if a else float('nan')):>+9.3f}"
              f"{(statistics.mean(b) if b else float('nan')):>+9.3f}  "
              + " ".join(f"{x:+.0f}" for x in daypl))


if __name__ == "__main__":
    main()
