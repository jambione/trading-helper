#!/usr/bin/env python3
"""Disk and capture-size check (operator 11/06: will SIP data be more voluminous?). Read-only, no API.

Free space on the data volume, and the size of each day's recorded session folder (ai_reports/sessions/DAY) with its biggest files,
so the first SIP sessions can be compared with the IEX days around them. WARN below 25 GB free; CRITICAL below 12 GB.
    .venv/bin/python tools/disk_check.py [--days 12]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESS = ROOT / "ai_reports" / "sessions"
WARN_GB, CRIT_GB = 25.0, 12.0


def dir_mb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6


def level(free_gb: float) -> str:
    return "CRITICAL" if free_gb < CRIT_GB else ("WARN" if free_gb < WARN_GB else "ok")


def day_rows(n: int) -> list[tuple[str, float, dict]]:
    out = []
    for d in sorted(p for p in SESS.glob("20??-??-??") if p.is_dir())[-n:]:
        files = {f.name.replace(".jsonl.gz", ""): f.stat().st_size / 1e6 for f in d.iterdir() if f.is_file()}
        out.append((d.name, sum(files.values()), files))
    return out


def brief(n: int = 12) -> list[str]:
    path = "/System/Volumes/Data" if os.path.exists("/System/Volumes/Data") else str(ROOT)
    u = shutil.disk_usage(path)
    free = u.free / 1e9
    lines = ["## Disk and capture size", "", f"Free {free:.0f} GB of {u.total / 1e9:.0f} GB ({level(free)}; warn < {WARN_GB:.0f} GB, critical < {CRIT_GB:.0f} GB).", ""]
    rows = day_rows(n)
    if rows:
        lines += ["| session day | total MB | wire | prints | decisions | inputs |", "|---|---|---|---|---|---|"]
        for d, tot, f in reversed(rows):
            lines.append(f"| {d} | {tot:.0f} | {f.get('wire', 0):.0f} | {f.get('prints', 0):.0f} | {f.get('decisions', 0):.0f} | {f.get('inputs', 0):.0f} |")
        full = [r[1] for r in rows if r[1] > 100]
        if full:
            lines += ["", f"Average full session {sum(full) / len(full):.0f} MB; at that rate the free space lasts about {free * 1e3 / (sum(full) / len(full)) / 1.6:.0f} sessions "
                      "(x1.6 for the other daily files: ledgers, events, shadow)."]
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=12)
    print("\n".join(brief(ap.parse_args(argv).days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
