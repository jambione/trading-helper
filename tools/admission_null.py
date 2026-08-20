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
  OUTSIDE  liquid names the desk never watched, same instant, matched on
           price. This is the one that bounds the SCANNER. WITHIN and ACROSS
           are both drawn from names the desk chose, so they can only grade
           decisions made inside the watchlist — if the scanner selects names
           with no exploitable structure, both controls stay silent about it
           and every downstream result is bounded by something nobody
           measured.

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
import bars  # noqa: E402

# How close another symbol's bar must sit to count as "the same instant".
ACROSS_TOL_SEC = 90.0
# Random WITHIN draws per admission, taken from RTH bars of the same day.
WITHIN_DRAWS = 8
RTH_START_MIN = 9 * 60 + 35
RTH_END_MIN = 15 * 60 + 30
# OUTSIDE universe: random names per session, then filtered to ones the desk
# could actually have traded. A control made of illiquid tickers would be a
# comparison against something untradeable, which proves nothing either way.
OUTSIDE_POOL = 150
OUTSIDE_MIN_BARS = 200        # roughly half an RTH session of 1m prints
OUTSIDE_MIN_PRICE = 5.0       # matches ai_watch_min_price
# Price band for matching, as a multiple of the admitted name's price. A $6
# name and a $300 name do not have the same percent-move distribution.
OUTSIDE_BAND = (0.5, 2.0)
UNIVERSE = os.path.join(ROOT, "valid_tickers.txt")


def _day(ts) -> str:  # noqa: D401
    return datetime.fromtimestamp(
        float(ts), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def _et_minutes(ts: float) -> int:
    dt = datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET)
    return dt.hour * 60 + dt.minute


def load_universe() -> list[str]:
    try:
        with open(UNIVERSE, encoding="utf-8") as fh:
            return [ln.strip().upper() for ln in fh if ln.strip()]
    except OSError:
        return []


def build_outside_pool(day: str, watched: set[str], rng, feed: str) -> dict:
    """symbol -> (stamps, closes) for tradeable names the desk never watched.

    Filtered to names that actually printed through the session and sit above
    the desk's own price floor, so the control is a set of trades that could
    have been taken rather than a set of tickers.
    """
    universe = [s for s in load_universe() if s not in watched]
    if not universe:
        return {}
    pick = rng.sample(universe, min(OUTSIDE_POOL, len(universe)))
    bars.fetch_many(pick, day, feed)
    pool = {}
    for s in pick:
        stamps, closes = bars.fetch(s, day, feed)
        if not stamps or len(stamps) < OUTSIDE_MIN_BARS:
            continue
        if closes[0] < OUTSIDE_MIN_PRICE:
            continue
        pool[s] = (stamps, closes)
    return pool


def _index_at(stamps, t0: float) -> int:
    """Index of the bar at or just before t0, or -1."""
    import bisect
    return bisect.bisect_right(stamps, t0) - 1


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

    # One OUTSIDE pool per session, built before the loop so the batch fetch
    # happens once per day rather than once per admission.
    pools = {}
    for day in days:
        watched = {s for _ts, s in watched_at.get(day, ())}
        pools[day] = build_outside_pool(day, watched, rng, args.feed)
        print(f"  {day}: {len(pools[day])} tradeable outside names "
              f"(of {OUTSIDE_POOL} drawn)")

    admitted, within_pairs, across_pairs, outside_pairs = [], [], [], []
    no_bars = 0

    for t0, sym, day in admissions:
        stamps, closes = bars.fetch(sym, day, args.feed)
        if not stamps:
            no_bars += 1
            continue
        a = bars.forward_return(stamps, closes, t0, horizon)
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
                v = bars.forward_return(stamps, closes, s, horizon)
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
            o_st, o_cl = bars.fetch(other, day, args.feed)
            if not o_st:
                continue
            v = bars.forward_return(o_st, o_cl, t0, horizon)
            if v is not None:
                peers.append(v)
        if len(peers) >= 3:
            across_pairs.append((a, statistics.median(peers)))

        # OUTSIDE — names the desk never watched, same instant, price-matched.
        # Price matching matters: a $6 name and a $300 name do not share a
        # percent-move distribution, and the watchlist skews cheap.
        p0 = closes[max(0, min(len(closes) - 1,
                               _index_at(stamps, t0)))] if closes else None
        others = []
        for o_st, o_cl in pools.get(day, {}).values():
            i = _index_at(o_st, t0)
            if i < 0:
                continue
            if p0:
                ratio = o_cl[i] / p0
                if not (OUTSIDE_BAND[0] <= ratio <= OUTSIDE_BAND[1]):
                    continue
            v = bars.forward_return(o_st, o_cl, t0, horizon)
            if v is not None:
                others.append(v)
        if len(others) >= 3:
            outside_pairs.append((a, statistics.median(others)))

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
    print(_stat("OUTSIDE never watched, same moment",
                [o for _a, o in outside_pairs]))
    print()
    print(_paired("admitted - outside (paired)",
                  [a - o for a, o in outside_pairs]))
    print(_paired("admitted - within  (paired)",
                  [a - w for a, w in within_pairs]))
    print(_paired("admitted - across  (paired)",
                  [a - c for a, c in across_pairs]))

    print("\nThe paired rows are the whole point.")
    print("  outside  bounds the SCANNER — negative means the desk would have")
    print("           done as well or better on names it never looked at, and")
    print("           nothing downstream of selection can recover that.")
    print("  within   bounds the TIMING — negative means the moment was worse")
    print("           than an arbitrary one in the same name.")
    print("These are volatile small caps: the mean is outlier-driven, so read")
    print("the median and the beat rate, and read the sigma before believing")
    print("either. Outside names are price-matched but not matched on volume,")
    print("volatility or news, so a negative outside row is a reason to test")
    print("the scanner properly, not a finished verdict on it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
