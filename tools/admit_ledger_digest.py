#!/usr/bin/env python3
"""Digest admit_ledger refuses for one ET day (read-only).

Usage::

    .venv/bin/python tools/admit_ledger_digest.py --day 2026-09-16
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ET = ZoneInfo("America/New_York")
OUT_DIR = ROOT / "benchmarks" / "admit_ledger"


def _ledger_path(day: str) -> Path:
    try:
        from ai_paths import resolve_report_dir
        base = resolve_report_dir()
    except Exception:
        base = ROOT / "ai_reports"
    return base / "admit_ledger" / f"{day}.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--day",
        default=datetime.now(ET).strftime("%Y-%m-%d"),
        help="ET day YYYY-MM-DD (default: today ET)",
    )
    args = ap.parse_args()
    day = str(args.day).strip()
    path = _ledger_path(day)
    if not path.exists():
        print(f"missing {path}")
        return 1

    stage_n: Counter[str] = Counter()
    reason_n: Counter[tuple[str, str]] = Counter()
    syms: set[str] = set()
    n = 0
    for line in path.open(encoding="utf-8"):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        n += 1
        stage = str(e.get("stage") or "?")
        reason = str(e.get("reason") or "?")
        stage_n[stage] += 1
        reason_n[(stage, reason)] += 1
        sym = str(e.get("symbol") or "").upper()
        if sym:
            syms.add(sym)

    lines = [
        f"# Admit ledger digest — `{day}`",
        "",
        f"Lines: **{n}** · unique symbols: **{len(syms)}**",
        "",
        "## By stage",
        "",
    ]
    for stage, c in stage_n.most_common():
        lines.append(f"- `{stage}`: {c}")
    lines.extend(["", "## Top reasons (stage, reason)", ""])
    for (stage, reason), c in reason_n.most_common(25):
        lines.append(f"- `{stage}` / `{reason}`: {c}")
    lines.append("")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{day}_summary.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
