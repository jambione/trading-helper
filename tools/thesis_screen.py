#!/usr/bin/env python3
"""Screen the turnaround theses — different information, same kill gates.

The RSI/%R/RVOL/zone family is falsified (entry_rule_screen, 2026-08-20).
This file does not rearrange those features. It splits the same admissions
on information the mechanical layer never used:

  open_drive     9:35–10:00. Small-cap energy is front-loaded.
  morning/mid/late   honesty: late should not win if open_drive is the edge.
  research       symbol was on that day's grok/claude list, or source=research.
  scanner        the complement — late-attention tape.
  champion       rank-1 / highest-score idea that day. Usually underpowered.
  catalyst_text  research + catalyst language in the write-up. Text proxy,
                 not a news timestamp.
  feature_ok     exhaustion was actually present (IEX saw %R).
  feature_blind  the heat gate was a listing filter that day.
  chase          name already +2% from the open by admission — we arrived late.
  fresh          |open→admission| < 1% — the move had not happened yet.
  high_rvol      rvol ≥ 2 and present.

Scoring is desk_null: eligible-within, vol-matched outside, IWM residual,
20 bps haircut vs cash. PASS requires n≥30, net median > 0, and
eligible-within paired median > 0 at ≥2σ. A green raw return is not a pass.

A PASS here is permission to sweep exits for THAT slice's horizon. It is
not a live config change. Config stays frozen until a forward paper test.

Read-only. Usage:
    python3 tools/thesis_screen.py [--days N] [--horizon-min N]
    python3 tools/thesis_screen.py --slices open_drive,fresh,research
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


def _rvol(tags) -> float:
    v = tags.get("rvol")
    return v if v is not None else 0.0


SLICES = {
    "all": ("every RTH admission — baseline, should track admission_null",
            lambda _t, _s: True),
    "open_drive": ("9:35–10:00",
                   lambda t, _s: t["tod"] == "open_drive"),
    "morning": ("10:00–12:00",
                lambda t, _s: t["tod"] == "morning"),
    "midday": ("12:00–14:00",
               lambda t, _s: t["tod"] == "midday"),
    "late": ("14:00–15:30 — honesty check, usually worse",
             lambda t, _s: t["tod"] == "late"),
    "research": ("on that day's research list or source=research",
                 lambda t, _s: t["research"]),
    "scanner": ("not on the research list",
                lambda t, _s: t["scanner"]),
    "champion": ("rank-1 idea that day — expect UNDERPOWERED",
                 lambda t, _s: t["champion"]),
    "catalyst_text": ("research write-up has catalyst language",
                      lambda t, _s: t["catalyst"]),
    "feature_ok": ("%R present — heat was actually computed",
                   lambda t, _s: t["feature_ok"]),
    "feature_blind": ("%R missing — IEX never saw the name",
                      lambda t, _s: not t["feature_ok"]),
    "chase": ("already +2% from the open at admission",
              lambda t, _s: t["chase"]),
    "fresh": ("|open→admission| < 1% — move had not happened",
              lambda t, _s: t["fresh"]),
    "high_rvol": ("rvol ≥ 2 and present",
                  lambda t, _s: _rvol(t) >= 2.0),
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--feed", choices=("sip", "iex"), default="sip")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--haircut", type=float, default=N.HAIRCUT_PCT)
    ap.add_argument("--bench", default=N.BENCH)
    ap.add_argument("--slices", type=str, default="",
                    help="comma-separated subset of slice names")
    ap.add_argument("--flatten-et", default="",
                    help="HH:MM ET cap so the window cannot run past EOD flatten "
                         "(live desk is 15:50). Empty = unclipped bars.")
    args = ap.parse_args()
    horizon = args.horizon_min * 60.0
    rng = random.Random(args.seed)
    flatten_min = None
    if args.flatten_et.strip():
        hh, mm = args.flatten_et.strip().split(":")
        flatten_min = int(hh) * 60 + int(mm)

    names = [n.strip() for n in args.slices.split(",") if n.strip()] or list(SLICES)
    bad = [n for n in names if n not in SLICES]
    if bad:
        print(f"unknown slices: {bad}; have {list(SLICES)}")
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
    admissions = N.collect_admissions(rows)
    print(f"{len(admissions)} admissions in RTH across {days[0]}..{days[-1]}, "
          f"horizon {args.horizon_min:g}m, haircut {args.haircut:.2f}%, "
          f"bench {args.bench}"
          + (f", flatten {args.flatten_et} ET" if flatten_min is not None else ""))
    if not admissions:
        print("EMPTY RUN — no RTH admissions; no verdict")
        return 1

    ctx = N.prepare_context(rows, days, args.feed, rng,
                            haircut=args.haircut, bench=args.bench,
                            flatten_min=flatten_min)
    research = N.load_research_by_day()
    n_research_days = sum(1 for d in days if research.get(d, {}).get("symbols"))
    print(f"research coverage: {n_research_days}/{len(days)} sessions have a list")

    tagged = []
    no_bars = 0
    for t0, sym, day, first in admissions:
        s = N.score_one(t0, sym, day, horizon, ctx)
        if s is None:
            no_bars += 1
            continue
        s["tags"] = N.tag_admission(first, s, research)
        tagged.append(s)
    print(f"scored {len(tagged)} ({no_bars} had no bars)")
    if not tagged:
        print("EMPTY RUN — nothing scored; no verdict")
        return 1

    ranked = []
    for name in names:
        desc, pred = SLICES[name]
        scores = [s for s in tagged if pred(s["tags"], s)]
        v = N.print_scorecard(scores, args.haircut, title=f"{name} — {desc}")
        ranked.append((name, v, len(scores)))

    print(f"\n{'slice':<16} {'verdict':<13} {'n':>5}")
    print("-" * 36)
    for name, v, n in ranked:
        print(f"{name:<16} {v:<13} {n:>5}")

    print("\nPASS is permission to sweep exits for that slice, not a live")
    print("config change. Config stays frozen. If every slice FAILs or is")
    print("UNDERPOWERED at 30m, re-run with --horizon-min 60 before killing")
    print("the family — then the honest fork is overnight (H3) or a slower")
    print("swing on a different universe (H4).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
