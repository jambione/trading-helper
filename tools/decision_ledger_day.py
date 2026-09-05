#!/usr/bin/env python3
"""Summarize one ET day of the Package 1 decision ledger.

    .venv/bin/python tools/decision_ledger_day.py
    .venv/bin/python tools/decision_ledger_day.py --day 2026-09-05
    .venv/bin/python -m tools.decision_ledger_day --day 2026-09-05

Prints bucket histogram + top symbols stuck in readiness / heat / exh
for the 09:30–11:00 ET morning window (arm-stage rows only).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from ai_paths import resolve_report_dir  # noqa: E402
from desk_arm_buckets import BUCKETS, bucket_label  # noqa: E402

ET = ZoneInfo("America/New_York")


def _day_path(day: str) -> Path:
    return resolve_report_dir() / "decision_ledger" / f"{day}.jsonl"


def _mins(dt: datetime) -> float:
    return dt.hour * 60 + dt.minute + dt.second / 60.0


def _load(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if isinstance(r, dict):
                rows.append(r)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--day",
        default=datetime.now(tz=ET).strftime("%Y-%m-%d"),
        help="ET calendar day YYYY-MM-DD (default: today)",
    )
    ap.add_argument(
        "--path",
        default=None,
        help="Override ledger JSONL path",
    )
    args = ap.parse_args()
    path = Path(args.path) if args.path else _day_path(args.day)
    rows = _load(path)
    if not rows:
        print(f"no rows in {path}")
        return 1

    morn: list[dict] = []
    for r in rows:
        try:
            ts = float(r.get("ts"))
        except (TypeError, ValueError):
            continue
        dt = datetime.fromtimestamp(ts, tz=ET)
        if 9 * 60 + 30 <= _mins(dt) <= 11 * 60:
            morn.append(r)

    arm_rows = [
        r for r in morn
        if str(r.get("stage") or "") in ("arm", "watch")
        and r.get("arm_ok") is not True
    ]
    hist = Counter(
        str(r.get("arm_bucket") or "other") for r in arm_rows
    )

    print(f"decision ledger  day={args.day}  file={path}")
    print(f"rows_total={len(rows)}  morn_arm_refused={len(arm_rows)}  (09:30–11 ET)")
    print("\nbucket histogram (morning refusals):")
    for b in BUCKETS:
        n = hist.get(b, 0)
        if n:
            print(f"  {b:12} {n:6}  {bucket_label(b)}")
    for b, n in hist.most_common():
        if b not in BUCKETS:
            print(f"  {b:12} {n:6}")

    def _stuck(bucket: str, top: int = 8) -> None:
        by_sym: Counter[str] = Counter()
        for r in arm_rows:
            if str(r.get("arm_bucket") or "") != bucket:
                continue
            sym = str(r.get("symbol") or "")
            if sym:
                by_sym[sym] += 1
        if not by_sym:
            print(f"\n{bucket}: (none)")
            return
        print(f"\ntop stuck in {bucket}:")
        for sym, n in by_sym.most_common(top):
            # last why for that symbol
            last_why = ""
            for r in reversed(arm_rows):
                if r.get("symbol") == sym and r.get("arm_bucket") == bucket:
                    last_why = str(r.get("arm_why") or "")
                    break
            print(f"  {sym:6} n={n:4}  last_why={last_why}")

    for b in ("readiness", "heat", "exh", "macd_dir", "rsi"):
        _stuck(b)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
