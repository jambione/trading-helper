#!/usr/bin/env python3
"""overnight_plan_replay.py — run the live overnight book's own picking code
(overnight_book.rank) on past sessions and compare with the backtest.

Paper trading can only confirm the overnight edge slowly (a 178 bp nightly sd
against a 16 bp mean is ~6 months to t=1). What it CAN catch fast is a live
plan that does not pick what the backtest picks: different data, screens or
timing. This replays the live code on the last N sessions instead of waiting.

For each buy day it takes the same inputs the 06:30 plan would have had
(sessions before the day; today's asset list, so a little survivorship),
ranks with overnight_book.rank, and compares the top 20 with the backtest's
top 20 from ~/lh_cache (panel at /tmp/lh/panel.npz) where the panel covers the
day. Both sets are scored close -> next open from SIP daily bars (adjusted),
and the backtest set is also scored from the panel as a cross-check.

Read-only. ~30 data requests. Usage (mini, venv):
    .venv/bin/python tools/studies/overnight_plan_replay.py [--days 60]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import overnight_book as ob  # noqa: E402

LH = os.path.expanduser("~/lh_cache")


def backtest_picks():
    """{YYYY-MM-DD: ([syms], {sym: ON})} from the backtest panel, or {}."""
    if not os.path.exists("/tmp/lh/panel.npz"):
        return {}
    sys.path.insert(0, LH)
    import lh_core as C
    import lh_strat as S
    P = S.returns(C.load())
    cf, ON, d, syms = P["cf"], P["ON"], P["dates"], P["syms"]
    T = cf.shape[0]
    momd = np.full_like(cf, np.nan)
    with np.errstate(all="ignore"):
        momd[252:] = cf[231:-21] / cf[:-252] - 1
    out = {}
    for t in range(max(252, T - 200), T - 1):
        x = np.where(P["liquid"][t] & np.isfinite(momd[t]), momd[t], np.nan)
        ok = np.where(np.isfinite(x))[0]
        if len(ok) < 30:
            continue
        j = ok[np.argsort(-x[ok])][:20]
        out[str(d[t].date())] = ([str(syms[i]) for i in j], {str(syms[i]): float(ON[t + 1, i]) for i in j})
    return out


def open_close(syms, start, end):
    """{sym: {YYYY-MM-DD: (adj open, adj close)}} from SIP daily bars."""
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
            adjustment=Adjustment.ALL, feed=DataFeed.SIP)).data
        for s, bs in (got or {}).items():
            out[s] = {b.timestamp.astimezone(ob.ET).strftime("%Y-%m-%d"): (float(b.open), float(b.close)) for b in bs}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()
    tc = ob.book_client()
    today = datetime.now(ob.ET).date()
    sess = [c.date for c in ob.calendar(tc, today - timedelta(days=420 + 2 * args.days), today)]
    done = [d for d in sess if d < today]
    buy_days = done[-(args.days + 1):-1]          # each needs its next session's open
    first_key = sess[sess.index(buy_days[0]) - (ob.LOOK + 5)]
    uni = ob.universe(tc)
    print(f"universe {len(uni)}; fetching daily bars {first_key} .. {done[-1]}", flush=True)
    bars = ob.daily_closes(sorted(uni), first_key, done[-1])
    bt = backtest_picks()
    plans = {}
    for day in buy_days:
        i = sess.index(day)
        keys = [d.isoformat() for d in sess[i - (ob.LOOK + 5):i]]
        plans[day.isoformat()] = [r["sym"] for r in ob.rank(bars, keys)[:ob.TOP_N]]
    union = set().union(*plans.values()) | {s for k, v in bt.items() if k in plans for s in v[0]}
    oc = open_close(union, buy_days[0], done[-1])

    def on(sym, day, nxt):
        a, b = oc.get(sym, {}).get(day), oc.get(sym, {}).get(nxt)
        return (b[0] / a[1] - 1) if (a and b and a[1] > 0) else None

    print(f"\n{'buy day':10} {'overlap':>7} {'live bp':>8} {'bt bp':>7} {'bt(panel)':>9}  live-only / bt-only")
    live_m, bt_m, both = [], [], []
    for n, day in enumerate(buy_days):
        k, nxt = day.isoformat(), done[done.index(day) + 1].isoformat()
        lv = [x for x in (on(s, k, nxt) for s in plans[k]) if x is not None]
        lm = np.mean(lv) * 1e4 if lv else float("nan")
        live_m.append(lm)
        if k in bt:
            bsyms, bon = bt[k]
            bv = [x for x in (on(s, k, nxt) for s in bsyms) if x is not None]
            bm = np.mean(bv) * 1e4 if bv else float("nan")
            pm = np.nanmean([v for v in bon.values()]) * 1e4
            ov = len(set(bsyms) & set(plans[k]))
            bt_m.append(bm); both.append((lm, bm))
            lo = ",".join(sorted(set(plans[k]) - set(bsyms))[:4])
            bo = ",".join(sorted(set(bsyms) - set(plans[k]))[:4])
            print(f"{k:10} {ov:>5}/20 {lm:+8.1f} {bm:+7.1f} {pm:+9.1f}  {lo} / {bo}")
        else:
            print(f"{k:10} {'-':>7} {lm:+8.1f} {'(panel ends)':>17}")
    lm = np.array(live_m)
    print(f"\nlive code, {len(lm)} nights: mean {np.nanmean(lm):+.1f} bp, green {np.mean(lm[np.isfinite(lm)] > 0):.0%}")
    if both:
        b = np.array(both)
        print(f"nights with both ({len(b)}): live {np.nanmean(b[:, 0]):+.1f} bp vs backtest picks {np.nanmean(b[:, 1]):+.1f} bp; "
              f"nightly correlation {np.corrcoef(b[:, 0], b[:, 1])[0, 1]:.2f}")


if __name__ == "__main__":
    main()
