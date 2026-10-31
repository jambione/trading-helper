#!/usr/bin/env python3
"""Diagnostic tool to inspect what candidate names were blocked today and why.

Usage:
    python3 tools/blocked_today.py
    python3 tools/blocked_today.py --date 2026-10-27
"""
import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
EVENTS_PATH = ROOT / "ai_reports" / "events.jsonl"
OUTCOMES_PATH = ROOT / "ai_reports" / "outcomes.jsonl"


def main():
    parser = argparse.ArgumentParser(description="Inspect blocked candidates for a given day.")
    parser.add_argument("--date", default=None, help="Date to inspect (YYYY-MM-DD), default is today.")
    args = parser.parse_args()

    now_et = datetime.now(ET)
    target_date = args.date or now_et.strftime("%Y-%m-%d")

    print(f"\n" + "=" * 95)
    print(f"{'CANDIDATE BLOCKER & GATE DIAGNOSTIC: ' + target_date:^95}")
    print("=" * 95)

    # 1. Collect trades that actually executed
    executed_trades = []
    if OUTCOMES_PATH.exists():
        with open(OUTCOMES_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    et = d.get("entry_time")
                    if et:
                        dt = datetime.fromtimestamp(et, ET)
                        if dt.strftime("%Y-%m-%d") == target_date:
                            executed_trades.append(d)
                except Exception:
                    pass

    # 2. Collect block and skip events
    blocks_by_sym = defaultdict(list)
    if EVENTS_PATH.exists():
        with open(EVENTS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    ts = d.get("ts")
                    if ts:
                        dt = datetime.fromtimestamp(ts, ET)
                        if dt.strftime("%Y-%m-%d") == target_date:
                            sym = d.get("symbol")
                            kind = d.get("kind")
                            reason = d.get("reason") or d.get("blocker") or d.get("block_code") or d.get("stage")
                            if sym and reason and kind in ("watch_skip", "watch_drop", "entry_skip", "arm_refuse", "entry_decision"):
                                hhmm = dt.strftime("%H:%M:%S")
                                blocks_by_sym[sym].append((hhmm, kind, reason))
                except Exception:
                    pass

    print(f"Executed Trades Today: {len(executed_trades)}")
    if executed_trades:
        for t in executed_trades:
            sym = t.get("symbol")
            en = t.get("entry_price")
            ex = t.get("exit_price") or t.get("exit_price_approx")
            pl = t.get("realized_pl_usd")
            r = t.get("realized_r_multiple")
            dt = datetime.fromtimestamp(t.get("entry_time"), ET).strftime("%H:%M:%S")
            print(f"  ✅ EXECUTED: {dt} {sym:<5} en=${en} ex=${ex} P&L=${pl} (R={r})")

    print(f"\nBlocked / Screened Candidates Today ({len(blocks_by_sym)} symbols):")
    print(f"{'Symbol':<7} | {'Last Block Time':<15} | {'Event Kind':<16} | {'Reason for Refusal / Block'}")
    print("-" * 95)

    if not blocks_by_sym:
        print("  No block/skip events recorded for this date.")
    else:
        for sym in sorted(blocks_by_sym.keys()):
            history = blocks_by_sym[sym]
            last_event = history[-1]
            hhmm, kind, reason = last_event
            # clean up reason
            r_clean = str(reason).replace("_", " ")
            print(f"{sym:<7} | {hhmm:<15} | {kind:<16} | {r_clean}")

    print("=" * 95 + "\n")


if __name__ == "__main__":
    main()
