#!/usr/bin/env python3
"""Move the desk's audit trail from claude_reports/ into ai_reports/.

Commit 851279d finished the claude_* -> ai_* rename and dropped the compat
resolver, so every path now binds to ``ai_reports/``. The DATA was not moved
with it. Where that is true, the effect is silent and total:

    performance_summary() reads a file that does not exist and reports
    "No graded closed positions yet" — win rate, realized R and the
    by_close_reason breakdown all read as though the desk had never traded.

realized_r_today() reads the same file, and it gates new entries through
ai_daily_loss_limit_r — so a stranded trail also means the daily-loss limit
starts every session from zero regardless of what actually happened.

Dry run by default. Nothing is overwritten: a name already present in
ai_reports/ is reported and left alone for a human to reconcile.

    python3 scripts/migrate_report_dir.py            # show the plan
    python3 scripts/migrate_report_dir.py --apply    # do it
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD = ROOT / "claude_reports"
NEW = ROOT / "ai_reports"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually move the files (default: dry run)")
    args = ap.parse_args()

    if not OLD.exists():
        print(f"nothing to do — {OLD.name}/ does not exist")
        return 0

    items = sorted(p for p in OLD.iterdir() if not p.name.startswith("."))
    if not items:
        print(f"nothing to do — {OLD.name}/ is empty")
        return 0

    moves: list[tuple[Path, Path]] = []
    clashes: list[Path] = []
    for src in items:
        dst = NEW / src.name
        (clashes if dst.exists() else moves).append(
            dst if dst.exists() else (src, dst))  # type: ignore[arg-type]

    verb = "moving" if args.apply else "would move"
    print(f"{OLD} -> {NEW}")
    print(f"{verb} {len(moves)} item(s):")
    for src, dst in moves:  # type: ignore[misc]
        size = src.stat().st_size if src.is_file() else 0
        kind = "dir " if src.is_dir() else f"{size:>9,}b"
        print(f"  {kind}  {src.name}")

    if clashes:
        print(f"\n{len(clashes)} already present in {NEW.name}/ — LEFT ALONE, "
              f"reconcile by hand:")
        for dst in clashes:
            print(f"  {dst.name}")

    if not args.apply:
        print("\ndry run — re-run with --apply to move them")
        return 0

    NEW.mkdir(parents=True, exist_ok=True)
    for src, dst in moves:  # type: ignore[misc]
        shutil.move(str(src), str(dst))
    print(f"\nmoved {len(moves)} item(s)")

    # Prove the trail is readable again rather than asserting it.
    sys.path.insert(0, str(ROOT))
    try:
        import ai_positions
        s = ai_positions.performance_summary()
        print(f"performance_summary() now sees {s.get('count', 0)} graded "
              f"and {s.get('ungraded', 0)} ungraded closed trade(s)")
    except Exception as e:  # noqa: BLE001
        print(f"could not verify: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
