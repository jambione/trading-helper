#!/usr/bin/env python3
"""Would the CM RSI-2 entry filter have improved the arms the desk took?

The operator's rule is "anything trending up from 0 to 50 is a good entry,
never trending down". This replays it against arms that actually happened and
compares what it would have KEPT against what it would have BLOCKED.

Two things make this honest rather than decorative:

  * The RSI is RECOMPUTED from 1-minute bars, not read from the shadow log.
    The logged cm_rsi / cm_rsi_rising were drawn from two different frames
    until 2026-08-20 and contradicted each other on ~42% of comparisons
    (tools/rsi_trust.py), so replaying the rule against them would measure the
    bug, not the rule.

  * The outcome is measured from the same bars, mark-to-market at a fixed
    horizon, so it does not depend on how often the shadow logger happened to
    sample a name.

What it CANNOT tell you: the filter only ever removes arms, so this measures
whether the removed ones were worse than the kept ones. It says nothing about
entries the desk never took, and it models no stop, target, trail or fill —
a blocked arm that would have stopped out at -1R shows here as its
mark-to-market return, which flatters nothing but matches neither.

Read-only. Usage:
    python3 tools/rsi_counterfactual.py [--days N] [--horizon-min N]
                                        [--band-max 50] [--band-min 0]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ET = ZoneInfo("America/New_York")
SHADOW = os.path.join(ROOT, "ai_reports", "shadow.jsonl")

# strategy_three_indicator: RSI-2 with Wilder smoothing, "rising" judged over
# trend_lookback bars. Mirrored here so the replay matches what the engine
# publishes rather than inventing a second definition.
RSI_LENGTH = 2
TREND_LOOKBACK = 2


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
            if r.get("arm_ok") is True and r.get("ts") and r.get("symbol"):
                rows.append(r)
    if not rows:
        return []
    by_day = defaultdict(list)
    for r in rows:
        d = datetime.fromtimestamp(
            float(r["ts"]), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
        by_day[d].append(r)
    keep = sorted(by_day)[-days:]
    out = []
    for d in keep:
        out.extend(by_day[d])
    return out


def _wilder_rsi(closes: list[float], period: int) -> list[float | None]:
    """RSI series aligned to *closes*; None where it cannot form yet."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    alpha = 1.0 / period
    up = down = 0.0
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        if i == 1:
            up, down = gain, loss
        else:
            up = alpha * gain + (1.0 - alpha) * up
            down = alpha * loss + (1.0 - alpha) * down
        if down == 0:
            out[i] = 100.0
        elif up == 0:
            out[i] = 0.0
        else:
            out[i] = 100.0 - (100.0 / (1.0 + up / down))
    return out


def _fetch_day_bars(client, sym: str, day: str):
    """1-minute bars for one symbol-day, ET session plus a pre-market pad."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed

    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    start = d.replace(hour=8, minute=0)
    end = d.replace(hour=16, minute=30)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=start.astimezone(timezone.utc),
            end=end.astimezone(timezone.utc),
            limit=10000,
            extended_hours=True,
            feed=DataFeed.IEX,
        )
        df = client.get_stock_bars(req).df
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
    ap.add_argument("--days", type=int, default=3,
                    help="most recent N days of arms to replay")
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--band-max", type=float, default=50.0)
    ap.add_argument("--band-min", type=float, default=0.0)
    ap.add_argument("--price-min", type=float, default=None,
                    help="only replay arms at or above this price")
    ap.add_argument("--price-max", type=float, default=None,
                    help="only replay arms below this price. With --price-min, "
                         "answers whether the filter works on one price cohort "
                         "— e.g. the sub-$20 names ai_watch_min_price excludes.")
    args = ap.parse_args()

    arms = _load_arms(args.days)
    if args.price_min is not None or args.price_max is not None:
        lo = args.price_min if args.price_min is not None else 0.0
        hi = args.price_max if args.price_max is not None else float("inf")
        arms = [r for r in arms
                if r.get("price") is not None and lo <= float(r["price"]) < hi]
        print(f"price cohort: {lo:g} <= price < {hi:g}")
    if not arms:
        print("no arms found in shadow.jsonl")
        return 1

    days = sorted({datetime.fromtimestamp(float(r["ts"]), timezone.utc)
                   .astimezone(ET).strftime("%Y-%m-%d") for r in arms})
    print(f"replaying {len(arms)} arms across {days[0]}..{days[-1]}")

    sec = json.load(open(os.path.join(ROOT, "config", "secrets.json")))
    import alpaca_api as aa
    client = aa.connect_data_client(
        {"api_key": sec["api_key"], "secret_key": sec["secret_key"]})

    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in arms:
        d = datetime.fromtimestamp(
            float(r["ts"]), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
        by_key[(str(r["symbol"]).upper(), d)].append(r)

    kept: list[float] = []
    blocked_ext: list[float] = []
    blocked_falling: list[float] = []
    no_bars = no_rsi = no_forward = 0
    horizon = timedelta(minutes=args.horizon_min)

    for (sym, day), rows in sorted(by_key.items()):
        df = _fetch_day_bars(client, sym, day)
        if df is None or len(df) < RSI_LENGTH + TREND_LOOKBACK + 2:
            no_bars += len(rows)
            continue
        stamps = [t.timestamp() for t in df.index]
        closes = [float(c) for c in df["close"]]
        rsi = _wilder_rsi(closes, RSI_LENGTH)

        for r in rows:
            ts = float(r["ts"])
            # Last completed bar at or before the arm.
            i = -1
            for k, s in enumerate(stamps):
                if s <= ts:
                    i = k
                else:
                    break
            if i < TREND_LOOKBACK or rsi[i] is None or rsi[i - TREND_LOOKBACK] is None:
                no_rsi += 1
                continue
            level = float(rsi[i])
            rising = level > float(rsi[i - TREND_LOOKBACK])

            # Mark-to-market at the horizon, from the same bars.
            target = ts + horizon.total_seconds()
            j = None
            for k in range(i + 1, len(stamps)):
                if stamps[k] <= target:
                    j = k
                else:
                    break
            if j is None or stamps[j] - stamps[i] < horizon.total_seconds() * 0.5:
                no_forward += 1
                continue
            fwd = (closes[j] - closes[i]) / closes[i] * 100.0

            if level > args.band_max:
                blocked_ext.append(fwd)
            elif level < args.band_min:
                blocked_ext.append(fwd)
            elif not rising:
                blocked_falling.append(fwd)
            else:
                kept.append(fwd)

    def _stat(name: str, vals: list[float]) -> None:
        if not vals:
            print(f"  {name:<28} n=0")
            return
        wins = sum(1 for v in vals if v > 0)
        print(f"  {name:<28} n={len(vals):<5} "
              f"mean={statistics.fmean(vals):+.3f}%  "
              f"median={statistics.median(vals):+.3f}%  "
              f"win={100.0 * wins / len(vals):.1f}%")

    allv = kept + blocked_ext + blocked_falling
    print(f"\nhorizon {args.horizon_min:.0f}m, band {args.band_min:g}-"
          f"{args.band_max:g} and rising")
    print(f"skipped: {no_bars} no bars, {no_rsi} no RSI, "
          f"{no_forward} no forward window\n")
    _stat("ALL ARMS (baseline)", allv)
    _stat("WOULD KEEP (in band, rising)", kept)
    _stat("WOULD BLOCK — extended", blocked_ext)
    _stat("WOULD BLOCK — not rising", blocked_falling)
    _stat("WOULD BLOCK (both)", blocked_ext + blocked_falling)

    if kept and allv:
        lift = statistics.fmean(kept) - statistics.fmean(allv)
        print(f"\n  lift of the filter vs taking every arm: {lift:+.3f}% "
              f"per trade at {args.horizon_min:.0f}m")
        print(f"  arms retained: {len(kept)}/{len(allv)} "
              f"({100.0 * len(kept) / len(allv):.0f}%)")
    print("\nMark-to-market only — no stop, target, trail or fill modelled, and")
    print("the filter can only REMOVE arms, never add ones the desk skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
