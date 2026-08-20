#!/usr/bin/env python3
"""Rank candidate ENTRY RULES against the same controls that failed the desk.

admission_null answered one question — do the desk's actual admissions beat a
random moment / a random name? (No: −0.35% vs within at 30m, 4.2σ; 0.6σ vs
outside.) But its sample is the entries the live gates produced, so it cannot
say whether some OTHER rule over the same tape would have had edge. The heat
sweep could not answer that either: every cell kept the live 86-second exits,
so cool entries were only ever tested shackled to a horizon where the median
move is zero.

This screens rules directly, with no trading machinery in the loop. A rule is
a predicate over a shadow tick — the per-tick features the desk already logs
(exhaustion, cm_rsi, cm_rsi_rising, rvol, in_zone, arm_ok…). For each rule:

  1. Replay it over every watched tick of the last N sessions.
  2. Take the FIRST firing per symbol, then nothing again until a full
     horizon has passed — consecutive ticks fire together, and overlapping
     windows on one name are one bet counted many times, which is how a fake
     4σ gets manufactured.
  3. Score each moment off 1m bars, paired against WITHIN (random instant,
     same name and day) and OUTSIDE (price-matched names never watched).

A rule earns the simulator only if `rule − within` is positive with real
sigma. `rule fwd` alone is not the bar: these names drift, so a rule can look
green while being worse than throwing a dart at its own chart.

The pipeline this feeds: screen here → sweep in optimize_rstop with exits
sized to the rule's horizon → forward paper test. Kill at the first gate that
fails.

Read-only. Usage:
    python3 tools/entry_rule_screen.py [--days N] [--horizon-min N]
    python3 tools/entry_rule_screen.py --rules cool_rising,hot_rising
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
import os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import shadow_report as sr  # noqa: E402
import bars  # noqa: E402
from admission_null import (  # noqa: E402
    OUTSIDE_BAND,
    RTH_END_MIN,
    RTH_START_MIN,
    WITHIN_DRAWS,
    _day,
    _et_minutes,
    _index_at,
    _paired,
    _stat,
    build_outside_pool,
)


def _f(row: dict, key: str) -> float | None:
    try:
        v = row.get(key)
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _cool(row, lo=0.0, hi=50.0) -> bool:
    e = _f(row, "exhaustion")
    return e is not None and lo <= e < hi


# name -> (description, predicate). Every predicate must be defensive: shadow
# rows carry None for any feature the desk could not compute at that tick, and
# None must read as "rule does not fire", never as a crash or a free pass.
RULES: dict = {
    # ── H1 family: the posture today's buckets point at ──
    "cool_rising": (
        "exhaustion 0-50 AND cm_rsi_rising — the range heat_min=40 refuses",
        lambda r: _cool(r) and r.get("cm_rsi_rising") is True,
    ),
    "cool_only": (
        "exhaustion 0-50, no direction test",
        _cool,
    ),
    "cool_rsi_band": (
        "exhaustion 0-50 AND cm_rsi<=50 AND rising — the desk's own RSI rule, cold side",
        lambda r: (_cool(r) and r.get("cm_rsi_rising") is True
                   and (_f(r, "cm_rsi") or 999) <= 50),
    ),
    "cool_rvol2": (
        "exhaustion 0-50 AND rvol>=2 — cold entry, but only where volume showed up",
        lambda r: _cool(r) and (_f(r, "rvol") or 0) >= 2.0,
    ),
    "cool_in_zone": (
        "exhaustion 0-50 AND price in the drawn zone — the pullback actually reached",
        lambda r: _cool(r) and bool(r.get("in_zone")),
    ),
    # ── diagnostics: known answers, to prove the screen is honest ──
    "hot_rising": (
        "exhaustion>=75 AND rising — what the desk buys; should score NEGATIVE",
        lambda r: (_f(r, "exhaustion") or 0) >= 75 and r.get("cm_rsi_rising") is True,
    ),
    "live_arm": (
        "arm_ok ticks — the live gates themselves; should track admission_null",
        lambda r: r.get("arm_ok") is True,
    ),
}


def collect_moments(rows, rule, horizon: float):
    """First firing per symbol, then silence for a full horizon.

    Shadow samples land every ~20s, so a rule that is true for ten minutes is
    true on ~30 consecutive ticks — one opinion, thirty rows. Overlapping
    forward windows on one name are the same bet re-counted, and re-counting
    is where fake significance comes from. One fire, one horizon of quiet.
    """
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_sym[str(r["symbol"]).upper()].append(r)
    moments = []
    for sym, series in by_sym.items():
        series.sort(key=lambda r: r.get("ts") or 0)
        quiet_until = 0.0
        for r in series:
            ts = float(r.get("ts") or 0)
            if not ts or ts < quiet_until:
                continue
            if not (RTH_START_MIN <= _et_minutes(ts) <= RTH_END_MIN):
                continue
            try:
                fired = bool(rule(r))
            except Exception:
                fired = False
            if fired:
                moments.append((ts, sym, _day(ts)))
                quiet_until = ts + horizon
    return moments


def score_moments(moments, pools, horizon: float, feed: str, rng):
    """(fwd, vs_within, vs_outside) lists for a set of (ts, sym, day)."""
    fwd, within_d, outside_d = [], [], []
    for t0, sym, day in moments:
        stamps, closes = bars.fetch(sym, day, feed)
        if not stamps:
            continue
        a = bars.forward_return(stamps, closes, t0, horizon)
        if a is None:
            continue
        fwd.append(a)

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
            within_d.append(a - statistics.median(draws))

        i = _index_at(stamps, t0)
        p0 = closes[i] if 0 <= i < len(closes) else None
        others = []
        for o_st, o_cl in pools.get(day, {}).values():
            j = _index_at(o_st, t0)
            if j < 0:
                continue
            if p0:
                ratio = o_cl[j] / p0
                if not (OUTSIDE_BAND[0] <= ratio <= OUTSIDE_BAND[1]):
                    continue
            v = bars.forward_return(o_st, o_cl, t0, horizon)
            if v is not None:
                others.append(v)
        if len(others) >= 3:
            outside_d.append(a - statistics.median(others))
    return fwd, within_d, outside_d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--feed", choices=("sip", "iex"), default="sip")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--rules", type=str, default="",
                    help="comma-separated subset of rule names")
    args = ap.parse_args()
    horizon = args.horizon_min * 60.0
    rng = random.Random(args.seed)

    names = [n.strip() for n in args.rules.split(",") if n.strip()] or list(RULES)
    bad = [n for n in names if n not in RULES]
    if bad:
        print(f"unknown rules: {bad}; have {list(RULES)}")
        return 1

    rows = sr.load()
    if not rows:
        print("no shadow log")
        return 1
    days = sorted({_day(r["ts"]) for r in rows if r.get("ts")})[-args.days:]
    dayset = set(days)
    rows = [r for r in rows if r.get("ts") and _day(r["ts"]) in dayset]
    print(f"{len(rows)} watched ticks across {days[0]}..{days[-1]}, "
          f"horizon {args.horizon_min:g}m, cooldown = horizon")

    watched_by_day: dict[str, set] = defaultdict(set)
    for r in rows:
        watched_by_day[_day(r["ts"])].add(str(r["symbol"]).upper())
    pools = {}
    for day in days:
        pools[day] = build_outside_pool(day, watched_by_day[day], rng, args.feed)
    print("outside pools: " + ", ".join(
        f"{d}:{len(p)}" for d, p in pools.items()))

    ranked = []
    for name in names:
        desc, rule = RULES[name]
        moments = collect_moments(rows, rule, horizon)
        syms = len({m[1] for m in moments})
        fwd, w, o = score_moments(moments, pools, horizon, args.feed, rng)
        print(f"\n=== {name} — {desc}")
        print(f"  fired {len(moments)} de-correlated moments "
              f"across {syms} symbols, scored {len(fwd)}")
        if not fwd:
            print("  nothing scored")
            continue
        print(_stat("rule fwd", fwd))
        print(_paired("rule - within", w))
        print(_paired("rule - outside", o))
        if len(w) < 30:
            print("  UNDERPOWERED — under 30 paired moments; direction only")
        ranked.append((name, len(w),
                       statistics.median(w) if w else float("nan"),
                       100.0 * sum(1 for x in w if x > 0) / len(w) if w else 0))

    ranked.sort(key=lambda r: -(r[2] if r[2] == r[2] else -99))
    print(f"\n{'rule':<16} {'n':>5} {'median vs within':>17} {'beat':>6}")
    print("-" * 48)
    for name, n, med, beat in ranked:
        print(f"{name:<16} {n:>5} {med:>+16.3f}% {beat:>5.0f}%")

    print("\nThe bar is `rule - within` positive with sigma, not a green")
    print("`rule fwd` — these names drift, so any rule can look green while")
    print("losing to a dart. Survivors go to optimize_rstop with exits sized")
    print("to THIS horizon, then to forward paper; friction on these names is")
    print("roughly 0.1-0.3% round trip and the medians here are gross of it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
