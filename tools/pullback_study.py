#!/usr/bin/env python3
"""
pullback_study.py — is the entry zone actually reachable?

The desk draws a pullback zone roughly ai_watch_zone_offset_pct under the last
print and waits for price to come back to it. On 2026-08-06 it drew 22 zones
and price reached 4 of them; the arm gate was therefore only ever ASKED 4
times. Before asking whether the arm conditions are too strict, the prior
question is whether the entry model waits for something that happens.

This measures, per admission episode, how deep price actually pulled back
against how deep the zone required — and then, for a range of hypothetical
depths, how many episodes each would have reached. That is the reachability
curve: it says what a shallower or deeper zone would have bought you, using
only prices the desk really observed.

WHAT IT DOES NOT SAY
    Reaching a zone is not a fill, and a fill is not a profit. A shallower
    zone is touched more often precisely because it demands less evidence of a
    pullback, so it will also enter on names that were never going to turn.
    This ranks REACHABILITY only. Whether the trades are worth taking is a
    separate question for tools/shadow_report.py (forward returns) and
    ultimately tools/ab_bench.py.

    The anchor is the first price observed with a zone attached, not the true
    session high, so depths are measured from where the desk started watching.

USAGE
    venv/bin/python tools/pullback_study.py
    venv/bin/python tools/pullback_study.py --all-days --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from ai_paths import find_report_file, resolve_report_dir  # noqa: E402

# Resolve the way the desk writes, via ai_paths — a hardcoded path silently
# freezes after a migration instead of failing.
SHADOW = (find_report_file("shadow.jsonl")
          or resolve_report_dir() / "shadow.jsonl")

# Depths to test, in percent below the anchor.
DEPTHS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]


def load(path: Path, day: date | None) -> list[dict]:
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not isinstance(r, dict) or r.get("price") is None:
            continue
        if day is not None:
            try:
                if datetime.fromtimestamp(r.get("ts") or 0).date() != day:
                    continue
            except Exception:
                continue
        out.append(r)
    return out


def episodes(rows: list[dict]) -> list[dict]:
    """One record per (symbol, admit_ts): anchor, depth reached, depth required."""
    by: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by[(r["symbol"], r.get("admit_ts"))].append(r)

    out = []
    for (sym, _admit), series in by.items():
        series.sort(key=lambda r: r.get("ts") or 0)
        # Anchor on the first sample that actually had a zone — before that the
        # desk had nothing to be reached.
        first_z = next((r for r in series if r.get("entry_high")), None)
        if not first_z:
            continue
        anchor = float(first_z["price"])
        if anchor <= 0:
            continue
        after = [r for r in series if (r.get("ts") or 0) >= (first_z.get("ts") or 0)]
        lows = [float(r["price"]) for r in after if r.get("price")]
        if not lows:
            continue
        low = min(lows)
        zone_top = float(first_z["entry_high"])

        out.append({
            "symbol": sym,
            "samples": len(after),
            "minutes": round(((after[-1].get("ts") or 0)
                              - (first_z.get("ts") or 0)) / 60.0, 1),
            "anchor": round(anchor, 4),
            "zone_top": round(zone_top, 4),
            # How far below the anchor the zone demanded price come.
            "required_depth_pct": round((anchor - zone_top) / anchor * 100.0, 3),
            # How far it actually went.
            "reached_depth_pct": round((anchor - low) / anchor * 100.0, 3),
            "touched": any(r.get("in_zone") for r in after),
            "source": first_z.get("source"),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=str(SHADOW))
    ap.add_argument("--all-days", action="store_true")
    ap.add_argument("--min-minutes", type=float, default=5.0,
                    help="ignore episodes watched for less than this (default 5)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    day = None if args.all_days else date.today()
    eps = episodes(load(Path(args.path), day))
    # An episode watched for two minutes had no chance to pull back; counting
    # it as "did not reach" would understate every depth.
    watched = [e for e in eps if e["minutes"] >= args.min_minutes]

    curve = []
    for d in DEPTHS:
        n = sum(1 for e in watched if e["reached_depth_pct"] >= d)
        curve.append({
            "depth_pct": d,
            "reached": n,
            "pct_of_episodes": (round(100.0 * n / len(watched), 1)
                                if watched else None),
        })

    req = [e["required_depth_pct"] for e in watched]
    reach = [e["reached_depth_pct"] for e in watched]
    report: dict[str, Any] = {
        "day": str(day) if day else "all",
        "episodes": len(eps),
        "episodes_watched_enough": len(watched),
        "min_minutes": args.min_minutes,
        "zone_required_depth_median_pct": (round(statistics.median(req), 3)
                                           if req else None),
        "actual_depth_median_pct": (round(statistics.median(reach), 3)
                                    if reach else None),
        "actual_depth_max_pct": round(max(reach), 3) if reach else None,
        "touched": sum(1 for e in watched if e["touched"]),
        "curve": curve,
        "episodes_detail": sorted(watched,
                                  key=lambda e: -e["reached_depth_pct"]),
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return

    print(f"\nPULLBACK REACHABILITY — {report['day']}")
    print(f"  episodes with a zone      : {report['episodes']}")
    print(f"  watched >= {args.min_minutes:.0f}m (usable) : "
          f"{report['episodes_watched_enough']}")
    if not watched:
        print("\n  Not enough observation yet.")
        return
    print(f"  zone demanded (median)    : "
          f"{report['zone_required_depth_median_pct']:.2f}% below anchor")
    print(f"  price actually fell (med) : "
          f"{report['actual_depth_median_pct']:.2f}%")
    print(f"  deepest pullback seen     : "
          f"{report['actual_depth_max_pct']:.2f}%")
    print(f"  zones actually touched    : {report['touched']}"
          f"/{len(watched)}")

    print("\n  If the zone sat this far below the anchor:")
    print(f"    {'depth':>7}{'reached':>10}{'of episodes':>14}")
    for c in curve:
        bar = "#" * int((c["pct_of_episodes"] or 0) / 5)
        print(f"    {c['depth_pct']:>6.2f}%{c['reached']:>10}"
              f"{(c['pct_of_episodes'] or 0):>13.0f}%  {bar}")

    print("\n  Deepest pullbacks observed:")
    for e in report["episodes_detail"][:8]:
        mark = "TOUCHED" if e["touched"] else ""
        print(f"    {e['symbol']:<6} fell {e['reached_depth_pct']:>6.2f}%  "
              f"(zone wanted {e['required_depth_pct']:>5.2f}%)  "
              f"{e['minutes']:>5.0f}m  {mark}")

    print("\n  Reaching a zone is not a fill and a fill is not a profit —"
          "\n  a shallower zone is touched more often because it demands less"
          "\n  evidence. Score any candidate depth in tools/shadow_report.py"
          "\n  before believing it.\n")


if __name__ == "__main__":
    main()
