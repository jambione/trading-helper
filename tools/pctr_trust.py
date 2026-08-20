#!/usr/bin/env python3
"""Is the %R the desk gates on the %R a chart would show?

live_exhaustion computes Williams %R from symbol_ohlc — Alpaca REST 1-minute
bars — filtered to a 25-minute wall-clock window. On a thin name IEX does not
print every minute, so that window can hold 11 bars instead of 21, and the
reading stops being a %R(21) at all: it becomes position-in-range over
whatever arrived, and the live price often IS the extreme, pinning it to -0.0
or -100.

This replays each arm against a %R(21) recomputed from the full 1-minute bar
series, using the arm's own price as the live close — the same construction
live_exhaustion intends, without the sparse-window filter. Then it asks the
question that matters: how often did the gating value pass the heat floor when
the honest one would have failed it, or the reverse, and what did those trades
do afterwards.

Read-only. Usage:
    python3 tools/pctr_trust.py [--days N] [--horizon-min N] [--heat-min 40]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ET = ZoneInfo("America/New_York")
SHADOW = os.path.join(ROOT, "ai_reports", "shadow.jsonl")

FAST_LENGTH = 21          # rte_fast_length
MATERIAL = 10.0           # EXH points; below this the two agree for our purpose


def _load_arms(days: int) -> list[dict]:
    rows = []
    with open(SHADOW, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("ts") and r.get("symbol") and r.get("price"):
                rows.append(r)
    by_day = defaultdict(list)
    for r in rows:
        d = datetime.fromtimestamp(
            float(r["ts"]), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
        by_day[d].append(r)
    out = []
    for d in sorted(by_day)[-days:]:
        out.extend(by_day[d])
    return out


def _fetch_day_bars(client, sym: str, day: str):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed

    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    try:
        df = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=d.replace(hour=8, minute=0).astimezone(timezone.utc),
            end=d.replace(hour=16, minute=30).astimezone(timezone.utc),
            limit=10000, extended_hours=True, feed=DataFeed.IEX,
        )).df
    except Exception:
        return None
    if df is None or df.empty:
        return None
    try:
        import pandas as pd
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(sym, level="symbol")
        return df.sort_index()
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--horizon-min", type=float, default=15.0)
    ap.add_argument("--heat-min", type=float, default=40.0)
    ap.add_argument("--arms-only", action="store_true",
                    help="replay only rows the desk actually armed")
    args = ap.parse_args()

    rows = _load_arms(args.days)
    if args.arms_only:
        rows = [r for r in rows if r.get("arm_ok") is True]
    rows = [r for r in rows if r.get("exhaustion") is not None]
    if not rows:
        print("nothing to replay")
        return 1

    days = sorted({datetime.fromtimestamp(float(r["ts"]), timezone.utc)
                   .astimezone(ET).strftime("%Y-%m-%d") for r in rows})
    print(f"replaying {len(rows)} readings across {days[0]}..{days[-1]}")

    sec = json.load(open(os.path.join(ROOT, "config", "secrets.json")))
    import alpaca_api as aa
    client = aa.connect_data_client(
        {"api_key": sec["api_key"], "secret_key": sec["secret_key"]})

    by_key = defaultdict(list)
    for r in rows:
        d = datetime.fromtimestamp(
            float(r["ts"]), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
        by_key[(str(r["symbol"]).upper(), d)].append(r)

    diffs: list[float] = []
    by_src: dict[str, list[float]] = defaultdict(list)
    false_pass: list[float] = []   # local passed heat, honest failed
    false_block: list[float] = []  # local failed heat, honest passed
    agreed_pass: list[float] = []
    pinned = 0
    skipped = 0
    horizon = timedelta(minutes=args.horizon_min).total_seconds()

    for (sym, day), rs in sorted(by_key.items()):
        df = _fetch_day_bars(client, sym, day)
        if df is None or len(df) < FAST_LENGTH + 2:
            skipped += len(rs)
            continue
        stamps = [t.timestamp() for t in df.index]
        highs = [float(v) for v in df["high"]]
        lows = [float(v) for v in df["low"]]
        closes = [float(v) for v in df["close"]]

        for r in rs:
            ts = float(r["ts"])
            px = float(r["price"])
            i = -1
            for k, s in enumerate(stamps):
                if s <= ts:
                    i = k
                else:
                    break
            if i < FAST_LENGTH:
                skipped += 1
                continue
            # %R(21) with this print as the live close — live_exhaustion's own
            # construction, minus the sparse-window filter.
            win_h = highs[i - FAST_LENGTH + 2:i + 1] + [px]
            win_l = lows[i - FAST_LENGTH + 2:i + 1] + [px]
            hh, ll = max(win_h), min(win_l)
            if hh - ll <= 0:
                skipped += 1
                continue
            honest_exh = 100.0 + (-100.0 * (hh - px) / (hh - ll))
            local_exh = float(r["exhaustion"])
            d = abs(local_exh - honest_exh)
            diffs.append(d)
            by_src[str(r.get("pctr_src") or "none")].append(d)
            if local_exh in (0.0, 100.0):
                pinned += 1

            # Forward return from the same bars.
            j = None
            for k in range(i + 1, len(stamps)):
                if stamps[k] <= ts + horizon:
                    j = k
                else:
                    break
            if j is None or stamps[j] - stamps[i] < horizon * 0.5:
                continue
            fwd = (closes[j] - closes[i]) / closes[i] * 100.0
            lp = local_exh >= args.heat_min
            hp = honest_exh >= args.heat_min
            if lp and not hp:
                false_pass.append(fwd)
            elif hp and not lp:
                false_block.append(fwd)
            elif lp and hp:
                agreed_pass.append(fwd)

    diffs.sort()
    n = len(diffs)
    print(f"\ncompared {n} readings ({skipped} skipped)\n")
    print(f"|local EXH - honest EXH|:  median {diffs[n // 2]:.1f}  "
          f"p90 {diffs[int(n * 0.9)]:.1f}  max {diffs[-1]:.1f}")
    material = sum(1 for d in diffs if d >= MATERIAL)
    print(f"  {material} of {n} ({100.0 * material / n:.1f}%) differ by "
          f">= {MATERIAL:.0f} EXH points")
    print(f"  local reading pinned at exactly 0 or 100: {pinned} "
          f"({100.0 * pinned / n:.1f}%)")

    print("\nby the source the desk labelled it:")
    for src, ds in sorted(by_src.items(), key=lambda kv: -len(kv[1])):
        ds.sort()
        bad = sum(1 for d in ds if d >= MATERIAL)
        print(f"  {src:<14} n={len(ds):<5} median {ds[len(ds) // 2]:>5.1f}  "
              f">={MATERIAL:.0f} pts: {100.0 * bad / len(ds):>5.1f}%")

    def _stat(name, vals):
        if not vals:
            print(f"  {name:<34} n=0")
            return
        w = sum(1 for v in vals if v > 0)
        print(f"  {name:<34} n={len(vals):<5} mean={statistics.fmean(vals):+.3f}%  "
              f"win={100.0 * w / len(vals):.1f}%")

    print(f"\nheat floor {args.heat_min:g}, forward {args.horizon_min:g}m:")
    _stat("both agree it passes", agreed_pass)
    _stat("local PASSES, honest fails", false_pass)
    _stat("local BLOCKS, honest passes", false_block)
    print("\nThe middle line is trades the desk took because the sparse-window")
    print("reading said heat that a full 21-bar window would not have shown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
