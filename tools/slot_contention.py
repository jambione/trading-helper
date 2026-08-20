#!/usr/bin/env python3
"""When slots were full, did the desk hold the better name?

ai_max_positions is small (2), so the book is decided by whoever arms first —
poll timing and zone geometry, not trade quality. Prioritising slots is only
worth building if the names turned away actually did better than the ones
occupying the slots.

For every watch_skip/max_positions event this compares, over the same forward
window from the moment of the skip:

  * what the SKIPPED name went on to do
  * what the positions actually HELD at that moment went on to do

Both measured from 1-minute bars, so neither depends on how the desk happened
to exit. If the skipped names systematically outrun the held ones there is
something for a ranker to capture; if it is noise, ranking is complexity with
no edge behind it.

Read-only. Usage:
    python3 tools/slot_contention.py [--days N] [--horizon-min N]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ET = ZoneInfo("America/New_York")
EVENTS = os.path.join(ROOT, "ai_reports", "events.jsonl")
OUTCOMES = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")


def _jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def _day(ts):
    return datetime.fromtimestamp(
        float(ts), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def _fetch(client, sym, day, cache):
    if (sym, day) in cache:
        return cache[(sym, day)]
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed

    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    df = None
    try:
        df = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=d.replace(hour=8, minute=0).astimezone(timezone.utc),
            end=d.replace(hour=16, minute=30).astimezone(timezone.utc),
            limit=10000, extended_hours=True, feed=DataFeed.IEX,
        )).df
        import pandas as pd
        if df is not None and not df.empty and isinstance(df.index, pd.MultiIndex):
            df = df.xs(sym, level="symbol")
        df = None if df is None or df.empty else df.sort_index()
    except Exception:
        df = None
    cache[(sym, day)] = df
    return df


def _fwd(df, t0: float, horizon: float):
    """Pct change from the bar at t0 to the last bar within the horizon."""
    if df is None:
        return None
    stamps = [t.timestamp() for t in df.index]
    closes = [float(v) for v in df["close"]]
    i = None
    for k, s in enumerate(stamps):
        if s <= t0:
            i = k
        else:
            break
    if i is None:
        return None
    j = None
    for k in range(i + 1, len(stamps)):
        if stamps[k] <= t0 + horizon:
            j = k
        else:
            break
    if j is None or stamps[j] - stamps[i] < horizon * 0.5:
        return None
    return (closes[j] - closes[i]) / closes[i] * 100.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--horizon-min", type=float, default=15.0)
    args = ap.parse_args()
    horizon = args.horizon_min * 60.0

    events = [r for r in _jsonl(EVENTS) if r.get("ts")]
    outcomes = [r for r in _jsonl(OUTCOMES)
                if r.get("entry_time") and r.get("exit_time")]
    if not events:
        print("no events log")
        return 1
    days = sorted({_day(r["ts"]) for r in events})[-args.days:]
    skips = [r for r in events
             if r.get("kind") == "watch_skip"
             and str(r.get("reason")) == "max_positions"
             and _day(r["ts"]) in days]
    if not skips:
        print("no max_positions skips in range")
        return 1
    print(f"{len(skips)} max_positions skips across {days[0]}..{days[-1]}")

    sec = json.load(open(os.path.join(ROOT, "config", "secrets.json")))
    import alpaca_api as aa
    client = aa.connect_data_client(
        {"api_key": sec["api_key"], "secret_key": sec["secret_key"]})

    cache = {}
    skipped_fwd, held_fwd, paired = [], [], []

    for ev in skips:
        ts = float(ev["ts"])
        day = _day(ts)
        sym = str(ev.get("symbol") or "").upper()
        if not sym:
            continue
        s_fwd = _fwd(_fetch(client, sym, day, cache), ts, horizon)
        if s_fwd is None:
            continue
        # Which names were actually open at that instant.
        open_now = [o for o in outcomes
                    if float(o["entry_time"]) <= ts <= float(o["exit_time"])]
        h_vals = []
        for o in open_now:
            hs = str(o["symbol"]).upper()
            v = _fwd(_fetch(client, hs, _day(ts), cache), ts, horizon)
            if v is not None:
                h_vals.append(v)
        skipped_fwd.append(s_fwd)
        if h_vals:
            h = statistics.fmean(h_vals)
            held_fwd.append(h)
            paired.append(s_fwd - h)

    def _s(name, v):
        if not v:
            print(f"  {name:<34} n=0")
            return
        print(f"  {name:<34} n={len(v):<4} median {statistics.median(v):+.2f}%  "
              f"mean {statistics.fmean(v):+.2f}%  "
              f"up {100.0 * sum(1 for x in v if x > 0) / len(v):.0f}%")

    print(f"\nforward {args.horizon_min:g}m from the moment of the skip:")
    _s("SKIPPED (turned away)", skipped_fwd)
    _s("HELD (occupying the slots)", held_fwd)
    _s("skipped minus held (paired)", paired)
    if paired:
        better = sum(1 for d in paired if d > 0)
        print(f"\n  skipped name beat the held book on {better}/{len(paired)} "
              f"({100.0 * better / len(paired):.0f}%) of contested moments")
        print("  A ranker can only capture what this difference contains.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
