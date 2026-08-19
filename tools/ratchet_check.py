#!/usr/bin/env python3
"""Live check: dual-tranche T1 + stop ≥ entry; 1-share software shelf ≥ entry
once last − give has cleared the fill.

Usage (on the Mac mini, from the repo):

    python3 tools/ratchet_check.py
    python3 tools/ratchet_check.py --json

Exit 0 if every dual long is ok (or there are none). Exit 1 on any fail.
Does not place or cancel orders.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai_positions as cp  # noqa: E402
import alpaca_trader  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true", help="print rows as JSON")
    args = p.parse_args(argv)

    state = cp._load_state()
    detail = alpaca_trader.get_positions_detail() or {}
    detail_u = {str(k).upper(): v for k, v in detail.items()}
    try:
        orders = alpaca_trader.get_open_orders(limit=100) or []
    except Exception as e:
        print(f"open orders unavailable: {e}", file=sys.stderr)
        orders = []

    rows = cp.evaluate_ratchet_invariants(state, detail_u, orders)
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("no confirmed open names to check")
    else:
        for r in rows:
            mark = "OK" if r.get("ok") else "FAIL"
            print(
                f"{mark:4} {r.get('symbol'):6}  {r.get('event'):28}  "
                f"qty={r.get('live_qty')}  entry={r.get('entry')}  "
                f"stop={r.get('best_stop')}  t1={r.get('has_t1')}  "
                f"scaled={r.get('scaled')}"
            )

    fails = [r for r in rows if not r.get("ok")]
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
