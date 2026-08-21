#!/usr/bin/env python3
"""Rank candidate ENTRY RULES against the desk_null controls.

admission_null answers whether the live admissions beat no-decision.
This answers whether some OTHER predicate over the same tape would have.

A rule is a predicate over a shadow tick. For each rule:

  1. Replay it over every watched tick of the last N sessions.
  2. Take the FIRST firing per symbol, then nothing again until a full
     horizon has passed — consecutive ticks fire together, and overlapping
     windows on one name are one bet counted many times.
  3. Score off 1m bars against desk_null: eligible-within (honest timing),
     outside, IWM residual, haircut vs cash.

A rule earns the simulator only if verdict() is PASS — n≥30, median net of
haircut > 0, eligible-within paired median > 0 at ≥2σ. `rule fwd` alone is
not the bar. Feature-missing ticks (exhaustion is None) do not fire a rule
that reads exhaustion: the predicates already treat None as False.

The H1 family (cool_*) and the honesty checks (hot_rising, live_arm) stay
here so a re-run is comparable. New information (open-drive, research vs
scanner, chase vs fresh) lives in thesis_screen.py — do not add RSI/%R
permutations to this file; that family is falsified.

Read-only. Usage:
    python3 tools/entry_rule_screen.py [--days N] [--horizon-min N]
    python3 tools/entry_rule_screen.py --rules cool_rising,hot_rising
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bars
import desk_null as N
import shadow_report as sr


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
            if not (N.RTH_START_MIN <= bars.et_minutes(ts) <= N.RTH_END_MIN):
                continue
            try:
                fired = bool(rule(r))
            except Exception:  # noqa: BLE001 — a broken predicate must skip, not crash
                fired = False
            if fired:
                moments.append((ts, sym, bars.day_of(ts)))
                quiet_until = ts + horizon
    return moments


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--feed", choices=("sip", "iex"), default="sip")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--rules", type=str, default="",
                    help="comma-separated subset of rule names")
    ap.add_argument("--haircut", type=float, default=N.HAIRCUT_PCT)
    ap.add_argument("--bench", default=N.BENCH)
    args = ap.parse_args()
    horizon = args.horizon_min * 60.0
    rng = random.Random(args.seed)

    names = [n.strip() for n in args.rules.split(",") if n.strip()] or list(RULES)
    bad = [n for n in names if n not in RULES]
    if bad:
        print(f"unknown rules: {bad}; have {list(RULES)}")
        return 1
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
    print(f"{len(rows)} watched ticks across {days[0]}..{days[-1]}, "
          f"horizon {args.horizon_min:g}m, cooldown = horizon")

    ctx = N.prepare_context(rows, days, args.feed, rng,
                            haircut=args.haircut, bench=args.bench)

    ranked = []
    for name in names:
        desc, rule = RULES[name]
        moments = collect_moments(rows, rule, horizon)
        scores = N.score_moments(moments, horizon, ctx)
        print(f"\n=== {name} — {desc}")
        print(f"  fired {len(moments)} de-correlated moments "
              f"across {len({m[1] for m in moments})} symbols, "
              f"scored {len(scores)}")
        v = N.print_scorecard(scores, args.haircut)
        elig = N.diffs(scores, "eligible")
        med = statistics.median(elig) if elig else None
        ranked.append((name, v, len(scores), med))

    ranked.sort(key=lambda r: -(r[3] if r[3] is not None else -99))
    print(f"\n{'rule':<16} {'verdict':<13} {'n':>5} {'vs eligible':>12}")
    print("-" * 50)
    for name, v, n, med in ranked:
        med_s = f"{med:+.3f}%" if med is not None else "n/a"
        print(f"{name:<16} {v:<13} {n:>5} {med_s:>12}")

    print("\nPASS = n≥30, median net of haircut > 0, eligible-within > 0 at ≥2σ.")
    print("Survivors go to optimize_rstop with exits sized to THIS horizon.")
    print("Do not add another RSI/%R arrangement; that family is falsified.")
    print("New information (clock, research, chase) is tools/thesis_screen.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
