#!/usr/bin/env python3
"""Score the operator's discretionary picks against the bar everything failed.

THE QUESTION
    On 2026-09-05 nine entry rules, a 15-cell EXH x RSI search and 42 exit
    policies were measured over 2026-08-24..09-04. Every entry rule failed
    its controls; every exit shape scored zero gross. The conclusion was that
    no feature in shadow.jsonl identifies a moment — only a volatile name.

    That leaves one hypothesis the tape cannot settle: that the operator was
    reading something the desk does not log. These rows test it. If the picks
    clear the bar, there is something real to find and the job becomes
    finding it. If they land on the pool's base rate, that is the answer.

THE BAR (desk_null's, unchanged, so the number is comparable)
    n >= 30, median net of the round trip > 0, and a positive paired median
    against eligible-within at >= 2 sigma.

THE CONTROLS, weakest to strongest
    pool          every arm-evaluated moment. Says whether the picks beat the
                  universe at all.
    eligible      other RTH moments on the SAME name, desk_null's definition.
                  Holds the ticker constant, so it scores TIMING.
    passes        the names the operator looked at and rejected. Holds the
                  screen constant — the only control that tests selection as
                  the operator actually faced it, and the reason --pass rows
                  are worth logging.

HONESTY
    * entry at the 1m close at the pick's timestamp, never the shadow price:
      that field runs ~0.7% low and books stale quote as profit
    * within a bar the low is assumed to print before the high
    * a pick is skipped until its full horizon has elapsed, so a pick made
      today is not scored today
    * conviction, if recorded, is reported but never used to select

USAGE
    .venv/bin/python tools/score_picks.py
    .venv/bin/python tools/score_picks.py --hold 60 --policy ladder
    .venv/bin/python tools/score_picks.py --pool 1200
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import bars  # noqa: E402
import desk_null as N  # noqa: E402

ET = ZoneInfo("America/New_York")
PICKS = os.path.join(_ROOT, "ai_reports", "picks.jsonl")
SHADOW = os.path.join(_ROOT, "ai_reports", "shadow.jsonl")
SIDE = 0.10          # half the round trip, charged per fill
DRAWS = 8

POLICIES = {
    # name: (initial stop %, [(tier %, fraction)], trail %, arm level %)
    "fixed": (5.0, [(5.0, 1.0)], 0.0, 0.0),
    "ladder": (5.0, [(2.0, 1 / 3), (5.0, 1 / 3)], 2.0, 2.0),
    "trail1": (5.0, [], 1.0, 0.0),
}


def play(seq, stop, tiers, give, arm_at):
    """Walk one path. Returns pct net of the round trip, or None."""
    left, pnl, peak, lvl = 1.0, 0.0, 0.0, -stop
    tiers, last = list(tiers), None
    for h, l, c in seq:
        if l <= lvl:                      # low before high, deliberately
            return pnl + left * (lvl - SIDE) - SIDE
        while tiers and h >= tiers[0][0] and left > 1e-9:
            px, frac = tiers.pop(0)
            take = min(frac, left)
            pnl += take * (px - SIDE)
            left -= take
        if left <= 1e-9:
            return pnl - SIDE
        peak = max(peak, h)
        if give > 0 and peak >= arm_at:
            lvl = max(lvl, peak - give)
        last = c
    return None if last is None else pnl + left * (last - SIDE) - SIDE


def path(sym: str, day: str, t0: float, hold_min: float, feed: str):
    """Bars after t0, as (high%, low%, close%) off the 1m close at t0."""
    st, hi, lo, cl = bars.fetch_ohlc(sym, day, feed)
    if not st:
        return None
    i = next((j for j in range(len(st)) if st[j] >= t0), None)
    if i is None:
        return None
    ep = cl[i]
    if ep <= 0:
        return None
    end, seq = st[i] + hold_min * 60.0, []
    for j in range(i + 1, len(st)):
        if st[j] > end:
            break
        seq.append((100.0 * (hi[j] - ep) / ep, 100.0 * (lo[j] - ep) / ep,
                    100.0 * (cl[j] - ep) / ep))
    return seq or None


def score(sym, day, t0, hold, feed, pol):
    seq = path(sym, day, t0, hold, feed)
    return None if seq is None else play(seq, *pol)


def load_picks(path: str = PICKS):
    out = []
    try:
        fh = open(path)
    except FileNotFoundError:
        return out
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
                t0 = float(r["ts"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            sym = str(r.get("symbol") or "").upper()
            if sym and r.get("action") in ("take", "pass"):
                out.append((sym, bars.day_of(t0), t0, r.get("action"),
                            r.get("conviction"), r.get("features")))
    return out


def pool_moments(days: set[str], cap: int, rng):
    """Arm-evaluated moments on the same sessions, one per quarter hour."""
    seen, out = set(), []
    try:
        fh = open(SHADOW)
    except FileNotFoundError:
        return out
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
                t0 = float(r["ts"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if r.get("arm_ok") is None:
                continue
            sym, day = str(r.get("symbol") or ""), bars.day_of(t0)
            if not sym or day not in days:
                continue
            k = (sym, day, int(t0 // 900))
            if k in seen:
                continue
            seen.add(k)
            out.append((sym, day, t0))
    rng.shuffle(out)
    return out[:cap]


def first_watch_map(days: set[str]):
    fw = {}
    try:
        fh = open(SHADOW)
    except FileNotFoundError:
        return fw
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
                t0 = float(r["ts"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            sym, day = str(r.get("symbol") or ""), bars.day_of(t0)
            if not sym or day not in days:
                continue
            k = (sym, day)
            if k not in fw or t0 < fw[k]:
                fw[k] = t0
    return fw


def line(label, vals):
    if not vals:
        return
    print("  %-24s n=%-4d mean %+6.2f%%  median %+6.2f%%  win %2.0f%%"
          % (label, len(vals), statistics.fmean(vals), statistics.median(vals),
             100.0 * sum(1 for v in vals if v > 0) / len(vals)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hold", type=float, default=120.0, help="minutes")
    ap.add_argument("--policy", choices=sorted(POLICIES), default="ladder")
    ap.add_argument("--feed", choices=("sip", "iex"), default="sip")
    ap.add_argument("--pool", type=int, default=800, help="pool moments")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--picks", default=PICKS,
                    help="score a different picks file (dry runs, replays)")
    a = ap.parse_args()

    err = N.require_bars_client()
    if err:
        print(err, file=sys.stderr)
        return 2

    rng = random.Random(a.seed)
    pol = POLICIES[a.policy]
    rows = load_picks(a.picks)
    if not rows:
        print("no picks logged yet — record some with tools/mark_pick.py")
        return 0

    import time
    ripe = [r for r in rows if time.time() - r[2] > a.hold * 60.0 + 300]
    green = len(rows) - len(ripe)
    takes = [r for r in ripe if r[3] == "take"]
    passes = [r for r in ripe if r[3] == "pass"]
    days = {r[1] for r in ripe}

    print("=" * 74)
    print("  DISCRETIONARY PICKS — %s policy, %.0fm hold, %.2f%% round trip"
          % (a.policy, a.hold, 2 * SIDE))
    print("=" * 74)
    print("  %d logged (%d take / %d pass) over %d sessions%s"
          % (len(rows), sum(1 for r in rows if r[3] == "take"),
             sum(1 for r in rows if r[3] == "pass"), len(days),
             "; %d too recent to score" % green if green else ""))

    tv, by_day, diffs = [], defaultdict(list), []
    fw = first_watch_map(days)
    for sym, day, t0, _act, _c, _f in takes:
        v = score(sym, day, t0, a.hold, a.feed, pol)
        if v is None:
            continue
        tv.append(v)
        by_day[day].append(v)
        st = bars.fetch_ohlc(sym, day, a.feed)[0]
        if not st:
            continue
        cand = [s for s in N.eligible_stamps(st, t0, fw.get((sym, day), t0))
                if s + a.hold * 60.0 <= st[-1]]
        if not cand:
            continue
        pick = cand if len(cand) <= DRAWS else rng.sample(cand, DRAWS)
        cv = [x for x in (score(sym, day, s, a.hold, a.feed, pol) for s in pick)
              if x is not None]
        if cv:
            diffs.append(v - statistics.median(cv))

    pv = [v for v in (score(s, d, t, a.hold, a.feed, pol)
                      for s, d, t, _a, _c, _f in passes) if v is not None]
    pool = [v for v in (score(s, d, t, a.hold, a.feed, pol)
                        for s, d, t in pool_moments(days, a.pool, rng))
            if v is not None]

    print()
    line("PICKS (take)", tv)
    line("passes (rejected)", pv)
    line("pool (all moments)", pool)
    if not tv:
        print("\n  nothing scorable yet.")
        return 0

    # A ladder produces a positive median on ANY selection — it books small
    # gains often and leaves the losers to run. The null test on 2026-09-05
    # scored 60 random moments at a +0.40% median and a -0.50% mean. So the
    # median leg of the bar is nearly free under this policy, the mean is
    # what the account tracks, and eligible-within is what actually decides.
    if tv and statistics.median(tv) > 0 > statistics.fmean(tv):
        print("\n  NOTE  positive median with a negative mean is what a ladder"
              "\n        does to any selection, including a random one. The"
              "\n        mean is the account's number; the control below is"
              "\n        what says whether the picks carry information.")

    print()
    ps = N.paired_stats(diffs)
    if ps["n"]:
        print("  vs eligible-within       n=%-4d median %+6.2f%%  beat %2.0f%%  (%.1f sigma)"
              % (ps["n"], ps["median"], ps["beat"], ps["sigma"]))
    if pv:
        print("  picks - passes           %+6.2f%% on the mean, %+6.2f%% on the median"
              % (statistics.fmean(tv) - statistics.fmean(pv),
                 statistics.median(tv) - statistics.median(pv)))
    pos = sum(1 for v in by_day.values() if statistics.fmean(v) > 0)
    p = N.sign_test_p(pos, len(by_day))
    print("  by session               %d/%d positive%s"
          % (pos, len(by_day), "  p=%.3f" % p if p is not None else ""))

    ok = (len(tv) >= N.MIN_N and statistics.median(tv) > 0
          and ps["n"] and ps["median"] is not None and ps["median"] > 0
          and ps["sigma"] >= N.MIN_SIGMA)
    print("\n  BAR: n>=%d, median > 0, eligible-within > 0 at >=%.0f sigma"
          % (N.MIN_N, N.MIN_SIGMA))
    print("  VERDICT: %s" % ("PASS" if ok else "FAIL"))
    if len(tv) < N.MIN_N:
        print("  (underpowered — %d more picks to reach the bar)"
              % (N.MIN_N - len(tv)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
