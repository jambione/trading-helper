#!/usr/bin/env python3
"""auction_print_check.py — would MOC/MOO orders have filled at the prices the
overnight backtest used?

LONGER_HOLDS_2026-09-27 found one daily-horizon edge: buy the top-20 12-1
momentum liquid names at the close, sell them at the next open, ~16 bp/night
gross. It survives only at about 2 bp/side or less, which assumes auction
fills. The backtest priced those legs at the daily bar's close and next open.

A market-on-close (market-on-open) order at retail size fills at the official
closing (opening) cross price. So the question is whether the daily bar's
close/open IS that cross. This reads the SIP trade tape around 16:00 and 09:30
for a sample of the study's own nightly picks (on_sels.json), finds the
auction prints by condition code, and compares:
  close  official closing print (condition 6 or M) vs daily bar close
  open   official opening print (condition O or Q) vs daily bar open
  night  overnight return from the auction prints vs from the daily bars
and reports the auction print's dollar size (room for our order).

Daily bars here are adjustment=raw for the same days, so prices are comparable
to the tape.

USAGE (on the mini; reads ~/lh_cache/on_sels.json)
  .venv/bin/python tools/studies/auction_print_check.py [--nights 40] [--sels ~/lh_cache/on_sels.json]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402

ET = ZoneInfo("America/New_York")
CLOSE_CODES = {"6", "M"}
OPEN_CODES = {"O", "Q"}


def at(day, h, m, s=0):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=h, minute=m, second=s, tzinfo=ET)


def auction_prints(cl, syms, day, h0, m0, h1, m1, codes, prefer=None):
    """{sym: (price, size)} of the largest print carrying one of *codes*.

    *prefer* (a code) wins over size: the listing market's official print
    ("Q" official open, "M" official close) is the price an MOO/MOC order
    gets, while "O"/"6" also tag other venues' opening/closing prints.
    """
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest
    out = {}
    try:
        got = cl.get_stock_trades(StockTradesRequest(
            symbol_or_symbols=syms, start=at(day, h0, m0).astimezone(timezone.utc),
            end=at(day, h1, m1).astimezone(timezone.utc), feed=DataFeed.SIP)).data
    except Exception as e:  # noqa: BLE001
        print(f"  trades {day}: {e}"[:140], flush=True)
        return out
    for sym, trs in (got or {}).items():
        best = None
        for tr in trs:
            if set(tr.conditions or []) & codes:
                if best is None or float(tr.size) > best[1]:
                    best = (float(tr.price), float(tr.size))
        if prefer:
            off = [tr for tr in trs if prefer in set(tr.conditions or [])]
            if off:
                tr = max(off, key=lambda x: float(x.size))
                best = (float(tr.price), float(tr.size))
        if best:
            out[sym] = best
    return out


def daily_raw(cl, syms, day):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    out = {}
    try:
        got = cl.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=syms, timeframe=TimeFrame.Day, start=at(day, 0, 0).astimezone(timezone.utc),
            end=at(day, 23, 0).astimezone(timezone.utc), feed=DataFeed.SIP,
            adjustment=Adjustment.RAW)).data
    except Exception as e:  # noqa: BLE001
        print(f"  daily {day}: {e}"[:140], flush=True)
        return out
    for sym, bs in (got or {}).items():
        for b in bs:
            out[sym] = (float(b.open), float(b.close))
    return out


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nights", type=int, default=40)
    ap.add_argument("--sels", default=os.path.expanduser("~/lh_cache/on_sels.json"))
    args = ap.parse_args()
    sels = json.load(open(args.sels))
    days = sorted(sels)
    step = max(1, len(days) // args.nights)
    pick = days[::step][:args.nights]
    cl = bars.client()
    rows = []
    for d in pick:
        s = sels[d]
        syms, nxt = s["syms"], s["next"]
        cp = auction_prints(cl, syms, d, 15, 59, 16, 2, CLOSE_CODES, prefer="M")
        time.sleep(0.4)
        op = auction_prints(cl, syms, nxt, 9, 29, 9, 32, OPEN_CODES, prefer="Q")
        time.sleep(0.4)
        dc = daily_raw(cl, syms, d)
        time.sleep(0.4)
        do = daily_raw(cl, syms, nxt)
        time.sleep(0.4)
        for sym in syms:
            if sym in dc and sym in do:
                rows.append({"day": d, "sym": sym, "bar_close": dc[sym][1], "bar_open": do[sym][0],
                             "auc_close": cp.get(sym), "auc_open": op.get(sym)})
        print(f"  {d}: {len(syms)} picks, close prints {len(cp)}, open prints {len(op)}", flush=True)

    print(f"\nAUCTION PRINT CHECK: {len(pick)} nights, {len(rows)} name-nights "
          f"({pick[0]}..{pick[-1]})")
    for leg, bk, ak in (("CLOSE", "bar_close", "auc_close"), ("OPEN", "bar_open", "auc_open")):
        have = [r for r in rows if r[ak]]
        diffs = [abs(r[ak][0] / r[bk] - 1) * 1e4 for r in have]
        dollars = [r[ak][0] * r[ak][1] for r in have]
        print(f"  {leg}: auction print found {len(have)}/{len(rows)}; |auction - daily bar| "
              f"median {q(diffs, .5):.2f} bp, p90 {q(diffs, .9):.2f} bp, "
              f"within 1 bp {sum(x <= 1 for x in diffs) / max(1, len(diffs)):.0%}; "
              f"auction print size median ${q(dollars, .5):,.0f}, p10 ${q(dollars, .1):,.0f}")
    both = [r for r in rows if r["auc_close"] and r["auc_open"]]
    if both:
        g_bar = [(r["bar_open"] / r["bar_close"] - 1) * 1e4 for r in both]
        g_auc = [(r["auc_open"][0] / r["auc_close"][0] - 1) * 1e4 for r in both]
        print(f"\n  overnight return per name-night (n={len(both)}): daily bars {statistics.mean(g_bar):+.1f} bp, "
              f"auction prints {statistics.mean(g_auc):+.1f} bp, difference "
              f"{statistics.mean(a - b for a, b in zip(g_auc, g_bar)):+.2f} bp")
    worst = sorted((r for r in rows if r["auc_close"]),
                   key=lambda r: -abs(r["auc_close"][0] / r["bar_close"] - 1))[:8]
    print("\n  largest close mismatches:")
    for r in worst:
        print(f"    {r['day']} {r['sym']:<5} bar close {r['bar_close']:.2f}  auction {r['auc_close'][0]:.2f} "
              f"({(r['auc_close'][0] / r['bar_close'] - 1) * 1e4:+.1f} bp, size {r['auc_close'][1]:.0f})")


if __name__ == "__main__":
    main()
