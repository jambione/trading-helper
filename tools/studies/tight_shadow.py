#!/usr/bin/env python3
"""Tight-spread shadow list: what a "liquid, tight, moving" name supply would have offered the desk, built after the
close from data a live process could have known (16-minute-delayed SIP, as the desk's own spread cap), and scored
against the desk's real book with the SAME method. Never trades, never touches config or the live desk.
Pre-registration: docs/studies/tight_shadow_prereg.json.

Per DAY:
  universe   common stocks (assets.json filters) with prior close >= $10 and prior-day $vol >= $50M; top 400 by $vol
  checkpoints every 30 min 10:00-15:00 ET; at checkpoint T the knowable time is K = T - 16 min:
    SHADOW(T) = names up >= +1.0% on the day at K (last 1-min close <= K vs prior close) whose SIP NBBO spread at K
                is <= 0.03% of mid; the 15 largest % gainers among them. A name is "on the shadow list" from T to T+30.
  DESK BOOK  names in the desk's admit_funnel kept_symbols during the same 30-min window (what the desk actually held)
  events     desk-like square moments in each group's names while listed: 1-min close with fast %R (21, EMA 7) >= -20
             and slow %R (112, EMA 3) >= -20 (SIP 1-min, 04:00-16:00, prior bars of the day only), first per name per
             clock 15-min block, 10:00-15:30
  outcome    gross15 = next bar open -> close ~15 min later; cost = full SIP spread at the decision (nbbo_at);
             net15 = gross15 - cost (both groups priced identically)
Writes ai_reports/tight_shadow/DAY.json and appends a summary line to ai_reports/tight_shadow/summary.jsonl.
Run on the mini after the close:  .venv/bin/python tools/studies/tight_shadow.py [DAY]
"""
from __future__ import annotations

import collections
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import bars as B  # noqa: E402
import exec_report as ER  # noqa: E402
from allsym_daily_fetch import assets  # noqa: E402
from ticker_filters import is_common, is_levered_etp  # noqa: E402
from overnight_book import FUNDISH  # noqa: E402
from sr_breakout_history import wr  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = os.path.join(ROOT, "ai_reports", "tight_shadow")
DELAY = 16 * 60
MIN_PX, MIN_DV, TOP_U = 10.0, 50e6, 400
MOVER, MAX_SPREAD, LIST_N = 1.0, 0.03, 15
LOG = collections.Counter()


def _req(fn, *a, **k):
    for att in range(5):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            LOG["request_retry"] += 1
            print(f"  retry {att}: {str(e)[:90]}", file=sys.stderr, flush=True)
            time.sleep(8 * (att + 1))
    LOG["request_failed"] += 1
    return None


def daily_prev(cl, syms, day):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    out = {}
    for i in range(0, len(syms), 200):
        r = _req(cl.get_stock_bars, StockBarsRequest(symbol_or_symbols=syms[i:i + 200], timeframe=TimeFrame.Day,
                 start=(d - timedelta(days=7)).astimezone(timezone.utc), end=(d - timedelta(seconds=1)).astimezone(timezone.utc),
                 feed=DataFeed.SIP, adjustment=Adjustment.RAW))
        for s, rows in ((r.data if r else None) or {}).items():
            rows = [b for b in rows if b.timestamp.astimezone(ET).date() < d.date()]
            if rows:
                out[s] = (float(rows[-1].close), float(rows[-1].close) * float(rows[-1].volume))
        time.sleep(0.4)
    return out


def minutes(cl, syms, day):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    out = {}
    for i in range(0, len(syms), 50):
        r = _req(cl.get_stock_bars, StockBarsRequest(symbol_or_symbols=syms[i:i + 50], timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                 start=d.replace(hour=4).astimezone(timezone.utc), end=d.replace(hour=16).astimezone(timezone.utc),
                 feed=DataFeed.SIP, adjustment=Adjustment.RAW))
        for s, rows in ((r.data if r else None) or {}).items():
            out[s] = [(b.timestamp.timestamp(), float(b.open), float(b.high), float(b.low), float(b.close)) for b in rows]
        time.sleep(0.4)
    return out


_NBBO = {}


def spread_at(cl, sym, t):
    k = (sym, int(t))
    if k not in _NBBO:
        q = ER.nbbo_at(cl, sym, datetime.fromtimestamp(t, timezone.utc))
        time.sleep(0.35)
        _NBBO[k] = None if not q else (q[1] - q[0]) / ((q[1] + q[0]) / 2) * 100
    return _NBBO[k]


def desk_book(day):
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    lo, hi = d.replace(hour=9, minute=30).timestamp(), d.replace(hour=16).timestamp()
    seen = []
    for line in open(os.path.join(ROOT, "ai_reports", "events.jsonl")):
        if "admit_funnel" not in line:
            continue
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if lo <= e.get("ts", 0) <= hi:
            seen.append((e["ts"], set(e.get("kept_symbols") or [])))
    return seen


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now(ET).strftime("%Y-%m-%d")
    os.makedirs(OUT, exist_ok=True)
    cl = B.client()
    names = assets()
    commons = sorted(s for s, n in names.items()
                     if s.isalpha() and is_common(s) and not is_levered_etp(s, n) and not FUNDISH.search(n.upper()))
    prev = daily_prev(cl, commons, day)
    uni = sorted((s for s, (c, dv) in prev.items() if c >= MIN_PX and dv >= MIN_DV), key=lambda s: -prev[s][1])[:TOP_U]
    book = desk_book(day)
    book_names = sorted(set().union(*[b for _, b in book])) if book else []
    mb = minutes(cl, sorted(set(uni) | set(book_names)), day)
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    cps = [d.replace(hour=10).timestamp() + 1800 * k for k in range(11)]
    shadow, listed = {}, collections.defaultdict(list)          # sym -> [(t0, t1)]
    for T in cps:
        K = T - DELAY
        movers = []
        for s in uni:
            rows = [r for r in mb.get(s, []) if r[0] + 60 <= K]
            if not rows or s not in prev:
                continue
            chg = (rows[-1][4] / prev[s][0] - 1) * 100
            if chg >= MOVER:
                movers.append((chg, s))
        tight = []
        for chg, s in sorted(movers, reverse=True):
            sp = spread_at(cl, s, K)
            if sp is not None and sp <= MAX_SPREAD:
                tight.append({"sym": s, "chg": round(chg, 2), "spread_pct": round(sp, 4)})
            if len(tight) >= LIST_N:
                break
        shadow[int(T)] = {"movers": len(movers), "list": tight}
        for x in tight:
            listed[x["sym"]].append((T, T + 1800))
    desk_listed = collections.defaultdict(list)
    for T in cps:
        names_T = set().union(*[b for ts, b in book if T <= ts < T + 1800]) if book else set()
        for s in names_T:
            desk_listed[s].append((T, T + 1800))
    t_end = d.replace(hour=15, minute=55).timestamp()

    def events(group):
        ev = []
        for s, wins in group.items():
            rows = sorted(mb.get(s, []))
            if len(rows) < 120:
                LOG["events_short_series"] += 1
                continue
            h = [r[2] for r in rows]
            l = [r[3] for r in rows]
            c = [r[4] for r in rows]
            fast, slow = wr(h, l, c, 21, 7), wr(h, l, c, 112, 3)
            used = set()
            for i in range(len(rows) - 1):
                tq = rows[i][0] + 60
                if not any(a <= tq < b for a, b in wins) or tq > d.replace(hour=15, minute=30).timestamp():
                    continue
                if fast[i] < -20 or slow[i] < -20:
                    continue
                blk = int(tq // 900)
                if blk in used:
                    continue
                used.add(blk)
                entry, tx = rows[i + 1][1], min(rows[i + 1][0] + 900, t_end)
                js = [k for k in range(i + 1, len(rows)) if rows[k][0] <= tx]
                if not js or js[-1] <= i + 1 or abs(rows[js[-1]][0] - tx) > 180:
                    LOG["events_no_exit"] += 1
                    continue
                sp = spread_at(cl, s, tq)
                if sp is None:
                    LOG["events_no_quote"] += 1
                    continue
                g = (c[js[-1]] / entry - 1) * 1e4
                ev.append({"sym": s, "t": tq, "gross15": round(g, 2), "cost": round(sp * 100, 2), "net15": round(g - sp * 100, 2)})
        return ev
    ev_sh, ev_dk = events(listed), events(desk_listed)

    def summ(ev):
        if not ev:
            return {"n": 0}
        return {"n": len(ev), "names": len({e["sym"] for e in ev}), "gross15": round(statistics.fmean(e["gross15"] for e in ev), 2),
                "cost": round(statistics.fmean(e["cost"] for e in ev), 2), "net15": round(statistics.fmean(e["net15"] for e in ev), 2)}
    res = {"day": day, "universe": len(uni), "minute_coverage": f"{sum(1 for s in uni if mb.get(s))}/{len(uni)}",
           "shadow_list_avg": round(statistics.fmean(len(v["list"]) for v in shadow.values()), 1),
           "shadow_names": len(listed), "desk_book_names": len(desk_listed),
           "overlap_names": len(set(listed) & set(desk_listed)),
           "shadow": summ(ev_sh), "desk_book": summ(ev_dk), "log": dict(LOG)}
    json.dump({"summary": res, "checkpoints": shadow, "events_shadow": ev_sh, "events_desk": ev_dk},
              open(os.path.join(OUT, f"{day}.json"), "w"), indent=1)
    rows = [json.loads(x) for x in open(os.path.join(OUT, "summary.jsonl"))] if os.path.exists(os.path.join(OUT, "summary.jsonl")) else []
    rows = [r for r in rows if r["day"] != day] + [res]
    with open(os.path.join(OUT, "summary.jsonl"), "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
