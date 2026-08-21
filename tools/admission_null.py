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

Controls (see tools/desk_null.py for the definitions and the hindsight fix):

  ELIGIBLE-WITHIN  same name, after it was on the watchlist. Honest timing.
  LEGACY-WITHIN    any RTH bar, |Δt| > horizon. The 2026-08-20 number; inflated.
  ACROSS           other watched names, same instant.
  OUTSIDE          never-watched, same instant, price-matched.
  OUTSIDE-VOL      also matched on 15m realized vol.
  IWM              small-cap beta over the same horizon.

Forward returns come from 1-minute bars, not the shadow series.

Read-only. Usage:
    python3 tools/admission_null.py [--days N] [--horizon-min N] [--feed sip]
"""
from __future__ import annotations

import argparse
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bars
import desk_null as N
import shadow_report as sr

# Re-exports: entry_rule_screen imported these from here before the kernel
# lived in desk_null. Keep the names so a stale import still runs.
OUTSIDE_BAND = N.OUTSIDE_BAND
OUTSIDE_POOL = N.OUTSIDE_POOL
RTH_END_MIN = N.RTH_END_MIN
RTH_START_MIN = N.RTH_START_MIN
WITHIN_DRAWS = N.WITHIN_DRAWS
ACROSS_TOL_SEC = N.ACROSS_TOL_SEC
UNIVERSE = str(N.UNIVERSE)
_day = N._day
_et_minutes = N._et_minutes
_index_at = N._index_at
_stat = N._stat
_paired = N._paired
load_universe = N.load_universe
build_outside_pool = N.build_outside_pool


def _across_pairs(admissions, watched_at, horizon, feed):
    """admitted fwd vs median of other watched names at the same instant."""
    pairs = []
    for t0, sym, day, _row in admissions:
        stamps, closes = bars.fetch(sym, day, feed)
        if not stamps:
            continue
        a = bars.forward_return(stamps, closes, t0, horizon)
        if a is None:
            continue
        peers = []
        seen = set()
        for ts, other in watched_at.get(day, ()):
            if other == sym or other in seen or abs(ts - t0) > ACROSS_TOL_SEC:
                continue
            seen.add(other)
            o_st, o_cl = bars.fetch(other, day, feed)
            if not o_st:
                continue
            v = bars.forward_return(o_st, o_cl, t0, horizon)
            if v is not None:
                peers.append(v)
        if len(peers) >= 3:
            import statistics
            pairs.append((a, statistics.median(peers)))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--feed", choices=("sip", "iex"), default="sip")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--haircut", type=float, default=N.HAIRCUT_PCT,
                    help="round-trip spread in percent, vs cash only")
    ap.add_argument("--bench", default=N.BENCH)
    args = ap.parse_args()
    horizon = args.horizon_min * 60.0
    rng = random.Random(args.seed)
    why = N.require_bars_client()
    if why:
        print(why)
        return 1

    rows = sr.load()
    if not rows:
        print("no shadow log")
        return 1
    days = sorted({bars.day_of(r["ts"]) for r in rows if r.get("ts")})[-args.days:]
    dayset = set(days)
    rows = [r for r in rows if r.get("ts") and bars.day_of(r["ts"]) in dayset]
    admissions = N.collect_admissions(rows)
    print(f"{len(admissions)} admissions in RTH across {days[0]}..{days[-1]}")
    if not admissions:
        print("EMPTY RUN — no RTH admissions; no verdict")
        return 1

    ctx = N.prepare_context(rows, days, args.feed, rng,
                            haircut=args.haircut, bench=args.bench)
    scores = []
    no_bars = 0
    for t0, sym, day, _row in admissions:
        s = N.score_one(t0, sym, day, horizon, ctx)
        if s is None:
            no_bars += 1
            continue
        scores.append(s)

    if not scores:
        print("EMPTY RUN — no admission scored; no verdict")
        return 1

    print(f"scored {len(scores)} ({no_bars} had no bars)")
    print(f"\nforward {args.horizon_min:g}m, {args.feed.upper()} 1m bars, "
          f"haircut {args.haircut:.2f}%, bench {args.bench}:\n")
    N.print_scorecard(scores, args.haircut, title="all admissions")

    watched_at = N.watched_times(rows)
    across = _across_pairs(admissions, watched_at, horizon, args.feed)
    print(N.format_paired("admitted - across (same-instant names)",
                          [a - c for a, c in across]))

    print("\nThe paired rows are the whole point.")
    print("  eligible-within  is the timing claim. Legacy-within includes the")
    print("                   pre-list run-up and is kept only to show the bias.")
    print("  outside          bounds the SCANNER. vol+price is the tighter one;")
    print("                   empty vol matches are skipped, not silently")
    print("                   downgraded to price-only.")
    print("  residual         name minus IWM. Until this is off zero, drift is")
    print("                   the tape, not the name.")
    print("  haircut          is vs cash. It cancels in a paired same-cost row.")
    print("A PASS on this tool is not a config change. Next step is")
    print("tools/thesis_screen.py — different information, same gates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
