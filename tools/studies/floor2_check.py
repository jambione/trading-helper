#!/usr/bin/env python3
"""floor2_short pre-check: do the recorded source lists hold sub-$10 names a $2 floor could seat?

Scans ~/session_snapshots/DAY/state_snapshots.jsonl.gz for every recorded file and counts the distinct symbols
priced $2-10 per file (any dict with a symbol/ticker and a price/last/close field). Reads local files only.
  .venv/bin/python tools/studies/floor2_check.py 2026-10-06 2026-10-16
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(os.getenv("SESSION_SNAPSHOT_DIR") or Path.home() / "session_snapshots")
SYM_KEYS, PX_KEYS = ("symbol", "ticker", "sym"), ("price", "last", "close", "last_price", "px")


def walk(x, out: set):
    if isinstance(x, dict):
        s = next((x[k] for k in SYM_KEYS if isinstance(x.get(k), str)), None)
        p = next((x[k] for k in PX_KEYS if isinstance(x.get(k), (int, float))), None)
        if s and p is not None and 2.0 <= p < 10.0:
            out.add(s.upper())
        for v in x.values():
            walk(v, out)
    elif isinstance(x, list):
        for v in x:
            walk(v, out)


def scan(day: str) -> dict[str, set]:
    p = BASE / day / "state_snapshots.jsonl.gz"
    got: dict[str, set] = defaultdict(set)
    if not p.exists():
        return {}
    with gzip.open(p, "rt") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            walk(r.get("data"), got[str(r.get("file"))])
    return got


def main() -> None:
    days = sys.argv[1:] or ["2026-10-06"]
    for d in days:
        got = scan(d)
        if not got:
            print(f"{d}: no recording")
            continue
        print(f"{d}: " + ", ".join(f"{f} {len(s)}" for f, s in sorted(got.items()) if s) or f"{d}: none under $10")
        allsyms = sorted(set().union(*got.values()))
        print(f"   sub-$10 names ({len(allsyms)}): {' '.join(allsyms[:40])}")


if __name__ == "__main__":
    main()
