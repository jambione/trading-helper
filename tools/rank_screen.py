#!/usr/bin/env python3
"""Can any feature RANK simultaneous candidates for a slot?

This asks a different question from every screen that came before it, and the
difference is the reason it is worth running at all.

entry_rule_screen and thesis_screen test TIME-SERIES prediction: does buying
this name at this moment beat buying it at another moment? Everything failed
that — exhaustion, RSI level and direction, RVOL, zone membership, the live
gates. This tests CROSS-SECTIONAL ranking: given six names all armable right
now, does a feature say which one goes furthest? Those can differ, because
whatever the tape does to all six cancels in the comparison. A feature can be
useless for "will this go up" and still order "which of these goes up more".

The economics differ too, and this is the point. slot_contention measured
SWAPPING out of a held name, which has to clear a round trip and did not.
Choosing between two candidates costs nothing extra — the desk opens a
position either way — so the bar here is beating zero, not beating the spread.

CHOICE SET. A contested moment is a cluster of `watch_skip / max_positions`
events: names the desk turned away because the book was full. Using the
skipped names is deliberate — the desk rejected all of them, so ranking within
that set carries none of its own preference. The name that got the slot is
excluded for exactly that reason.

TWO NUMBERS, and the second is the decision-relevant one:

  top − bottom      does the feature separate the set at all?
  top − set median  what ranking buys OVER the status quo, which is an
                    arbitrary pick (whoever armed first — poll timing and
                    zone geometry, not merit).

INDEPENDENCE. Contested moments arrive seconds apart with overlapping names
and overlapping forward windows; scoring them all would count one opinion many
times. Moments are taken non-overlapping (one per cooldown, default = the
horizon), and significance is reported BY SESSION as well as pooled, because
2026-08-19 alone is 82% of the skip events in the current tape.

Read-only. Nothing here changes config. Usage:
    python3 tools/rank_screen.py --days 5 --horizon-min 30
    python3 tools/rank_screen.py --days 5 --horizon-min 15 --min-set 3
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import random
import statistics
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bars  # noqa: E402
import desk_null as N  # noqa: E402
import shadow_report as sr  # noqa: E402

EVENTS = os.path.join(ROOT, "ai_reports", "events.jsonl")
# Names skipped within this of each other were competing for the same slot.
CLUSTER_SEC = 60.0
# A shadow row this far from the moment still describes it.
FEATURE_TOL_SEC = 45.0
MIN_SET = 2


def _f(row: dict, key: str):
    try:
        v = row.get(key)
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _bool_feat(row: dict, key: str):
    v = row.get(key)
    return None if v is None else (1.0 if bool(v) else 0.0)


# name -> (description, extractor, prefer). prefer="high" ranks the largest
# value first; "low" the smallest. Both directions of the same feature are
# listed where the desk has no settled opinion, since a feature that separates
# in EITHER direction is a finding — but see the reversed/shuffle checks.
FEATURES = {
    "cm_rsi_low":    ("prefer LOWER CM RSI-2 — least extended of the set",
                      lambda r: _f(r, "cm_rsi"), "low"),
    "cm_rsi_high":   ("prefer HIGHER CM RSI-2", lambda r: _f(r, "cm_rsi"), "high"),
    "exh_low":       ("prefer LOWER %R exhaustion — coolest of the set",
                      lambda r: _f(r, "exhaustion"), "low"),
    "exh_high":      ("prefer HIGHER %R exhaustion",
                      lambda r: _f(r, "exhaustion"), "high"),
    "rvol_high":     ("prefer HIGHER relative volume",
                      lambda r: _f(r, "rvol"), "high"),
    "score_high":    ("prefer HIGHER scanner score",
                      lambda r: _f(r, "score"), "high"),
    "pct_change_low": ("prefer SMALLEST move so far — least chased",
                       lambda r: _f(r, "pct_change"), "low"),
    "pct_change_high": ("prefer BIGGEST move so far — most momentum",
                        lambda r: _f(r, "pct_change"), "high"),
    "proximity_low": ("prefer CLOSEST to its entry zone",
                      lambda r: _f(r, "proximity_pct"), "low"),
    "spread_low":    ("prefer TIGHTEST spread — cheapest to trade",
                      lambda r: _f(r, "spread_r"), "low"),
    "rising":        ("prefer cm_rsi_rising true",
                      lambda r: _bool_feat(r, "cm_rsi_rising"), "high"),
    "in_zone":       ("prefer price already in the drawn zone",
                      lambda r: _bool_feat(r, "in_zone"), "high"),
    # Honesty check. Must land on zero; if a random ranker separates the set,
    # the harness is manufacturing the spread and no other row can be believed.
    "random":        ("RANDOM order — control, must score ~0",
                      lambda r: None, "high"),
}


def load_contested(days: set[str]) -> dict[str, list]:
    """day -> sorted [(ts, symbol)] of max_positions skips."""
    out: dict[str, list] = defaultdict(list)
    if not os.path.exists(EVENTS):
        return out
    with open(EVENTS, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("kind") != "watch_skip":
                continue
            if str(d.get("reason")) != "max_positions":
                continue
            ts = d.get("ts")
            sym = str(d.get("symbol") or "").upper()
            if not ts or not sym:
                continue
            ts = float(ts)
            day = bars.day_of(ts)
            if day in days:
                out[day].append((ts, sym))
    for v in out.values():
        v.sort()
    return out


def build_moments(contested, horizon: float, cooldown: float, min_set: int):
    """Non-overlapping contested moments: (ts, day, [symbols]).

    One opinion per cooldown. Consecutive skips name the same crowd seconds
    apart, and their forward windows overlap almost entirely, so scoring each
    would inflate n without adding information.
    """
    moments = []
    for day, rows in sorted(contested.items()):
        quiet_until = 0.0
        for ts, _sym in rows:
            if ts < quiet_until:
                continue
            if not (N.RTH_START_MIN <= bars.et_minutes(ts) <= N.RTH_END_MIN):
                continue
            names = sorted({s for t, s in rows if abs(t - ts) <= CLUSTER_SEC})
            if len(names) >= min_set:
                moments.append((ts, day, names))
                quiet_until = ts + cooldown
    return moments


def feature_index(rows):
    """symbol -> (sorted stamps, rows) for nearest-tick feature lookup."""
    by: dict[str, list] = defaultdict(list)
    for r in rows:
        ts = r.get("ts")
        sym = str(r.get("symbol") or "").upper()
        if ts and sym:
            by[sym].append((float(ts), r))
    out = {}
    for sym, v in by.items():
        v.sort(key=lambda x: x[0])
        out[sym] = ([x[0] for x in v], [x[1] for x in v])
    return out


def features_at(index, sym: str, ts: float):
    hit = index.get(sym)
    if not hit:
        return None
    stamps, rows = hit
    i = bisect.bisect_left(stamps, ts)
    best, gap = None, None
    for k in (i - 1, i):
        if 0 <= k < len(stamps):
            g = abs(stamps[k] - ts)
            if gap is None or g < gap:
                best, gap = rows[k], g
    return best if (best is not None and gap is not None
                    and gap <= FEATURE_TOL_SEC) else None


def score_feature(moments, index, horizon, ctx, name, rng):
    """Paired spreads for one feature across every usable moment."""
    desc, extract, prefer = FEATURES[name]
    top_bottom, top_median, by_day = [], [], defaultdict(list)
    used_sets = []
    for ts, day, names in moments:
        hz = N.capped_horizon(ts, horizon, getattr(ctx, "flatten_min", None))
        if hz is None or hz <= 0:
            continue
        members = []
        for sym in names:
            row = features_at(index, sym, ts)
            if row is None:
                continue
            val = rng.random() if name == "random" else extract(row)
            if val is None:
                continue
            st, cl = bars.fetch(sym, day, ctx.feed)
            fwd = bars.forward_return(st, cl, ts, hz) if st else None
            if fwd is None:
                continue
            members.append((val, fwd, sym))
        if len(members) < MIN_SET:
            continue
        members.sort(key=lambda m: m[0], reverse=(prefer == "high"))
        # Ties carry no opinion — if the best and worst share a value the
        # feature did not actually rank this set.
        if members[0][0] == members[-1][0]:
            continue
        fwds = [m[1] for m in members]
        tb = members[0][1] - members[-1][1]
        tm = members[0][1] - statistics.median(fwds)
        top_bottom.append(tb)
        top_median.append(tm)
        by_day[day].append(tm)
        used_sets.append(len(members))
    return top_bottom, top_median, by_day, used_sets


def _line(label, vals):
    if not vals:
        return f"    {label:<18} n=0"
    n = len(vals)
    beat = 100.0 * sum(1 for v in vals if v > 0) / n
    se = 50.0 / (n ** 0.5)
    sigma = abs(beat - 50.0) / se if se else 0.0
    return (f"    {label:<18} n={n:<4} median {statistics.median(vals):+.3f}%  "
            f"mean {statistics.fmean(vals):+.3f}%  beat {beat:.0f}%  "
            f"({sigma:.1f}σ)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--cooldown-min", type=float, default=0.0,
                    help="minutes between scored moments (default = horizon)")
    ap.add_argument("--min-set", type=int, default=MIN_SET)
    ap.add_argument("--feed", choices=("sip", "iex"), default="sip")
    ap.add_argument("--flatten-et", default="15:50")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    horizon = args.horizon_min * 60.0
    cooldown = (args.cooldown_min or args.horizon_min) * 60.0
    rng = random.Random(args.seed)
    hh, _, mm = args.flatten_et.partition(":")
    flatten_min = int(hh) * 60 + int(mm or 0)

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

    contested = load_contested(dayset)
    moments = build_moments(contested, horizon, cooldown, args.min_set)
    if not moments:
        print("EMPTY RUN — no contested moments; no verdict")
        return 1
    sizes = sorted(len(m[2]) for m in moments)
    print(f"{len(moments)} non-overlapping contested moments across "
          f"{len({m[1] for m in moments})} sessions "
          f"(cooldown {cooldown / 60:g}m, set size median {statistics.median(sizes):.0f})")
    for day in days:
        k = sum(1 for m in moments if m[1] == day)
        print(f"  {day}: {k} moments")

    ctx = N.prepare_context(rows, days, args.feed, rng, build_outside=False,
                            flatten_min=flatten_min)
    index = feature_index(rows)

    ranked = []
    for name in FEATURES:
        tb, tm, by_day, sizes = score_feature(
            moments, index, horizon, ctx, name, random.Random(args.seed))
        desc = FEATURES[name][0]
        print(f"\n=== {name} — {desc}")
        if not tm:
            print("    no moment ranked by this feature")
            continue
        print(_line("top - bottom", tb))
        print(_line("top - set median", tm))
        pos = sum(1 for d in by_day if statistics.median(by_day[d]) > 0)
        p = N.sign_test_p(pos, len(by_day))
        print(f"    by session       {pos}/{len(by_day)} positive  "
              f"p={p:.3f}" if p is not None else "    by session       n/a")
        ranked.append((name, len(tm), statistics.median(tm),
                       100.0 * sum(1 for v in tm if v > 0) / len(tm), p))

    ranked.sort(key=lambda r: -r[2])
    print(f"\n{'feature':<18} {'n':>4} {'top-median':>11} {'beat':>6} {'p(sess)':>8}")
    print("-" * 52)
    for name, n, med, beat, p in ranked:
        ps = f"{p:.3f}" if p is not None else "n/a"
        print(f"{name:<18} {n:>4} {med:>+10.3f}% {beat:>5.0f}% {ps:>8}")

    print("\n'top - set median' is what ranking buys over the status quo —")
    print("an arbitrary pick from the same crowd. Costs cancel (the desk opens")
    print("a position either way), so the bar is beating zero, not the spread.")
    print("`random` must sit on zero. If it does not, believe nothing above it.")
    print("A feature earns a build only with session support, not pooled sigma:")
    print("one session is most of the skip events on this tape.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
