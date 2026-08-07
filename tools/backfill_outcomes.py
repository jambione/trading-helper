#!/usr/bin/env python3
"""backfill_outcomes.py — repair outcome rows whose exit was never resolved.

The desk used to recognise only the exits it placed itself, so a
hand-liquidated position, an EOD flatten, or a bracket leg whose id was lost to
a restart all landed as exit_price=null, realized_r=null, close_reason
"stopped_out". That last field was a default, not an observation: on 2026-08-07
all four rows said stopped_out and not one exit was within 2% of its stop.

The fills were in the broker's history the whole time. This walks outcomes.jsonl
and, for any row missing an exit price, matches the closing SELL fill by symbol
and by "after this position's entry_time" — so an earlier round trip in the same
name cannot price a later one — then recomputes realized R and P&L and relabels
the reason from the order TYPE.

HONESTY
  A repaired row is reconstructed, not observed. Every row this touches is
  stamped `backfilled: true` with `backfill_ts`, so anything reading
  outcomes.jsonl can tell the two apart and exclude them from a live-recording
  audit. Rows that already carry an exit price are never modified.

  Dry-run by default. Writes a .bak beside the file before changing anything.

USAGE
    .venv/bin/python tools/backfill_outcomes.py            # show what it would do
    .venv/bin/python tools/backfill_outcomes.py --apply    # write it
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _fill_ts(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _reason_from_type(otype: str, scaled_out: bool) -> str:
    t = (otype or "").lower()
    if "stop" in t:
        return "trailed_out" if scaled_out else "stopped_out"
    if "limit" in t:
        return "target_hit"
    if "market" in t:
        return "flattened"
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the repaired file (default: dry run)")
    ap.add_argument("--days", type=int, default=7,
                    help="how far back to pull broker fills (default 7)")
    args = ap.parse_args()

    import ai_paths
    path = ai_paths.resolve_report_dir() / "outcomes.jsonl"
    if not path.exists():
        print(f"no outcomes file at {path}")
        return 1

    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                print("! skipping unparseable line")
    print(f"{len(rows)} outcome row(s) in {path}")

    import ai_trading as gt
    gt.init_for_ai()
    import alpaca_trader as at
    fills = [f for f in (at.get_filled_orders(limit=500, days=args.days) or [])
             if str(f.get("side") or "").lower() == "sell"]
    print(f"{len(fills)} sell fill(s) from the broker\n")

    repaired = 0
    for r in rows:
        if r.get("exit_price") is not None:
            continue
        sym = str(r.get("symbol") or "").upper()
        entry_ts = r.get("entry_time")
        best, best_ts = None, -1.0
        for f in fills:
            if str(f.get("symbol") or "").upper() != sym:
                continue
            ts = _fill_ts(f.get("filled_at"))
            if entry_ts and ts and ts < float(entry_ts):
                continue
            if ts is not None and ts > best_ts:
                best, best_ts = f, ts
        if not best:
            print(f"  {sym:6s} no matching fill — left as unknown")
            continue

        px = float(best.get("filled_avg_price") or 0) or None
        if not px:
            continue
        entry = r.get("entry_price") or 0
        stop = r.get("stop_price") or 0
        qty = r.get("total_qty") or 0
        risk = entry - stop
        was = r.get("close_reason")
        reason = _reason_from_type(best.get("type"), bool(r.get("scaled_out")))

        r["exit_price"] = px
        r["realized_r_multiple"] = ((px - entry) / risk) if (entry and risk > 0) else None
        r["realized_pl_usd"] = ((px - entry) * qty) if (entry and qty) else None
        r["close_reason"] = reason
        r["backfilled"] = True
        r["backfill_ts"] = time.time()
        repaired += 1
        print(f"  {sym:6s} exit={px:<8} R={r['realized_r_multiple']:+.2f} "
              f"pl={r['realized_pl_usd']:+.2f}  reason: {was} -> {reason}")

    if not repaired:
        print("\nnothing to repair.")
        return 0

    print(f"\n{repaired} row(s) repairable.")
    if not args.apply:
        print("dry run — re-run with --apply to write.")
        return 0

    backup = path.with_suffix(".jsonl.bak")
    shutil.copy2(path, backup)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print(f"wrote {path}  (backup: {backup})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
