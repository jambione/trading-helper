#!/usr/bin/env python3
"""
sim_heat_min_sweep.py — does raising ai_watch_exhaustion_heat_min_pct help?

The 08-11 A/B showed continuation is a strict superset of exhaustion_scalp
(scalp_only = 0): it takes every overbought arm plus a tail of `heating` arms
gated by ``ai_watch_exhaustion_heat_min_pct`` (default 50). Those marginal arms
averaged roughly half the 30m forward return of the overbought core, so the
question is whether a higher heat floor trims the weak tail or just trims trades.

SECTIONS
  A. SHAPE  heating arms bucketed by %R at arm time — mean/median/hit/SE per
            bucket. If dilution is real, low buckets underperform high ones.
  B. SWEEP  cumulative effect of each heat_min: how many cont-only arms survive,
            what they return, and what the blended book looks like vs scalp.

HONESTY
  • 30m forward return on shadow polls is a signal-quality proxy, NOT trade P&L:
    no stop, no T1, no slippage, no position sizing.
  • Samples are not independent — one symbol trending for an hour contributes
    many in-zone polls that overlap the same 30m window. n is poll count, not
    trade count, so SE understates the true error.
  • Shadow prices are ask-like.
  • Selecting a threshold on the same day that motivated it is in-sample
    fitting. Treat any winner as a hypothesis to forward-test.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import sim_edge_mode_ab as sim

import ai_entry_watch as ew


def stats(xs: list[float]) -> dict[str, Any]:
    """Mean/median/hit-rate/SE for a forward-return sample."""
    if not xs:
        return {"n": 0, "mean": None, "median": None, "hit": None, "se": None}
    n = len(xs)
    m = sum(xs) / n
    se = (statistics.stdev(xs) / (n ** 0.5)) if n > 1 else None
    return {
        "n": n,
        "mean": m,
        "median": statistics.median(xs),
        "hit": sum(1 for x in xs if x > 0) / n * 100.0,
        "se": se,
    }


def fmt(s: dict[str, Any]) -> str:
    if not s["n"]:
        return "n=0"
    se = f" ±{s['se']:.3f}" if s["se"] is not None else ""
    return (
        f"n={s['n']:<4} mean={s['mean']:+.3f}%{se}  "
        f"med={s['median']:+.3f}%  hit={s['hit']:.0f}%"
    )


def collect(shadow: list[dict]) -> dict[str, Any]:
    """Split in-zone samples into the overbought core and the heating tail."""
    cc = sim.cont_cfg()
    in_zone = [r for r in shadow if r.get("in_zone") is True]

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in shadow:
        by_sym[str(r.get("symbol") or "")].append(r)
    for s in by_sym:
        by_sym[s].sort(key=lambda x: float(x.get("ts") or 0))

    core: list[float] = []          # overbought — armed by BOTH modes
    heating: list[tuple[float, float, str]] = []   # (pct_r, fwd, symbol)
    heat_no_fwd = 0

    for r in in_zone:
        rec = sim.rec_from_shadow(r)
        state = ew.exhaustion_state(rec, cc)
        sym = str(r.get("symbol") or "")
        fr = sim.fwd_ret(by_sym, sym, r.get("ts"))
        if state == "overbought":
            if fr is not None:
                core.append(fr)
        elif state == "heating":
            ex = ew.exhaustion_pct(rec)
            if ex is None:
                continue
            if fr is None:
                heat_no_fwd += 1
                continue
            heating.append((ex, fr, sym))

    return {
        "in_zone_n": len(in_zone),
        "core": core,
        "heating": heating,
        "heat_no_fwd": heat_no_fwd,
    }


def section_shape(heating: list[tuple[float, float, str]]) -> list[dict]:
    """Heating arms bucketed by %R at arm time."""
    edges = [(0, 40), (40, 50), (50, 55), (55, 60), (60, 65),
             (65, 70), (70, 75), (75, 80)]
    out = []
    for lo, hi in edges:
        xs = [f for ex, f, _ in heating if lo <= ex < hi]
        syms = {s for ex, _, s in heating if lo <= ex < hi}
        row = stats(xs)
        row.update({"lo": lo, "hi": hi, "syms": len(syms),
                    "gated_today": lo < 50})
        out.append(row)
    return out


def section_sweep(
    core: list[float],
    heating: list[tuple[float, float, str]],
    thresholds: list[float],
) -> list[dict]:
    """Cumulative effect of each heat_min on the continuation book."""
    core_s = stats(core)
    rows = []
    for t in thresholds:
        kept = [f for ex, f, _ in heating if ex + 1e-9 >= t]
        blended = core + kept
        k, b = stats(kept), stats(blended)
        rows.append({
            "heat_min": t,
            "cont_only": k,
            "blended": b,
            "dropped": len(heating) - len(kept),
            # Total captured move: proxy for opportunity, not risk-adjusted.
            "sum_blended": sum(blended),
            "sum_core": sum(core),
            "core_mean": core_s["mean"],
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="heat_min threshold sweep")
    ap.add_argument("--day", default="2026-08-11")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    shadow_path = sim._report("shadow.jsonl")
    shadow = sim.load_jsonl_day(shadow_path, args.day, ("ts",))
    if not shadow:
        print(f"ERROR: no shadow rows for {args.day} ({shadow_path})",
              file=sys.stderr)
        return 1

    d = collect(shadow)
    core, heating = d["core"], d["heating"]
    shape = section_shape(heating)
    thresholds = [50.0, 55.0, 60.0, 62.5, 65.0, 67.5, 70.0, 72.5, 75.0]
    sweep = section_sweep(core, heating, thresholds)

    if args.json:
        print(json.dumps(
            {"day": args.day, "shape": shape, "sweep": sweep,
             "core": stats(core), "heating_n": len(heating)},
            indent=2, default=str))
        return 0

    core_s = stats(core)
    print("=" * 74)
    print(f"HEAT_MIN SWEEP — {args.day}")
    print("  does a higher heating floor trim the weak tail, or just trim trades?")
    print("=" * 74)
    print()
    print(f"in-zone samples: {d['in_zone_n']}")
    print(f"overbought core (armed by BOTH modes): {fmt(core_s)}")
    print(f"heating tail (continuation-only):      n={len(heating)}"
          f"   (+{d['heat_no_fwd']} with no 30m forward window)")
    print()

    print("A) HEATING ARMS BY %R AT ARM TIME")
    print(f"   {'band':<12} {'stats':<52} syms")
    for r in shape:
        tag = f"{r['lo']}-{r['hi']}"
        if r["gated_today"]:
            tag += " *"
        print(f"   {tag:<12} {fmt(r):<52} {r['syms']}")
    print("   * below today's heat_min=50 — already refused as heating_too_low")
    print()

    print("B) SWEEP — continuation book at each heat_min")
    print(f"   {'heat_min':<9} {'cont-only arms':<40} {'blended w/ core':<40} drop")
    for r in sweep:
        print(f"   {r['heat_min']:<9.1f} {fmt(r['cont_only']):<40} "
              f"{fmt(r['blended']):<40} {r['dropped']}")
    print()
    print(f"   scalp baseline (core only):  {fmt(core_s)}")
    print()
    print("   total captured 30m move (sum of fwd%, opportunity proxy):")
    print(f"     core only: {sweep[0]['sum_core']:+.1f}%")
    for r in sweep:
        print(f"     heat_min {r['heat_min']:<5.1f}: {r['sum_blended']:+.1f}%"
              f"   ({r['blended']['n']} arms)")
    print()
    print("HONESTY")
    print("  30m fwd on polls is signal quality, not P&L — no stop, T1, or slippage.")
    print("  Overlapping polls of the same symbol are not independent; SE is optimistic.")
    print("  Picking a threshold on the day that motivated it is in-sample fitting.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
