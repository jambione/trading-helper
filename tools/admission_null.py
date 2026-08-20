#!/usr/bin/env python3
"""Does being admitted mean anything, or is it just which names we watched?

Every measurement the desk makes is the desk against itself. The gate
scorecard weighs admits against rejects, trail_exits weighs exits against what
came next, slot_contention weighs skipped against held. None asks whether the
admission decision beats *no decision*, and desk_report prints the caveat under
every run: "None of this is randomized — the desk chose what it watched."

So "admitted names returned +0.63% over 30m" is not yet a result. If the same
names returned as much from an arbitrary moment that session, the gating stack
is selecting the day's movers and taking credit for their movement.

Two controls, both matched to the admission by construction:

  WITHIN   the same symbol, same session, a different instant. Separates
           picking a MOMENT from picking a NAME.
  ACROSS   the other names on the watchlist at the same instant. Catches the
           whole list moving together.

Forward returns come from 1-minute bars, deliberately NOT from the shadow
series. Scoring off shadow samples — which is what desk_report and
shadow_report.forward_return do — can only measure an admission whose episode
survived half the horizon, and how long the desk keeps watching a name is
decided after the admission. On 2026-08-14..20 the median episode ran 383
seconds against a 30-minute horizon, so that rule scored 168 of 750
admissions and kept the longest fifth. Bars do not care how long the desk
watched.

Read-only. Usage:
    python3 tools/admission_null.py [--days N] [--horizon-min N] [--feed sip]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
ET = ZoneInfo("America/New_York")

import shadow_report as sr  # noqa: E402

# How close another symbol's bar must sit to count as "the same instant".
ACROSS_TOL_SEC = 90.0
# Random WITHIN draws per admission, taken from RTH bars of the same day.
WITHIN_DRAWS = 8
RTH_START_MIN = 9 * 60 + 35
RTH_END_MIN = 15 * 60 + 30


def _day(ts) -> str:
    return datetime.fromtimestamp(
        float(ts), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def _et_minutes(ts: float) -> int:
    dt = datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET)
    return dt.hour * 60 + dt.minute


def _client():
    sec = json.load(open(os.path.join(ROOT, "config", "secrets.json")))
    import alpaca_api as aa
    return aa.connect_data_client(
        {"api_key": sec["api_key"], "secret_key": sec["secret_key"]})


def _fetch(client, sym: str, day: str, feed: str, cache: dict):
    """(stamps, closes) of 1m RTH bars for sym/day, or (None, None)."""
    key = (sym, day)
    if key in cache:
        return cache[key]
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed

    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    out = (None, None)
    try:
        df = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=d.replace(hour=9, minute=25).astimezone(timezone.utc),
            end=d.replace(hour=16, minute=5).astimezone(timezone.utc),
            limit=10000, extended_hours=False,
            feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
        )).df
        import pandas as pd
        if df is not None and not df.empty:
            if isinstance(df.index, pd.MultiIndex):
                df = df.xs(sym, level="symbol")
            df = df.sort_index()
            out = ([t.timestamp() for t in df.index],
                   [float(v) for v in df["close"]])
    except Exception:
        out = (None, None)
    cache[key] = out
    return out


def _fwd(stamps, closes, t0: float, horizon: float) -> float | None:
    """Pct change from the bar at/just before t0 to the bar horizon later."""
    if not stamps:
        return None
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
    p0 = closes[i]
    return None if not p0 else (closes[j] - p0) / p0 * 100.0


def _stat(label: str, vals: list[float]) -> str:
    if not vals:
        return f"  {label:<34} n=0"
    up = 100.0 * sum(1 for v in vals if v > 0) / len(vals)
    return (f"  {label:<34} n={len(vals):<5} "
            f"median {statistics.median(vals):+.3f}%  "
            f"mean {statistics.fmean(vals):+.3f}%  up {up:.0f}%")


def _paired(label: str, diffs: list[float]) -> str:
    if not diffs:
        return f"  {label:<34} n=0"
    n = len(diffs)
    win = 100.0 * sum(1 for v in diffs if v > 0) / n
    se = 50.0 / (n ** 0.5)
    sigma = abs(win - 50.0) / se if se else 0.0
    return (f"  {label:<34} n={n:<5} "
            f"median {statistics.median(diffs):+.3f}%  "
            f"mean {statistics.fmean(diffs):+.3f}%  "
            f"beat {win:.0f}%  ({sigma:.1f}σ)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--feed", choices=("sip", "iex"), default="sip")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    horizon = args.horizon_min * 60.0
    rng = random.Random(args.seed)

    rows = sr.load()
    if not rows:
        print("no shadow log")
        return 1
    days = sorted({_day(r["ts"]) for r in rows if r.get("ts")})[-args.days:]
    dayset = set(days)
    rows = [r for r in rows if r.get("ts") and _day(r["ts"]) in dayset]
    eps = sr.by_episode(rows)

    # One admission = one episode's first sample. Bars decide the outcome.
    admissions = []
    watched_at = defaultdict(set)      # day -> {(ts, symbol)}
    for (sym, _adm), series in eps.items():
        if not series:
            continue
        t0 = float(series[0].get("ts") or 0)
        if not t0 or not (RTH_START_MIN <= _et_minutes(t0) <= RTH_END_MIN):
            continue
        admissions.append((t0, str(sym).upper(), _day(t0)))
        for r in series:
            ts = float(r.get("ts") or 0)
            if ts:
                watched_at[_day(ts)].add((ts, str(r.get("symbol") or "").upper()))
    print(f"{len(admissions)} admissions in RTH across {days[0]}..{days[-1]}")

    client = _client()
    cache: dict = {}
    admitted, within_pairs, across_pairs = [], [], []
    no_bars = 0

    for t0, sym, day in admissions:
        stamps, closes = _fetch(client, sym, day, args.feed, cache)
        if not stamps:
            no_bars += 1
            continue
        a = _fwd(stamps, closes, t0, horizon)
        if a is None:
            continue
        admitted.append(a)

        # WITHIN — same symbol and day, random RTH instants.
        pool = [s for s in stamps
                if RTH_START_MIN <= _et_minutes(s) <= RTH_END_MIN
                and abs(s - t0) > horizon]
        draws = []
        if pool:
            for s in rng.sample(pool, min(WITHIN_DRAWS, len(pool))):
                v = _fwd(stamps, closes, s, horizon)
                if v is not None:
                    draws.append(v)
        if draws:
            within_pairs.append((a, statistics.median(draws)))

        # ACROSS — other names on the watchlist at the same instant.
        peers = []
        seen = set()
        for ts, other in watched_at.get(day, ()):
            if other == sym or other in seen or abs(ts - t0) > ACROSS_TOL_SEC:
                continue
            seen.add(other)
            o_st, o_cl = _fetch(client, other, day, args.feed, cache)
            if not o_st:
                continue
            v = _fwd(o_st, o_cl, t0, horizon)
            if v is not None:
                peers.append(v)
        if len(peers) >= 3:
            across_pairs.append((a, statistics.median(peers)))

    if not admitted:
        print("no admission scored — nothing to say")
        return 1

    print(f"scored {len(admitted)} ({no_bars} had no bars)")
    print(f"\nforward {args.horizon_min:g}m, {args.feed.upper()} 1m bars:\n")
    print(_stat("ADMITTED (at the decision)", admitted))
    print(_stat("WITHIN  same name, other moment",
                [w for _a, w in within_pairs]))
    print(_stat("ACROSS  other names, same moment",
                [c for _a, c in across_pairs]))
    print()
    print(_paired("admitted - within  (paired)",
                  [a - w for a, w in within_pairs]))
    print(_paired("admitted - across  (paired)",
                  [a - c for a, c in across_pairs]))

    print("\nThe paired rows are the whole point. A stack that picks MOMENTS")
    print("earns a positive 'admitted - within'; one that only picks NAMES")
    print("does not, however good the ADMITTED row looks alone. These are")
    print("volatile small caps — the mean is outlier-driven, so read the")
    print("median and the beat rate, and read the sigma before believing it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
