#!/usr/bin/env python3
"""exit_report.py — the sell side's counterpart to shadow_report.py.

The buy side samples every candidate on every poll and records why it did not
arm, which is what makes a gate's cost measurable. Until 2026-08-07 a position
left the telemetry the instant it opened — 4099 shadow rows that day, every one
status=watching — so "should we have sold there?" had nothing behind it but a
terminal outcome row. This reads position_shadow.jsonl, which logs every open
position on every tick INCLUDING the quiet ones.

SECTIONS
  1. HOLDS            how long positions were held, and what the exit
                      machinery decided each tick. "hold" dominating is
                      expected; what matters is whether anything else ever
                      fires, because a mechanism that never fires is untested
                      rather than working.

  2. EXCURSIONS       MAE/MFE in R — the worst and best the trade ever got to.
                      Unreconstructable from an outcome row, which knows only
                      entry and exit. This is what says a stop was too tight
                      (deep MAE on winners) or a target left money behind
                      (MFE far above the exit).

  3. SELL SIGNAL      what the indicators said while the desk held, and what
                      price did next. The entry gate refuses names flagged
                      sell; this asks whether that flag is worth acting on
                      once already in.

HONESTY
  Same rules as the sibling tools. Missing is not zero, small samples are
  labelled underpowered rather than averaged into a verdict, and none of this
  is randomized — the desk chose what it held. Findings are hypotheses for
  tools/ab_bench.py.

USAGE
    .venv/bin/python tools/exit_report.py
    .venv/bin/python tools/exit_report.py --all-days
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ET = ZoneInfo("America/New_York")


def _day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=ET).strftime("%Y-%m-%d")


def _load(path: Path, day: str | None) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if day and _day(r.get("ts", 0)) != day:
            continue
        rows.append(r)
    return rows


def _fmt(v, nd=2, suffix=""):
    return "—" if v is None else f"{v:.{nd}f}{suffix}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all-days", action="store_true")
    ap.add_argument("--day", default=None, help="ET date, YYYY-MM-DD")
    args = ap.parse_args()

    import ai_paths
    path = ai_paths.resolve_report_dir() / "position_shadow.jsonl"
    day = None if args.all_days else (args.day or _day(time.time()))
    rows = _load(path, day)

    label = "ALL DAYS" if args.all_days else (day or "")
    print("=" * 66)
    print(f"  EXIT REPORT — {label}")
    print("=" * 66)
    if not rows:
        print(f"\n  no rows in {path}")
        print("  Nothing was held, or ai_position_shadow_enabled is off.")
        print("  A day with no positions is not a failure — but a day WITH")
        print("  positions and no rows means the exit side is unmeasured.\n")
        return 0

    # ── group into per-position episodes ──────────────────────────────────
    eps: dict[tuple, list] = defaultdict(list)
    for r in rows:
        eps[(r.get("symbol"), round(r.get("ts", 0) - (r.get("hold_sec") or 0)))
            ].append(r)
    for v in eps.values():
        v.sort(key=lambda x: x.get("ts", 0))

    print(f"\n  ticks logged      {len(rows)}")
    print(f"  positions held    {len(eps)}")

    # ── 1. HOLDS ──────────────────────────────────────────────────────────
    print("\n1. HOLDS  (what the exit machinery decided, per tick)")
    for why, n in Counter(r.get("exit_why") for r in rows).most_common():
        print(f"   {str(why):26s} {n:6d}")
    fired = {w for w in (r.get("exit_why") for r in rows) if w and w != "hold"}
    if not fired:
        print("   -> nothing but 'hold' fired. Every exit mechanism on this")
        print("      desk is UNTESTED, not working. A stop that never")
        print("      triggered has not been shown to trigger.")

    holds = [max(r.get("hold_sec") or 0 for r in v) for v in eps.values()]
    if holds:
        print(f"\n   hold time  median {statistics.median(holds)/60:.1f}m"
              f"   max {max(holds)/60:.1f}m")

    # ── 2. EXCURSIONS ─────────────────────────────────────────────────────
    print("\n2. EXCURSIONS  (R reached while held — not reconstructable later)")
    print(f"   {'symbol':8s} {'MAE':>7s} {'MFE':>7s} {'last':>7s} {'held':>7s}")
    mae_all, mfe_all = [], []
    for (sym, _), v in sorted(eps.items()):
        last = v[-1]
        mae, mfe = last.get("mae_r"), last.get("mfe_r")
        if mae is not None:
            mae_all.append(mae)
        if mfe is not None:
            mfe_all.append(mfe)
        print(f"   {str(sym):8s} {_fmt(mae):>7s} {_fmt(mfe):>7s} "
              f"{_fmt(last.get('unrealized_r')):>7s} "
              f"{(last.get('hold_sec') or 0)/60:6.1f}m")
    if len(mae_all) < 5:
        print(f"   [UNDERPOWERED] n={len(mae_all)} — descriptive only")
    elif mae_all and mfe_all:
        print(f"\n   median MAE {statistics.median(mae_all):+.2f}R"
              f"   median MFE {statistics.median(mfe_all):+.2f}R")
        deep = [m for m in mae_all if m <= -0.8]
        if deep:
            print(f"   {len(deep)}/{len(mae_all)} came within 0.2R of the stop"
                  " — tighter stops would have cut them")

    # ── 3. SELL SIGNAL ────────────────────────────────────────────────────
    print("\n3. SELL SIGNAL WHILE HELD")
    with_ind = [r for r in rows if r.get("has_indicators")]
    print(f"   ticks with indicator data   {len(with_ind)}/{len(rows)}"
          f"  ({100*len(with_ind)//max(1,len(rows))}%)")
    if not with_ind:
        print("   -> the desk held blind. sell_signal cannot defend a")
        print("      position whose indicators it never sees; check that held")
        print("      names are exempt from the momentum ticker eviction.")
    else:
        flagged = [r for r in with_ind if r.get("sell_signal")]
        print(f"   ticks flagged sell          {len(flagged)}")
        for name in ("cm_ok", "pctr_ok", "cm_rsi_rising"):
            t = sum(1 for r in with_ind if r.get(name))
            print(f"   {name:26s} true on {t}/{len(with_ind)}")

    print("\n  None of this is randomized — the desk chose what it held.")
    print("  Hypotheses for tools/ab_bench.py, not verdicts.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
