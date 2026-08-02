#!/usr/bin/env python3
"""End-of-day (or any-day) token / cost report from token_metrics.jsonl.

Usage (from trading-helper root)::

    python3 tools/token_report.py
    python3 tools/token_report.py --day 2026-08-02
    python3 tools/token_report.py --all
    python3 tools/token_report.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_suggest import TOKEN_METRICS_PATH, summarize_token_metrics  # noqa: E402


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"${x:,.4f}" if x < 1 else f"${x:,.2f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="AI desk token / cost report")
    ap.add_argument(
        "--day",
        default="today",
        help="ET calendar day YYYY-MM-DD, or 'today' (default)",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="All rows in the metrics file (ignores --day)",
    )
    ap.add_argument(
        "--path",
        type=Path,
        default=None,
        help=f"Override metrics path (default: {TOKEN_METRICS_PATH})",
    )
    ap.add_argument("--json", action="store_true", help="Print raw summary JSON")
    args = ap.parse_args()

    day = None if args.all else (args.day or "today")
    summary = summarize_token_metrics(args.path, day=day)

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return 0

    print(f"AI token metrics  day={summary.get('day')}  "
          f"file={summary.get('path')}")
    print(f"  calls:  {summary.get('count', 0)}")
    print(f"  cost:   {_fmt_usd(summary.get('total_cost_usd'))}")
    print(f"  tokens: in={summary.get('input_tokens', 0):,}  "
          f"out={summary.get('output_tokens', 0):,}  "
          f"cache_read={summary.get('cache_read_input_tokens', 0):,}  "
          f"cache_create={summary.get('cache_creation_input_tokens', 0):,}")

    by_phase = summary.get("by_phase") or {}
    if by_phase:
        print("  by phase:")
        for phase, slot in sorted(by_phase.items()):
            print(
                f"    {phase:12}  n={slot.get('n', 0):3}  "
                f"cost={_fmt_usd(slot.get('total_cost_usd'))}  "
                f"in={slot.get('input_tokens', 0):,}  "
                f"out={slot.get('output_tokens', 0):,}"
            )

    by_backend = summary.get("by_backend") or {}
    if by_backend:
        print("  by backend:")
        for backend, slot in sorted(by_backend.items()):
            print(
                f"    {backend:12}  n={slot.get('n', 0):3}  "
                f"cost={_fmt_usd(slot.get('total_cost_usd'))}"
            )

    last = summary.get("last") or {}
    if last:
        print(
            f"  last:   phase={last.get('phase')}  backend={last.get('backend')}  "
            f"model={last.get('model')}  cost={_fmt_usd(last.get('total_cost_usd'))}"
        )

    if not summary.get("count"):
        print("  (no rows — research must run with CLI --output-format json)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
