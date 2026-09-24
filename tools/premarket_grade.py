#!/usr/bin/env python3
"""premarket_grade.py — were the premarket scan's picks worth seating?

Reads ai_reports/premarket_scan/<day>.jsonl (observe mode). For each picked
symbol, from SIP bars once the day is 15+ minutes old:
  gap        official 09:30 open vs the prior close, %
  first_hr   09:30 open -> 10:30 close, %
  day        09:30 open -> 16:00 close, %
  hi         best price after the open vs the open, %
plus whether the book admitted it (admit_funnel kept_symbols, first time)
and whether the desk traded it (outcomes P/L). The comparison row is every
name the book admitted that day that the scan did NOT pick.

The decision it informs: turn ai_movers_premarket_feed on only if the picks
move at least as well as what the book finds by itself, and earlier.

USAGE (on the mini, after 16:16 ET)
    .venv/bin/python tools/premarket_grade.py --day 2026-09-24
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402


def picks(day: str) -> dict:
    """{sym: {"t": first pick ts, "pm_gap": gap at that pick}}."""
    path = os.path.join(ROOT, "ai_reports", "premarket_scan", f"{day}.jsonl")
    out: dict = {}
    if not os.path.exists(path):
        return out
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        for p in r.get("picks") or []:
            s = p.get("symbol")
            if s and s not in out:
                out[s] = {"t": r.get("ts"), "pm_gap": p.get("pm_gap")}
    return out


def admitted(day: str) -> dict:
    first: dict = {}
    t0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET).timestamp()
    for line in open(os.path.join(ROOT, "ai_reports", "events.jsonl")):
        if '"admit_funnel"' not in line:
            continue
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        ts = float(e.get("ts") or 0)
        if ts < t0 or bars.day_of(ts) != day:
            continue
        for s in e.get("kept_symbols") or []:
            first.setdefault(str(s), ts)
    return first


def traded(day: str) -> dict:
    out: dict = {}
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        et = r.get("entry_time")
        if isinstance(et, (int, float)) and bars.day_of(et) == day:
            s = r.get("symbol")
            n, pl = out.get(s, (0, 0.0))
            out[s] = (n + 1, pl + float(r.get("realized_pl_usd") or 0))
    return out


def moves(syms: list[str], day: str) -> dict:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    cl = bars.client()
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET)
    out: dict = {}
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        m1 = cl.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=chunk, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=d0.replace(hour=9, minute=30).astimezone(timezone.utc),
            end=d0.replace(hour=16, minute=0).astimezone(timezone.utc),
            feed=DataFeed.SIP, limit=1_000_000)).data
        db = cl.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
            start=(d0 - timedelta(days=7)).astimezone(timezone.utc),
            end=(d0 - timedelta(minutes=1)).astimezone(timezone.utc),
            feed=DataFeed.SIP)).data
        for s in chunk:
            mb = m1.get(s) or []
            prior = [b for b in (db.get(s) or [])
                     if b.timestamp.astimezone(bars.ET).date() < d0.date()]
            if len(mb) < 60 or not prior:
                continue
            op, pc = float(mb[0].open), float(prior[-1].close)
            hr = [b for b in mb if b.timestamp.astimezone(bars.ET).hour * 60
                  + b.timestamp.astimezone(bars.ET).minute < 10 * 60 + 30]
            out[s] = {"open": op, "gap": (op / pc - 1) * 100,
                      "first_hr": (float(hr[-1].close) / op - 1) * 100 if hr else None,
                      "day": (float(mb[-1].close) / op - 1) * 100,
                      "hi": (max(float(b.high) for b in mb) / op - 1) * 100}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now(bars.ET).strftime("%Y-%m-%d"))
    ap.add_argument("--min-price", type=float, default=20.0)
    args = ap.parse_args()
    pk = picks(args.day)
    if not pk:
        print(f"PREMARKET GRADE {args.day}: no picks logged "
              f"(ai_reports/premarket_scan/{args.day}.jsonl missing or empty)")
        return
    adm = admitted(args.day)
    tr = traded(args.day)
    book_only = sorted(s for s in adm if s not in pk)
    mv = moves(sorted(set(pk) | set(book_only)), args.day)

    def fmt(x):
        return "—" if x is None else f"{x:+.2f}%"

    print(f"PREMARKET GRADE {args.day}: {len(pk)} picks, book admitted {len(adm)} names "
          f"({len(set(pk) & set(adm))} of them picked premarket)\n")
    print(f"  {'sym':<6}{'picked':>7}{'pm gap':>8}{'gap@open':>10}{'09:30-10:30':>12}"
          f"{'open-close':>11}{'best':>8}  {'admitted':<9}{'trades / P&L'}")
    for s in sorted(pk, key=lambda s: pk[s]["t"] or 0):
        m = mv.get(s, {})
        pt = datetime.fromtimestamp(pk[s]["t"], bars.ET).strftime("%H:%M") if pk[s]["t"] else "?"
        at = (datetime.fromtimestamp(adm[s], bars.ET).strftime("%H:%M") if s in adm else "no")
        n, pl = tr.get(s, (0, 0.0))
        print(f"  {s:<6}{pt:>7}{fmt(pk[s]['pm_gap']):>8}{fmt(m.get('gap')):>10}"
              f"{fmt(m.get('first_hr')):>12}{fmt(m.get('day')):>11}{fmt(m.get('hi')):>8}  "
              f"{at:<9}{(f'{n} / ${pl:+.2f}' if n else '-')}")

    def avg(group, k):
        # Medians: one small-cap runner (ARTL +200%) swamps a mean.
        xs = [mv[s][k] for s in group if s in mv and mv[s].get(k) is not None]
        return (statistics.median(xs), len(xs)) if xs else (None, 0)

    # Same price floor on both sides, or small caps swamp the comparison.
    floor = args.min_price
    book_cmp = [s for s in book_only if s in mv and mv[s]["open"] >= floor]
    print(f"\n  medians; comparison group limited to opens >= ${floor:g}")
    print(f"  {'group':<34}{'n':>4}{'09:30-10:30':>13}{'open-close':>12}{'best':>9}")
    for lab, grp in (("premarket picks", list(pk)),
                     (f"book names NOT picked (>= ${floor:g})", book_cmp)):
        a, n = avg(grp, "first_hr")
        b, _ = avg(grp, "day")
        c, _ = avg(grp, "hi")
        print(f"  {lab:<34}{n:>4}{fmt(a):>13}{fmt(b):>12}{fmt(c):>9}")


if __name__ == "__main__":
    main()
