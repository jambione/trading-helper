#!/usr/bin/env python3
"""source_optimal_study.py — each seed source's names under optimal conditions.

The live record says momentum is the losing source (-0.066R/trade, t -4.3),
but live momentum trades also carried stale tape, the paint-latch bug and a
merge-contaminated source label. This asks what the SOURCE is worth when the
plumbing is perfect:

  names      every name the source nominated (admit_ledger 'seed' rows, any
             verdict), seated from its first nomination while the source keeps
             nominating it (gaps < 3 min merged), 09:30-15:50
  data       SIP 1m bars from 04:00 (premarket warms the %R lines as live)
  arm        the live trigger: fast %R(21, EWM 7) crosses up through -50 with
             the slow line (%R 21 on 15m bars) rising; no staleness, no latch
             loss, no admission refusal
  gates      the intended ones only: $20-$100 at the cross, no gap-down > 1%
  book       live rules: max 5 open, one per name, 120 s cooldown after exit
  fill       the cross minute's close (never a shadow price)
  exits      (a) fixed hold, default 5 min (the 3-8 min target), and
             (b) live-shaped trail: arm at +0.30%, 0.35% from peak, 1% seed
             stop, 30-min time stop; stops read lows first (conservative)
  cost       the real SIP (ask-bid)/mid at entry and at exit, half each
             (cached in ai_reports/source_study_spreads.json)

USAGE (on the mini; needs Alpaca data keys; cache-first, safe to rerun)
  .venv/bin/python tools/studies/source_optimal_study.py
  .venv/bin/python tools/studies/source_optimal_study.py --days 2026-09-24 2026-09-25 --hold 8
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import pickle
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import mid_rise_runway_study as mr  # noqa: E402

ET = ZoneInfo("America/New_York")
BAR_CACHE = os.path.join(ROOT, "ai_reports", "source_study_bars.pkl")
SPREAD_CACHE = os.path.join(ROOT, "ai_reports", "source_study_spreads.json")
SOURCES = ("momentum", "trending", "movers", "research")
GAP_SEC = 180.0
# Sticky nomination: the source pools flicker (9/25 median enter->leave 7-13 s;
# a momentum name spent a median 1.3 min nominated in total), so "optimal" is
# seated from the first nomination until STICKY_SEC after the last one.
STICKY_SEC = 3600.0
COOLDOWN = 120.0
MAX_OPEN = 5
OPEN_MIN, LAST_ENTRY_MIN, FLAT_MIN = 9 * 60 + 30, 15 * 60 + 45, 15 * 60 + 50


def et_min(ts):
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def nominations(day, mode="sources"):
    """source -> sym -> [(start, end)].

    mode 'sources': the session recorder's sources stream (enter/leave per
    symbol and source) — every name the source actually nominated, from 9/25.
    mode 'refused': admit_ledger 'seed' rows, which are written ONLY for names
    the desk refused at seeding (thin_rvol, price_cap...): what the gates
    turned away, not what the source offered.
    """
    if mode == "sources":
        return _from_sources(day)
    path = os.path.join(ROOT, "ai_reports", "admit_ledger", f"{day}.jsonl")
    pts = defaultdict(lambda: defaultdict(list))
    for line in open(path):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("stage") != "seed":
            continue
        src = str(r.get("source") or "")
        if src not in SOURCES:
            continue
        pts[src][str(r.get("symbol") or "").upper()].append(float(r["ts"]))
    out = defaultdict(dict)
    for src, by in pts.items():
        for sym, ts in by.items():
            ts.sort()
            iv = [[ts[0], ts[0]]]
            for t in ts[1:]:
                if t - iv[-1][1] <= GAP_SEC:
                    iv[-1][1] = t
                else:
                    iv.append([t, t])
            out[src][sym] = _sticky([(a, b) for a, b in iv])
    return out


def _from_sources(day):
    import gzip
    path = os.path.join(ROOT, "ai_reports", "sessions", day, "sources.jsonl.gz")
    out = defaultdict(dict)
    if not os.path.exists(path):
        return out
    open_at: dict[tuple, float] = {}
    ivs = defaultdict(list)
    for line in gzip.open(path, "rt"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        src = "research" if str(r.get("source") or "") in ("agy", "xai", "research") else str(r.get("source") or "")
        if src not in SOURCES or r.get("event") not in ("enter", "leave"):
            continue
        k = (src, str(r.get("symbol") or "").upper())
        t = float(r["ts"])
        if r["event"] == "enter":
            open_at.setdefault(k, t)
        elif k in open_at:
            ivs[k].append((open_at.pop(k), t))
    for k, t in open_at.items():
        ivs[k].append((t, t + 86400))
    for (src, sym), v in ivs.items():
        out[src][sym] = _sticky(v)
    return out


def _sticky(ivs):
    ivs = sorted(ivs)
    return [(ivs[0][0], min(ivs[-1][1], ivs[-1][0] + 86400) + STICKY_SEC)]


def load_bars(days_syms):
    cache = {}
    if os.path.exists(BAR_CACHE):
        with open(BAR_CACHE, "rb") as f:
            cache = pickle.load(f)
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    import ai_entry_watch as ew
    cl = ew._data_client()
    dirty = False
    for day, syms in days_syms.items():
        want = sorted(s for s in syms if (s, day) not in cache)
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        prev_close = daily_prev_close(cl, want, day) if want else {}
        for i in range(0, len(want), 50):
            batch = want[i:i + 50]
            got = {}
            try:
                bs = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=batch, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=d.replace(hour=4).astimezone(timezone.utc),
                    end=d.replace(hour=16, minute=5).astimezone(timezone.utc),
                    feed=DataFeed.SIP))
                for sym, rows in (bs.data or {}).items():
                    got[sym] = ([r.timestamp.timestamp() for r in rows],
                                [float(r.open) for r in rows], [float(r.high) for r in rows],
                                [float(r.low) for r in rows], [float(r.close) for r in rows],
                                [float(r.volume) for r in rows])
            except Exception as e:  # noqa: BLE001
                print(f"  bars {day} batch {i}: {type(e).__name__}: {e}"[:160])
            for s in batch:
                cache[(s, day)] = (got.get(s), prev_close.get(s))
            dirty = True
            time.sleep(1.0)
        print(f"  bars {day}: {len(want)} fetched, {len(syms)} wanted")
    if dirty:
        with open(BAR_CACHE, "wb") as f:
            pickle.dump(cache, f)
    return cache


def daily_prev_close(cl, syms, day):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    out = {}
    for i in range(0, len(syms), 100):
        try:
            bs = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=syms[i:i + 100], timeframe=TimeFrame.Day,
                start=(d.replace(hour=0)).astimezone(timezone.utc).replace(day=max(1, d.day - 10))
                if d.day > 10 else d.astimezone(timezone.utc).replace(day=1, hour=0),
                end=d.replace(hour=0).astimezone(timezone.utc), feed=DataFeed.SIP))
            for sym, rows in (bs.data or {}).items():
                rows = [r for r in rows if r.timestamp.astimezone(ET).strftime("%Y-%m-%d") < day]
                if rows:
                    out[sym] = float(rows[-1].close)
        except Exception as e:  # noqa: BLE001
            print(f"  daily {day}: {e}"[:120])
        time.sleep(1.0)
    return out


class Spreads:
    def __init__(self):
        try:
            self.d = json.load(open(SPREAD_CACHE))
        except (OSError, ValueError):
            self.d = {}
        self.n_new = 0

    def at(self, sym, t):
        k = f"{sym}|{int(t)}"
        if k not in self.d:
            import ai_entry_watch as ew
            ew._SIP_SPREAD_CACHE.pop(sym, None)
            self.d[k] = ew.sip_spread_pct(sym, now=t, ttl=0, delay_min=0)
            self.n_new += 1
            if self.n_new % 100 == 0:
                self.save()
        return self.d[k]

    def save(self):
        with open(SPREAD_CACHE, "w") as f:
            json.dump(self.d, f)


def crosses(B, level=-50.0):
    """(index, slow_rising) for fast %R up-crosses through -50.

    Slow rising is a state, as live reads it: the slow line's current value
    above its previous distinct value. The slow line is %R(21) on 15m bars
    and moves once per 15 minutes, so comparing it minute to minute (what
    mid_rise_runway_study.find_crosses does) reads 'flat' ~97% of the time.
    """
    t, o, h, l, c, v = B
    fast = mr.percent_r_series(h, l, c, mr.FAST_LEN, mr.FAST_SPAN)
    slow = mr.slow_percent_r_on_1m(t, h, l, c)
    out = []
    cur = prev = None
    for i in range(len(c)):
        s_ = slow[i]
        if s_ is not None and s_ != cur:
            prev, cur = cur, s_
        if i == 0:
            continue
        a, b = fast[i - 1], fast[i]
        if a is None or b is None or not (a <= level < b):
            continue
        out.append((i, cur is not None and prev is not None and cur > prev))
    return out


def candidates(day, noms, bars):
    """All live-trigger crosses on nominated names, with gates applied."""
    out = []
    for sym, ivs in noms.items():
        rec = bars.get((sym, day))
        if not rec or not rec[0]:
            continue
        B, prev_close = rec
        t, o, h, l, c, v = B
        open_px = next((o[i] for i in range(len(t)) if et_min(t[i]) >= OPEN_MIN), None)
        gap = (open_px / prev_close - 1) * 100 if open_px and prev_close else None
        if gap is not None and gap < -1.0:
            continue
        for i, rising in crosses(B):
            if not rising:
                continue
            te = t[i] + 60  # the cross is known at the minute's close
            m = et_min(te)
            if not (OPEN_MIN <= m <= LAST_ENTRY_MIN):
                continue
            if not any(a <= te <= b for a, b in ivs):
                continue
            if not (20.0 <= c[i] <= 100.0):
                continue
            out.append((te, sym, i))
    out.sort()
    return out


def walk_exit(B, i0, px, mode, hold_min):
    t, o, h, l, c, v = B
    te = t[i0] + 60
    flat_t = None
    for k in range(i0 + 1, len(t)):
        if et_min(t[k]) >= FLAT_MIN:
            flat_t = k
            break
    if mode == "hold":
        j = bisect.bisect_left(t, te + hold_min * 60 - 60)
        j = min(j, len(t) - 1)
        if flat_t is not None:
            j = min(j, flat_t)
        return c[j], t[j] + 60
    stop, peak, armed = px * 0.99, px, False
    for k in range(i0 + 1, len(t)):
        if flat_t is not None and k >= flat_t:
            return c[k], t[k] + 60
        if l[k] <= stop:
            return min(stop, o[k]), t[k] + 60
        peak = max(peak, h[k])
        if not armed and peak >= px * 1.003:
            armed = True
        if armed:
            stop = max(stop, peak * (1 - 0.0035))
        if t[k] + 60 - te >= 30 * 60:
            return c[k], t[k] + 60
    return c[-1], t[-1] + 60


def run_book(day, cands, bars, mode, hold_min, spreads):
    open_until: dict[str, float] = {}
    cool: dict[str, float] = {}
    trades = []
    for te, sym, i in cands:
        live = [s for s, u in open_until.items() if u > te]
        if len(live) >= MAX_OPEN or sym in live or cool.get(sym, 0) > te:
            continue
        B, _ = bars[(sym, day)]
        px = B[4][i]
        xpx, xt = walk_exit(B, i, px, mode, hold_min)
        gross = xpx / px - 1
        se, sx = (spreads.at(sym, te), spreads.at(sym, xt)) if spreads else (None, None)
        cost = (se + sx) / 200 if se is not None and sx is not None else None
        trades.append({"day": day, "sym": sym, "t": te, "xt": xt, "px": px,
                       "gross": gross, "cost": cost, "se": se,
                       "net": None if cost is None else gross - cost})
        open_until[sym] = xt
        cool[sym] = xt + COOLDOWN
    return trades


def concurrency(trades, day):
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    t0 = d.replace(hour=9, minute=40).timestamp()
    t1 = d.replace(hour=15, minute=50).timestamp()
    n = ge1 = ge2 = 0
    tot = 0
    for m in range(int(t0), int(t1), 60):
        k = sum(1 for x in trades if x["t"] <= m < x["xt"])
        n += 1
        tot += k
        ge1 += k >= 1
        ge2 += k >= 2
    return tot / n, ge1 / n, ge2 / n


def stats(xs):
    if not xs:
        return None, None
    m = statistics.mean(xs)
    sd = statistics.pstdev(xs) if len(xs) > 1 else 0.0
    return m, (m / (sd / math.sqrt(len(xs))) if sd > 0 else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="*", default=None)
    ap.add_argument("--hold", type=float, default=5.0)
    ap.add_argument("--no-spread", action="store_true", help="skip real spreads (gross only)")
    ap.add_argument("--noms", choices=("sources", "refused"), default="sources")
    args = ap.parse_args()
    if args.days:
        days = args.days
    elif args.noms == "sources":
        base = os.path.join(ROOT, "ai_reports", "sessions")
        days = sorted(d for d in os.listdir(base) if os.path.exists(os.path.join(base, d, "sources.jsonl.gz")))
    else:
        days = sorted(f[:-6] for f in os.listdir(os.path.join(ROOT, "ai_reports", "admit_ledger"))
                      if f.endswith(".jsonl"))
    noms = {d: nominations(d, args.noms) for d in days}
    want = {d: set().union(*[set(by) for by in noms[d].values()]) for d in days}
    bars = load_bars(want)
    spreads = None if args.no_spread else Spreads()
    rows = []
    for mode in ("hold", "trail"):
        for src in SOURCES:
            all_tr, per_day, conc = [], [], []
            for d in days:
                c = candidates(d, noms[d].get(src, {}), bars)
                tr = run_book(d, c, bars, mode, args.hold, spreads)
                all_tr += tr
                nets = [x["net"] for x in tr if x["net"] is not None]
                per_day.append((d, len(tr), statistics.mean(nets) * 1e4 if nets else None))
                conc.append(concurrency(tr, d))
            if spreads:
                spreads.save()
            g = [x["gross"] * 1e4 for x in all_tr]
            n = [x["net"] * 1e4 for x in all_tr if x["net"] is not None]
            cst = [x["cost"] * 1e4 for x in all_tr if x["cost"] is not None]
            gm, gt = stats(g)
            nm, nt = stats(n)
            half = len(days) // 2
            h1 = [x["net"] * 1e4 for x in all_tr if x["net"] is not None and x["day"] in days[:half]]
            h2 = [x["net"] * 1e4 for x in all_tr if x["net"] is not None and x["day"] in days[half:]]
            tight = [x["net"] * 1e4 for x in all_tr
                     if x["net"] is not None and x["se"] is not None and x["se"] <= 0.20]
            rows.append({
                "exit": mode if mode == "trail" else f"hold{args.hold:g}m", "source": src,
                "names_per_day": statistics.mean(len(noms[d].get(src, {})) for d in days),
                "trades": len(all_tr), "opens_per_10m": len(all_tr) / (len(days) * 38),
                "avg_open": statistics.mean(c_[0] for c_ in conc),
                "ge1": statistics.mean(c_[1] for c_ in conc), "ge2": statistics.mean(c_[2] for c_ in conc),
                "win": sum(1 for x in n if x > 0) / len(n) if n else None,
                "gross_bp": gm, "gross_t": gt, "cost_bp": statistics.mean(cst) if cst else None,
                "net_bp": nm, "net_t": nt,
                "net_h1": statistics.mean(h1) if h1 else None, "net_h2": statistics.mean(h2) if h2 else None,
                "net_spread_gated": statistics.mean(tight) if tight else None, "n_gated": len(tight),
                "per_day": per_day})
    fmt = lambda v, f: "-" if v is None else format(v, f)  # noqa: E731
    print(f"\nnominations: {args.noms}; days {days[0]}..{days[-1]} ({len(days)}); "
          f"fill at the cross minute's close; real SIP spread")
    print(f"{'exit':9} {'source':9} {'nm/d':>5} {'trades':>6} {'/10m':>5} {'avg':>4} {'>=1':>4} {'>=2':>4} "
          f"{'win':>4} {'gross':>6} {'cost':>5} {'net':>6} {'t':>5} {'h1':>6} {'h2':>6} {'net<=.2%':>9}")
    for r in rows:
        print(f"{r['exit']:9} {r['source']:9} {r['names_per_day']:5.0f} {r['trades']:6d} "
              f"{r['opens_per_10m']:5.2f} {r['avg_open']:4.1f} {r['ge1']:4.0%} {r['ge2']:4.0%} "
              f"{fmt(r['win'], '4.0%')} {fmt(r['gross_bp'], '+6.1f')} {fmt(r['cost_bp'], '5.1f')} "
              f"{fmt(r['net_bp'], '+6.1f')} {fmt(r['net_t'], '+5.1f')} {fmt(r['net_h1'], '+6.1f')} "
              f"{fmt(r['net_h2'], '+6.1f')} {fmt(r['net_spread_gated'], '+6.1f')} ({r['n_gated']})")
    out = os.path.join(ROOT, "ai_reports", f"source_optimal_study_{args.noms}.json")
    with open(out, "w") as f:
        json.dump(rows, f, indent=1, default=str)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
