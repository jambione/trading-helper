#!/usr/bin/env python3
"""Is the name doing the work? Trigger vs name selection, under tomorrow's rules.

Population: book-admitted names >= $20, 2026-09-14..23, events after admission,
spread gate (name-day SIP spread <= 0.20%) and gap-down block (> 1%) applied.
Book rules per day (tools/studies/counterfactual_today.book_sim): new exits
(1% seed, arm +0.3%, 0.35% from peak), 5 slots, one per symbol, 120s
cooldown, 3R brake, flat 15:50. Cost = the name-day's median SIP spread.

Strategies
  S1  one arm, all names                       (tomorrow's setup)
  S2  one arm, only names above 50-day MA with news since the prior close
  S3  first eligible minute after admission, once per name-day, all names
  S4  S3 restricted to above-50ma + news names
"news" is judged at each event's own time (known live).
"""
from __future__ import annotations

import collections
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import counterfactual_today as cf  # noqa: E402
import daily_filter_screen as dfs  # noqa: E402
import entry_screen as es  # noqa: E402

DAYS = dfs.DAYS
H1 = set(DAYS[:4])


def name_day_spread(cl, sym: str, day: str) -> float | None:
    """Median SIP spread over 10:30-10:31 ET that day, %."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    t0 = datetime.strptime(day, "%Y-%m-%d").replace(hour=10, minute=30, tzinfo=bars.ET)
    try:
        q = cl.get_stock_quotes(StockQuotesRequest(
            symbol_or_symbols=sym, start=t0.astimezone(timezone.utc),
            end=(t0 + timedelta(minutes=1)).astimezone(timezone.utc),
            feed=DataFeed.SIP, limit=5000))
    except Exception:  # noqa: BLE001
        return None
    sp = sorted((r.ask_price - r.bid_price) / ((r.ask_price + r.bid_price) / 2) * 100
                for r in (q.data.get(sym) or [])
                if r.bid_price and r.ask_price and r.ask_price >= r.bid_price)
    return sp[len(sp) // 2] if sp else None


def main():
    from config import load_config
    cfg = load_config()
    first: dict = {}
    want = es.admitted(DAYS, first)
    for d in DAYS:
        want[d].add("SPY")
    ext = es.fetch_ext(want)
    allsyms = {s for v in want.values() for s in v if s != "SPY"}
    dfeat = dfs.daily_features(allsyms)
    news = dfs.news_times(want)
    cl = bars.client()

    per_day = {}   # day -> (cache, {strategy: events}, cost)
    n_spread = 0
    for d in DAYS:
        spy = ext.get(("SPY", d))
        cache, cost = {}, {}
        ev = {k: [] for k in ("S1", "S2", "S3", "S4")}
        for s in sorted(want[d]):
            if s == "SPY":
                continue
            df = ext.get((s, d))
            f = dfeat.get((s, d)) or {}
            if df is None or len(df) < 150 or f.get("gap") is None or f["gap"] < -1.0:
                continue
            sp = name_day_spread(cl, s, d)
            n_spread += 1
            time.sleep(0.2)
            if sp is None or sp > 0.20:
                continue
            ind = es.indicators(df, cfg, spy)
            B = es.to_B(df)
            cache[s] = B
            t_on = first.get((s, d))
            nt = news.get((s, d), [])
            took_first = False
            for i in range(2, len(B[0]) - 1):
                m = ind["mins"][i]
                if m < 9 * 60 + 40 or m > 15 * 60 + 30 or B[4][i] < 20:
                    continue
                if t_on is None or ind["ts"][i] < t_on:
                    continue
                good = bool(f.get("above_50ma")) and any(t <= ind["ts"][i] for t in nt)
                e = (ind["ts"][i], s, i)
                cost[(s, i)] = sp
                if not took_first:
                    took_first = True
                    ev["S3"].append(e)
                    if good:
                        ev["S4"].append(e)
                if "mid_rise" in es.fire(ind, i):
                    ev["S1"].append(e)
                    if good:
                        ev["S2"].append(e)
        per_day[d] = (cache, {k: sorted(v) for k, v in ev.items()}, cost)
        print(f"  {d}: names kept {len(cache)}", file=sys.stderr)

    labels = {"S1": "S1 one arm, all names (tomorrow)",
              "S2": "S2 one arm, 50ma+news names",
              "S3": "S3 first entry, all names",
              "S4": "S4 first entry, 50ma+news names"}
    print(f"NAME RANKING STUDY {DAYS[0]}..{DAYS[-1]}  (spread gate 0.20%, gap-down block 1%, "
          f"true name-day spreads; halves {DAYS[0]}..{DAYS[3]} | {DAYS[4]}..{DAYS[-1]})\n")
    print(f"  {'strategy':<36}{'trades':>7}{'win':>6}{'gross':>9}{'cost':>8}{'net':>9}"
          f"{'±':>7}{'P/L':>10}{'h1 net':>9}{'h2 net':>9}  per-day P/L")
    for k in ("S1", "S2", "S3", "S4"):
        trades, daypl = [], []
        for d in DAYS:
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
        g = [t["ret"] for t in trades]
        a = [t["net"] for t in trades if t["day"] in H1]
        b = [t["net"] for t in trades if t["day"] not in H1]
        se = statistics.stdev(net) / len(net) ** .5 if len(net) > 2 else float("nan")
        print(f"  {labels[k]:<36}{len(trades):>7}{sum(x > 0 for x in net) / len(net):>6.0%}"
              f"{statistics.mean(g):>+8.3f}%{statistics.mean(t['cost'] for t in trades):>7.3f}%"
              f"{statistics.mean(net):>+8.3f}%{se:>7.3f}{sum(daypl):>+10.2f}"
              f"{(statistics.mean(a) if a else float('nan')):>+9.3f}"
              f"{(statistics.mean(b) if b else float('nan')):>+9.3f}  "
              + " ".join(f"{x:+.0f}" for x in daypl))


if __name__ == "__main__":
    main()
