#!/usr/bin/env python3
"""Is the local trail banking gains or cutting trades before they breathe?

45 of 49 closed trades on 2026-08-20 exited on local_trail at a median -0.02R
after a median 85 seconds. That is either a stop doing its job on trades that
never worked, or a trail so tight it exits on noise — and the two look
identical in the exit log. The difference only shows up in what the stock did
AFTER the exit.

For each trail exit this measures:
  * how far the trade ran in its favour first (MFE, from bars)
  * where the stock was 5 / 15 / 30 minutes after the exit
  * how much of the exit was give-back from the high-water mark

A trail that is working leaves trades that go nowhere afterwards. A trail that
is too tight leaves a systematic positive drift after the exit — money that
was on the table.

Read-only. Usage:
    python3 tools/trail_exits.py [--days N] [--reason local_trail]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ET = ZoneInfo("America/New_York")
OUTCOMES = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")


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


def _pct(vals):
    if not vals:
        return "n=0"
    vals = sorted(vals)
    return (f"median {statistics.median(vals):+.2f}%  "
            f"mean {statistics.fmean(vals):+.2f}%  "
            f"up {100.0 * sum(1 for v in vals if v > 0) / len(vals):.0f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--reason", default="local_trail")
    args = ap.parse_args()

    rows = []
    for line in open(OUTCOMES, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("exit_time") and r.get("entry_time") and r.get("exit_price"):
            rows.append(r)
    by_day = defaultdict(list)
    for r in rows:
        d = datetime.fromtimestamp(
            r["exit_time"], timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
        by_day[d].append(r)
    sel = []
    for d in sorted(by_day)[-args.days:]:
        sel.extend(by_day[d])
    sel = [r for r in sel if str(r.get("close_reason")) == args.reason]
    if not sel:
        print(f"no {args.reason} exits found")
        return 1
    print(f"{len(sel)} {args.reason} exits\n")

    sec = json.load(open(os.path.join(ROOT, "config", "secrets.json")))
    import alpaca_api as aa
    client = aa.connect_data_client(
        {"api_key": sec["api_key"], "secret_key": sec["secret_key"]})

    cache = {}
    fwd = {5: [], 15: [], 30: []}
    run_up, give_back, holds, rs = [], [], [], []

    for r in sel:
        sym = str(r["symbol"]).upper()
        day = datetime.fromtimestamp(
            r["exit_time"], timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
        if (sym, day) not in cache:
            cache[(sym, day)] = _fetch_day_bars(client, sym, day)
        df = cache[(sym, day)]
        if df is None:
            continue
        stamps = [t.timestamp() for t in df.index]
        highs = [float(v) for v in df["high"]]
        closes = [float(v) for v in df["close"]]

        entry, exit_px = float(r["entry_price"]), float(r["exit_price"])
        t_in, t_out = float(r["entry_time"]), float(r["exit_time"])
        holds.append(float(r.get("hold_sec") or 0))
        if r.get("realized_r_multiple") is not None:
            rs.append(float(r["realized_r_multiple"]))

        # High-water mark while the trade was open.
        hi = [highs[k] for k, s in enumerate(stamps) if t_in <= s <= t_out]
        if hi:
            peak = max(hi)
            run_up.append((peak - entry) / entry * 100.0)
            give_back.append((peak - exit_px) / entry * 100.0)

        # Where it went after the exit.
        for mins in (5, 15, 30):
            tgt = t_out + mins * 60
            after = [closes[k] for k, s in enumerate(stamps)
                     if t_out < s <= tgt]
            if after and (stamps[-1] >= tgt - 90):
                fwd[mins].append((after[-1] - exit_px) / exit_px * 100.0)

    print(f"hold: median {statistics.median(holds):.0f}s   "
          f"realized R: median {statistics.median(rs):+.3f}")
    print(f"run-up before exit (peak vs entry):  {_pct(run_up)}")
    print(f"give-back from peak to exit:         {_pct(give_back)}")
    print("\nwhere the stock went AFTER the exit:")
    for mins in (5, 15, 30):
        print(f"  +{mins:>2}m   {_pct(fwd[mins])}")
    print("\nA trail doing its job leaves flat-to-negative drift afterwards.")
    print("Systematic positive drift is money the trail left on the table.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
