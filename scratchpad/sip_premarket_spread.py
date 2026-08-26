#!/usr/bin/env python3
"""Is the premarket book really 1.7R wide, or is that IEX not showing up?

The desk records spread_r from DataFeed.IEX quotes. IEX is one venue with a
couple of percent of consolidated volume and very little of it before 09:30,
so a wide premarket "spread" may be measuring IEX's absence rather than the
cost of crossing. Orders route against the NBBO, so the NBBO is the number
that prices a premarket entry.

This re-prices the SAME shadow rows against SIP historical quotes, matched
point-in-time (strictly the last quote BEFORE the row's instant, never the
one that closed it). Paired on identical rows and identical stops, so the
two columns are comparable by construction.

Self-validating: it recomputes the stored IEX spread_r from the row's own
bid/ask/stop first. If that does not reproduce the logged value the
arithmetic has drifted and the comparison is void, so it says so and stops.

Quote age is carried through because a tight spread quoted 20 minutes ago is
not a book you can trade against; "fresh" rows are reported separately.

Read-only. Usage:
    .venv/bin/python sip_premarket_spread.py [--days 5] [--fresh-sec 60]
"""
from __future__ import annotations

import argparse
import bisect
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
sys.path.insert(0, "tools")

import bars  # noqa: E402
import shadow_report as sr  # noqa: E402

ET = ZoneInfo("America/New_York")
RTH_LIKE = 0.13          # 2x the RTH median — "no worse than a normal moment"
OPEN_MIN = 9 * 60 + 30


def spread_r(ask, bid, stop):
    """Exact mirror of ai_entry_watch._spread_r. Round trip pays it twice."""
    try:
        a, b, st = float(ask), float(bid), float(stop)
    except (TypeError, ValueError):
        return None
    if not (a > 0 and b > 0 and 0 < st < a):
        return None
    if b >= a:                       # locked/crossed is unknowable, not free
        return None
    risk = a - st
    if risk <= 0:
        return None
    return round(2.0 * (a - b) / risk, 5)


def dist(label, xs, extra=""):
    if not xs:
        print(f"{label:<26} no rows")
        return
    xs = sorted(xs)
    n = len(xs)
    q = lambda p: xs[int(p * (n - 1))]  # noqa: E731
    good = 100 * sum(1 for v in xs if v <= RTH_LIKE) / n
    print(f"{label:<26} n={n:<6} p10 {q(.1):>7.3f}  median {q(.5):>7.3f}  "
          f"p90 {q(.9):>7.3f}  RTH-like {good:>3.0f}%{extra}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--fresh-sec", type=float, default=60.0)
    ap.add_argument("--session", choices=("pre", "rth"), default="pre")
    ap.add_argument("--window", default="",
                    help="RTH only, e.g. 10:00-11:00 — bounds the quote pull")
    ap.add_argument("--max-pairs", type=int, default=0,
                    help="cap symbol-days fetched (largest first); 0 = all")
    args = ap.parse_args()
    if args.session == "rth":
        lo, hi = (args.window or "10:00-11:00").split("-")
        w0 = int(lo[:2]) * 60 + int(lo[3:])
        w1 = int(hi[:2]) * 60 + int(hi[3:])
    else:
        w0, w1 = 4 * 60, OPEN_MIN
    in_window = lambda m: w0 <= m < w1  # noqa: E731

    rows = sr.load()
    days = sorted({bars.day_of(r["ts"]) for r in rows if r.get("ts")})[-args.days:]
    dayset = set(days)
    pre = [r for r in rows
           if r.get("ts") and bars.day_of(r["ts"]) in dayset
           and r.get("arm_ok") is not None
           and in_window(bars.et_minutes(r["ts"]))
           and r.get("spread_r") is not None
           and r.get("stop_price") is not None]
    print(f"sessions: {', '.join(days)}")
    print(f"window: {w0//60:02d}:{w0%60:02d}-{w1//60:02d}:{w1%60:02d} ET "
          f"({args.session})")
    print(f"rows with a logged IEX spread: {len(pre)}\n")
    if not pre:
        return 1

    # --- self-validation: reproduce the logged IEX number exactly ----------
    # Rows written before the 2026-08-21 fix recorded a locked book (bid ==
    # ask) as a free 0.000R round trip. Current _spread_r calls that
    # unknowable and returns None. Those rows are dropped rather than
    # compared — keeping them would put the names whose book the desk cannot
    # see at the top of the cheapest bucket, which is the bug, not the data.
    ok, locked, bad = [], 0, 0
    for r in pre:
        mine = spread_r(r.get("price"), r.get("bid"), r.get("stop_price"))
        if mine is None:
            try:
                if float(r.get("bid")) >= float(r.get("price")):
                    locked += 1
                    continue
            except (TypeError, ValueError):
                pass
            bad += 1
            continue
        if abs(mine - float(r["spread_r"])) > 1e-5:
            bad += 1
            continue
        ok.append(r)
    if bad:
        print(f"ABORT: arithmetic does not reproduce {bad}/{len(pre)} logged "
              f"spread_r values — the comparison would not be paired.")
        return 2
    pre = ok
    print(f"arithmetic check: reproduced all {len(pre)} logged IEX values ✓")
    if locked:
        print(f"  dropped {locked} pre-fix locked-book rows logged as 0.000R")
    print()

    cl = bars.client()
    if cl is None:
        print("no data client — cannot fetch SIP quotes")
        return 1
    from alpaca.data.requests import StockQuotesRequest
    from alpaca.data.enums import DataFeed
    import pandas as pd

    counts: dict = defaultdict(int)
    for r in pre:
        counts[(r.get("symbol") or r.get("ticker"), bars.day_of(r["ts"]))] += 1
    pairs = sorted(counts, key=lambda k: -counts[k])
    if args.max_pairs:
        pairs = pairs[:args.max_pairs]
        keep = set(pairs)
        pre = [r for r in pre
               if (r.get("symbol") or r.get("ticker"),
                   bars.day_of(r["ts"])) in keep]
        print(f"capped to {len(pairs)} busiest symbol-days "
              f"({len(pre)} rows)\n")
    books: dict = {}
    for sym, day in pairs:
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        try:
            df = cl.get_stock_quotes(StockQuotesRequest(
                symbol_or_symbols=sym,
                start=d.replace(hour=w0 // 60,
                                minute=w0 % 60).astimezone(timezone.utc),
                end=d.replace(hour=w1 // 60,
                              minute=w1 % 60).astimezone(timezone.utc),
                feed=DataFeed.SIP)).df
        except Exception as e:  # noqa: BLE001
            print(f"  {sym} {day}: fetch failed ({type(e).__name__})")
            books[(sym, day)] = ([], [], [])
            continue
        if df is None or df.empty:
            books[(sym, day)] = ([], [], [])
            continue
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(sym, level="symbol")
        df = df.sort_index()
        books[(sym, day)] = (
            [t.timestamp() for t in df.index],
            [float(v) for v in df["bid_price"]],
            [float(v) for v in df["ask_price"]],
        )
    got = sum(1 for v in books.values() if v[0])
    print(f"SIP premarket books fetched: {got} of {len(pairs)} symbol-days\n")

    iex, sip, sip_fresh = [], [], []
    by_hh = defaultdict(lambda: {"iex": [], "sip": []})
    by_sym = defaultdict(lambda: {"iex": [], "sip": [], "age": []})
    ages, no_quote, stale = [], 0, 0

    for r in pre:
        sym = r.get("symbol") or r.get("ticker")
        day = bars.day_of(r["ts"])
        stamps, bids, asks = books.get((sym, day), ([], [], []))
        v_iex = float(r["spread_r"])
        ts = float(r["ts"])
        # strictly BEFORE the instant — never the quote that closed it
        i = bisect.bisect_left(stamps, ts) - 1
        if i < 0:
            no_quote += 1
            continue
        age = ts - stamps[i]
        v_sip = spread_r(asks[i], bids[i], r["stop_price"])
        if v_sip is None:
            no_quote += 1
            continue
        iex.append(v_iex)
        sip.append(v_sip)
        ages.append(age)
        if age <= args.fresh_sec:
            sip_fresh.append(v_sip)
        else:
            stale += 1
        b = (bars.et_minutes(ts) // 30) * 30
        by_hh[b]["iex"].append(v_iex)
        by_hh[b]["sip"].append(v_sip)
        by_sym[sym]["iex"].append(v_iex)
        by_sym[sym]["sip"].append(v_sip)
        by_sym[sym]["age"].append(age)

    print(f"paired rows: {len(iex)}   (no usable SIP quote: {no_quote})")
    if ages:
        a = sorted(ages)
        print(f"matched quote age: median {a[len(a)//2]:.1f}s  "
              f"p90 {a[int(.9*(len(a)-1))]:.1f}s  "
              f"older than {args.fresh_sec:.0f}s: {100*stale/len(a):.0f}%\n")
    if not iex:
        return 1

    print("PAIRED — same rows, same stops, two feeds")
    dist("  IEX (what desk logs)", iex)
    dist("  SIP (what you cross)", sip)
    dist(f"  SIP, quote <{args.fresh_sec:.0f}s old", sip_fresh)
    med_i = sorted(iex)[len(iex) // 2]
    med_s = sorted(sip)[len(sip) // 2]
    if med_s > 0:
        print(f"\n  IEX overstates the premarket book by {med_i/med_s:.1f}x "
              f"at the median ({med_i:.3f}R vs {med_s:.3f}R)")
    print(f"  round trip to scratch: IEX says {med_i:.2f}R, "
          f"SIP says {med_s:.2f}R")

    print("\nBY HALF HOUR (median spread_r)")
    print(f"{'ET':<8}{'n':>7}{'IEX':>9}{'SIP':>9}{'SIP RTH-like':>14}")
    print("-" * 47)
    for b in sorted(by_hh):
        d = by_hh[b]
        n = len(d["sip"])
        mi = sorted(d["iex"])[n // 2]
        ms = sorted(d["sip"])[n // 2]
        good = 100 * sum(1 for v in d["sip"] if v <= RTH_LIKE) / n
        hh, mm = divmod(b, 60)
        print(f"{hh:02d}:{mm:02d}   {n:>7}{mi:>9.3f}{ms:>9.3f}{good:>13.0f}%")

    print("\nBY NAME (median spread_r, n>=30)")
    print(f"{'symbol':<8}{'n':>7}{'IEX':>9}{'SIP':>9}{'SIP RTH-like':>14}"
          f"{'q age':>9}")
    print("-" * 56)
    out = []
    for sym, d in by_sym.items():
        n = len(d["sip"])
        if n < 30:
            continue
        ms = sorted(d["sip"])[n // 2]
        mi = sorted(d["iex"])[n // 2]
        good = 100 * sum(1 for v in d["sip"] if v <= RTH_LIKE) / n
        age = sorted(d["age"])[n // 2]
        out.append((ms, sym, n, mi, good, age))
    for ms, sym, n, mi, good, age in sorted(out):
        print(f"{sym:<8}{n:>7}{mi:>9.3f}{ms:>9.3f}{good:>13.0f}%{age:>8.0f}s")

    playable = [o for o in out if o[0] <= RTH_LIKE]
    print(f"\nnames crossable premarket on SIP: {len(playable)} of {len(out)}"
          f"  ({', '.join(o[1] for o in playable) or 'none'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
