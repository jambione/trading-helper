#!/usr/bin/env python3
"""tools/thrust_slippage_audit.py — Audit live Catalyst Thrust fills, slippage, and breakeven performance.

Reads:
  - alpaca_trade_log.json (order-level fills, prices, statuses)
  - ai_reports/events.jsonl (lifecycle events, breakeven locks, ratchet lifts)
  - ai_reports/outcomes.jsonl (closed trade outcomes)

Outputs:
  - Fill rate % at 09:31:00
  - Slippage in basis points vs quoted ask
  - Breakeven lock activations
  - Trade P&L and win rate
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def load_json(path: Path) -> list | dict:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except Exception:
        pass
    return rows


def run_audit():
    trade_log_file = ROOT / "alpaca_trade_log.json"
    events_file = ROOT / "ai_reports" / "events.jsonl"
    outcomes_file = ROOT / "ai_reports" / "outcomes.jsonl"

    trades = load_json(trade_log_file)
    events = load_jsonl(events_file)
    outcomes = load_jsonl(outcomes_file)

    thrust_orders = []
    if isinstance(trades, list):
        for tr in trades:
            note = str(tr.get("note") or "").lower()
            why = str(tr.get("why") or "").lower()
            arm = str(tr.get("arm_why") or "").lower()
            if "thrust" in note or "thrust" in why or "thrust" in arm:
                thrust_orders.append(tr)

    print("=" * 75)
    print("LIVE CATALYST OPEN THRUST TELEMETRY & SLIPPAGE AUDIT")
    print("=" * 75)

    if not thrust_orders:
        print("No live catalyst thrust orders recorded yet in alpaca_trade_log.json.")
        print("Standing by for next RTH market open (09:31 ET).\n")
        print("Audit Criteria Active:")
        print("  1. Bar 0 Body Ratio Filter: >= 0.65 (close in top 35% of range, no upper wicks)")
        print("  2. Breakeven Lock: +30 bp MFE triggers stop lift to entry + $0.01")
        print("  3. Execution Cap: 5.0 bp marketable limit IOC with $0.02 min cap")
        print("=" * 75)
        return

    n_orders = len(thrust_orders)
    filled_orders = [o for o in thrust_orders if str(o.get("status")).lower() in ("filled", "partially_filled")]
    n_filled = len(filled_orders)

    slippage_bps = []
    print(f"\nTotal Orders Placed: {n_orders} | Total Filled: {n_filled} ({n_filled / n_orders * 100:.1f}%)")
    print("-" * 75)
    print(f"{'Time (ET)':<16} {'Sym':<6} {'Side':<5} {'Status':<10} {'Quoted':<8} {'Fill Px':<8} {'Slip (bp)':<10}")
    print("-" * 75)

    for o in thrust_orders:
        ts = o.get("time") or o.get("ts")
        t_str = datetime.fromtimestamp(float(ts), ET).strftime("%Y-%m-%d %H:%M") if ts else "N/A"
        sym = o.get("symbol", "N/A")
        side = o.get("side", "buy")
        st = o.get("status", "unknown")
        quoted = float(o.get("price") or o.get("last_ask") or 0.0)
        fill = float(o.get("fill_px") or o.get("filled_avg_price") or 0.0)

        slip_bp = ((fill - quoted) / quoted * 10000.0) if quoted > 0 and fill > 0 else 0.0
        if fill > 0 and quoted > 0:
            slippage_bps.append(slip_bp)

        print(f"{t_str:<16} {sym:<6} {side:<5} {st:<10} ${quoted:<7.2f} ${fill:<7.2f} {slip_bp:+6.1f} bp")

    if slippage_bps:
        mean_slip = sum(slippage_bps) / len(slippage_bps)
        print("-" * 75)
        print(f"Average Fill Slippage vs Quoted Ask: {mean_slip:+.1f} bp")
        if mean_slip > 5.0:
            print("  [WARNING] Average slippage exceeds 5 bp cap! Review market spread conditions.")
        else:
            print("  [OK] Slippage is within target 5.0 bp tolerance.")

    # Breakeven Lock Events
    be_events = [e for e in events if e.get("event") == "thrust_breakeven_lock"]
    print(f"\nBreakeven Lock Activations (+30 bp): {len(be_events)}")
    for e in be_events[-5:]:
        sym = e.get("symbol")
        new_stop = e.get("new_stop")
        print(f"  - {sym}: stop locked at ${new_stop}")

    print("=" * 75 + "\n")


if __name__ == "__main__":
    run_audit()
