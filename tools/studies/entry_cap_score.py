"""Scratch counts for the entry-cap arm. Does not score VALUE.

VALUE is tools/studies/spread_rvol_score.py on each half directory.
This script only checks that the 16 day files exist and counts scratches.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DAYS_A = ("2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09")
DAYS_B = ("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15")
ARMS = ("trail_dc", "trail_dc_entrycap")


def is_scratch(trade: dict) -> bool:
    entry = trade.get("entry_px")
    exit_px = trade.get("exit_px")
    if entry is None or exit_px is None:
        return False
    entry_f = float(entry)
    if entry_f == 0 or abs(float(exit_px) - entry_f) > 0.01 + 1e-9:
        return False
    peak = trade.get("peak_px")
    if peak is None:
        return True
    return (float(peak) - entry_f) / entry_f < 0.0015


def day_problem(directory: Path, day: str) -> str | None:
    closed = []
    for arm in ARMS:
        path = directory / f"{day}_{arm}.json"
        if not path.is_file():
            return "missing"
        doc = json.loads(path.read_text())
        closed.append(doc.get("closed") or [])
    if all(len(rows) == 0 for rows in closed):
        return "vacuous"
    return None


def scratch_counts(directory: Path, days: tuple[str, ...]) -> dict[str, int]:
    counts = {arm: 0 for arm in ARMS}
    for day in days:
        for arm in ARMS:
            doc = json.loads((directory / f"{day}_{arm}.json").read_text())
            counts[arm] += sum(1 for t in (doc.get("closed") or []) if is_scratch(t))
    return counts


def main() -> None:
    if "--check" not in sys.argv:
        raise SystemExit("refusing to read replays without --check")
    directory = Path(sys.argv[sys.argv.index("--check") + 1])
    problems = []
    for day in DAYS_A + DAYS_B:
        why = day_problem(directory, day)
        if why:
            problems.append(f"{day} {why}")
    if problems:
        print("INCOMPLETE " + ", ".join(problems))
        raise SystemExit(1)
    for name, days in (("A", DAYS_A), ("B", DAYS_B)):
        counts = scratch_counts(directory, days)
        print(
            f"half {name} scratches trail_dc {counts['trail_dc']} "
            f"entrycap {counts['trail_dc_entrycap']}"
        )


if __name__ == "__main__":
    main()
