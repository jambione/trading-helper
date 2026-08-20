#!/usr/bin/env python3
"""Does being admitted mean anything, or is it just which names we watched?

Every measurement the desk makes is the desk against itself. The gate
scorecard weighs admits against rejects, trail_exits weighs exits against what
came next, slot_contention weighs skipped against held. None of them asks
whether the admission decision beats *nothing*, and desk_report says so at the
bottom of every run: "None of this is randomized — the desk chose what it
watched."

So a number like "admitted names returned +0.63% over 30m" has no meaning yet.
If the same names returned +0.63% from an arbitrary moment that session, the
gating stack — CM RSI-2, %R exhaustion, RVOL, uptrend, zone geometry — is
selecting the day's movers and then taking credit for their movement.

Two controls, both from the shadow log, both matched to the admission by
construction:

  WITHIN   the same symbol, same session, a different instant. Asks whether
           the admission moment was special, or the symbol was.
  ACROSS   every other watched name at the same instant. Asks whether the
           watchlist simply moved together.

Forward returns use shadow_report.forward_return's rules — same price samples,
same horizon, same refusal to score a window that barely opened — so the
admitted column is directly comparable to desk_report's.

Read-only. Usage:
    python3 tools/admission_null.py [--days N] [--horizon-min N]
"""
from __future__ import annotations

import argparse
import bisect
import os
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

# How close another symbol's sample must sit to count as "the same instant".
ACROSS_TOL_SEC = 30.0


def _day(ts) -> str:
    return datetime.fromtimestamp(
        float(ts), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def episode_forwards(series: list[dict], horizon: float) -> list[float | None]:
    """forward_return for every index in *series*, in one linear sweep.

    Same semantics as shadow_report.forward_return: measured to the last
    sample inside the horizon, and None when that sample is less than half a
    horizon out — a truncated window is missing data, not a flat return.
    """
    n = len(series)
    ts = [float(r.get("ts") or 0) for r in series]
    px = [r.get("price") for r in series]
    out: list[float | None] = [None] * n
    j = 0
    for i in range(n):
        p0 = px[i]
        if not p0:
            continue
        if j < i:
            j = i
        while j + 1 < n and ts[j + 1] - ts[i] <= horizon:
            j += 1
        if j <= i or not px[j]:
            continue
        if ts[j] - ts[i] < horizon * 0.5:
            continue
        out[i] = (float(px[j]) - float(p0)) / float(p0) * 100.0
    return out


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
    se = 50.0 / (n ** 0.5)          # 1 s.e. on a win rate, in points
    sigma = abs(win - 50.0) / se if se else 0.0
    return (f"  {label:<34} n={n:<5} "
            f"median {statistics.median(diffs):+.3f}%  "
            f"mean {statistics.fmean(diffs):+.3f}%  "
            f"beat {win:.0f}%  ({sigma:.1f}σ from a coin flip)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    args = ap.parse_args()
    horizon = args.horizon_min * 60.0

    rows = sr.load()
    if not rows:
        print("no shadow log")
        return 1
    days = sorted({_day(r["ts"]) for r in rows if r.get("ts")})[-args.days:]
    dayset = set(days)
    rows = [r for r in rows if r.get("ts") and _day(r["ts"]) in dayset]
    eps = sr.by_episode(rows)
    print(f"{len(eps)} admission episodes across {days[0]}..{days[-1]}")

    # Per-episode forward returns, plus a flat time-indexed table for ACROSS.
    admitted: list[float] = []
    within_pairs: list[tuple[float, float]] = []      # (admitted, within)
    flat: dict[str, list[tuple[float, float]]] = defaultdict(list)
    adm_points: list[tuple[float, str, float]] = []   # (ts, symbol, fwd)

    for (sym, _adm_ts), series in eps.items():
        if len(series) < 2:
            continue
        fwd = episode_forwards(series, horizon)
        if fwd[0] is None:
            continue
        admitted.append(fwd[0])
        adm_points.append((float(series[0].get("ts") or 0), sym, fwd[0]))
        rest = [v for v in fwd[1:] if v is not None]
        if rest:
            within_pairs.append((fwd[0], statistics.median(rest)))
        for r, v in zip(series, fwd):
            if v is not None:
                flat[sym].append((float(r.get("ts") or 0), v))

    for v in flat.values():
        v.sort()

    # ACROSS: every OTHER symbol's forward return at the same instant.
    across_pairs: list[tuple[float, float]] = []
    for t0, sym, adm in adm_points:
        peers: list[float] = []
        for other, pts in flat.items():
            if other == sym or not pts:
                continue
            stamps = [p[0] for p in pts]
            k = bisect.bisect_left(stamps, t0)
            for cand in (k - 1, k):
                if 0 <= cand < len(pts) and abs(pts[cand][0] - t0) <= ACROSS_TOL_SEC:
                    peers.append(pts[cand][1])
                    break
        if len(peers) >= 3:
            across_pairs.append((adm, statistics.median(peers)))

    print(f"\nforward {args.horizon_min:g}m, shadow price samples:\n")
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

    print("\nThe paired rows are the whole point. A gating stack that picks")
    print("moments earns a positive 'admitted - within'. One that only picks")
    print("names does not, however good the ADMITTED row looks on its own.")
    print("Neither control is a market benchmark: both are drawn from names")
    print("the desk chose to watch, so they cannot detect a day when every")
    print("small-cap rose together. They bound the decision, not the universe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
