#!/usr/bin/env python3
"""How big must the live overnight account be to hold the strategy in whole shares?

Auction orders (opg/cls) take whole shares only (Alpaca fractional = time_in_force day only), so a small
account can hold only the cheap picks. For each past buy day this replays the live picker
(overnight_book.rank, as overnight_plan_replay does), sizes the 20 picks into whole shares at each account
size, and compares that book with the full equal-weight 20-name book the backtest assumes.

Sizing: target = account / 20 per name; floor(target / raw close) shares; leftover cash then buys one more
share of the most underweight name it can afford, repeatedly. Prices are the buy day's RAW close (what a
share costs); returns are close -> next open from adjusted bars. The live 15:40 -1% intraday filter is not
applied (it needs intraday prices), so the live book drops a few more names than this.

Also reports the current live rule ($25/order cap, 1 share each).

Read-only. Run on the mini: .venv/bin/python tools/studies/overnight_account_size.py [--days 120]
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not os.path.exists(os.path.join(ROOT, "overnight_book.py")):   # copied to /tmp and run from the repo
    ROOT = os.getcwd()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import overnight_book as ob  # noqa: E402
import overnight_plan_replay as rp  # noqa: E402

SIZES = [100, 250, 500, 1_000, 2_500, 5_000, 10_000, 25_000]
EDGE_BP = 16.0          # backtest OOS gross bp/night, for the "nights to confirm" and $/night columns


def raw_closes(syms, start, end):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    dc = ob.data_client()
    out = {}
    syms = sorted(syms)
    for i in range(0, len(syms), 200):
        got = dc.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=syms[i:i + 200], timeframe=TimeFrame.Day,
            start=datetime.combine(start, datetime.min.time(), ob.ET).astimezone(timezone.utc),
            end=datetime.combine(end, datetime.max.time(), ob.ET).astimezone(timezone.utc),
            adjustment=Adjustment.RAW, feed=DataFeed.SIP)).data
        for s, bs in (got or {}).items():
            out[s] = {b.timestamp.astimezone(ob.ET).strftime("%Y-%m-%d"): float(b.close) for b in bs}
    return out


def size_book(prices: dict, cash: float, n: int = 20) -> dict:
    target = cash / n
    sh = {s: int(target // p) for s, p in prices.items() if p > 0}
    left = cash - sum(sh[s] * prices[s] for s in sh)
    while True:
        cands = [s for s in sh if prices[s] <= left]
        if not cands:
            break
        s = min(cands, key=lambda x: sh[x] * prices[x])        # most underweight first
        sh[s] += 1
        left -= prices[s]
    return {s: q for s, q in sh.items() if q > 0}


def live_rule(prices: dict, cash: float = 100.0, per_order: float = 25.0) -> dict:
    out, left = {}, cash
    for s, p in prices.items():                                 # plan order = momentum rank
        if 0 < p <= per_order and p <= left:
            out[s] = 1
            left -= p
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    args = ap.parse_args()
    tc = ob.book_client()
    today = datetime.now(ob.ET).date()
    sess = [c.date for c in ob.calendar(tc, today - timedelta(days=420 + 2 * args.days), today)]
    done = [d for d in sess if d < today]
    buy_days = done[-(args.days + 1):-1]
    first_key = sess[sess.index(buy_days[0]) - (ob.LOOK + 5)]
    uni = ob.universe(tc)
    print(f"universe {len(uni)}; daily bars {first_key} .. {done[-1]}; {len(buy_days)} buy days", flush=True)
    bars = ob.daily_closes(sorted(uni), first_key, done[-1])
    plans = {}
    for day in buy_days:
        i = sess.index(day)
        keys = [d.isoformat() for d in sess[i - (ob.LOOK + 5):i]]
        plans[day.isoformat()] = [r["sym"] for r in ob.rank(bars, keys)[:ob.TOP_N]]
    union = set().union(*plans.values())
    oc = rp.open_close(union, buy_days[0], done[-1])
    raw = raw_closes(union, buy_days[0], done[-1])

    rows = {k: [] for k in ["full"] + SIZES + ["live$100"]}
    held = {k: [] for k in SIZES + ["live$100"]}
    deployed = {k: [] for k in SIZES + ["live$100"]}
    prices_seen = []
    for day in buy_days:
        k, nxt = day.isoformat(), done[done.index(day) + 1].isoformat()
        on, px = {}, {}
        for s in plans[k]:
            a, b, p = oc.get(s, {}).get(k), oc.get(s, {}).get(nxt), raw.get(s, {}).get(k)
            if a and b and a[1] > 0 and p:
                on[s], px[s] = b[0] / a[1] - 1, p
        if len(on) < 15:
            continue
        prices_seen += list(px.values())
        rows["full"].append(np.mean(list(on.values())))
        for size in SIZES + ["live$100"]:
            book = live_rule(px) if size == "live$100" else size_book(px, float(size))
            cash = 100.0 if size == "live$100" else float(size)
            val = sum(q * px[s] for s, q in book.items())
            # return on the whole account (idle cash earns 0), which is what the account actually sees
            r = sum(q * px[s] * on[s] for s, q in book.items()) / cash if book else 0.0
            rows[size].append(r)
            held[size].append(len(book))
            deployed[size].append(val / cash)

    full = np.array(rows["full"]) * 1e4
    n = len(full)
    pq = np.percentile(prices_seen, [10, 25, 50, 75, 90])
    print(f"\n{n} nights; pick prices (raw close) p10/25/50/75/90: " + " / ".join(f"${x:,.0f}" for x in pq))
    print(f"full equal-weight 20: mean {full.mean():+.1f} bp/night, sd {full.std(ddof=1):.0f} bp\n")
    print(f"{'account':>10} {'names':>6} {'deployed':>9} {'mean bp':>8} {'sd bp':>6} {'corr':>5} {'track sd':>9} "
          f"{'nights t=2 @16bp':>17} {'$/night @16bp':>14}")
    for size in SIZES + ["live$100"]:
        r = np.array(rows[size]) * 1e4
        cash = 100.0 if size == "live$100" else float(size)
        sd = r.std(ddof=1)
        dep = np.mean(deployed[size])
        corr = np.corrcoef(r, full)[0, 1] if r.std() > 0 else float("nan")
        track = (r - full * dep).std(ddof=1)
        need = math.ceil((2 * sd / (EDGE_BP * dep)) ** 2) if dep > 0 else float("nan")
        label = f"${size:,}" if size != "live$100" else "live rule"
        print(f"{label:>10} {np.mean(held[size]):>6.1f} {dep:>8.0%} {r.mean():>+8.1f} {sd:>6.0f} {corr:>5.2f} "
              f"{track:>9.0f} {need:>17} {cash * EDGE_BP * dep / 1e4:>14.2f}")
    print("\nmean bp / sd bp are on the whole account (idle cash earns 0). corr = nightly correlation with the full "
          "20-name book; track sd = sd of (account return - deployed x full-book return). 'nights t=2' = sessions to "
          "confirm a 16 bp/night edge at this book's own noise, scaled by the share deployed.")


if __name__ == "__main__":
    main()
