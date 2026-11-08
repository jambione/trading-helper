#!/usr/bin/env python3
"""Simulate marketable limit slippage cap across out-of-sample historical trade outcomes."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
import numpy as np

ROOT = Path(os.environ.get("REPO") or Path(__file__).resolve().parents[2])


def load_trades_for_period(start_date: str, end_date: str, outcomes_file: Path | None = None) -> list[dict]:
    outcomes_path = outcomes_file or (ROOT / "ai_reports" / "outcomes.jsonl")
    if not outcomes_path.exists():
        print(f"Error: {outcomes_path} not found")
        return []

    trades = []
    with open(outcomes_path) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue

            ts = r.get("entry_time") or r.get("ts")
            if not ts:
                continue
            d = datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
            if not (start_date <= d <= end_date):
                continue

            fill = r.get("entry_price")
            ask = (r.get("features") or {}).get("ask")
            slip_r = r.get("entry_slippage_r")
            r_mult = r.get("realized_r_multiple")
            pl_usd = r.get("realized_pl_usd")

            if r_mult is None or pl_usd is None or fill is None or fill <= 0:
                continue

            # Calculate entry slippage in basis points
            if ask and ask > 0 and abs(fill - ask) / ask < 0.20:
                slip_bp = (float(fill) - float(ask)) / float(ask) * 10000.0
            elif slip_r is not None:
                slip_bp = float(slip_r) * 500.0
            else:
                slip_bp = 0.0

            # Daily net bp proxy: realized_r * risk_bp, or direct pl / budget
            # Standard risk is 60 bp (0.6%) to 100 bp (1.0%).
            # We can compute return in basis points: pl_usd / (fill * qty) * 10000
            qty = r.get("total_qty") or 0.0
            if qty > 0 and fill > 0:
                trade_notional = float(fill) * float(qty)
                net_bp = float(pl_usd) / trade_notional * 10000.0
            else:
                net_bp = float(r_mult) * 60.0  # standard 60 bp R unit proxy

            trades.append({
                "date": d,
                "symbol": r.get("symbol"),
                "fill": float(fill),
                "ask": float(ask) if ask else float(fill),
                "slip_bp": float(slip_bp),
                "r_mult": float(r_mult),
                "pl_usd": float(pl_usd),
                "net_bp": float(net_bp),
                "close_reason": r.get("close_reason"),
            })

    return sorted(trades, key=lambda x: (x["date"], x["symbol"]))


def run_simulation(trades: list[dict], cap_bp: float = 5.0) -> dict:
    if not trades:
        return {}

    n_total = len(trades)
    base_pl = [t["pl_usd"] for t in trades]
    base_net_bp = [t["net_bp"] for t in trades]
    base_r = [t["r_mult"] for t in trades]

    # Retained vs Rejected under the cap
    retained = [t for t in trades if t["slip_bp"] <= cap_bp]
    rejected = [t for t in trades if t["slip_bp"] > cap_bp]

    ret_pl = [t["pl_usd"] for t in retained]
    ret_net_bp = [t["net_bp"] for t in retained]
    ret_r = [t["r_mult"] for t in retained]

    rej_pl = [t["pl_usd"] for t in rejected]
    rej_net_bp = [t["net_bp"] for t in rejected]
    rej_r = [t["r_mult"] for t in rejected]

    # Group by day for day-t calculation
    days = sorted(set(t["date"] for t in trades))
    day_deltas = []
    for d in days:
        d_all = [t for t in trades if t["date"] == d]
        d_ret = [t for t in retained if t["date"] == d]
        # Day net return mean (unfilled trades count as 0 bp)
        base_day_mean = np.mean([t["net_bp"] for t in d_all]) if d_all else 0.0
        # If capped, rejected trades yield 0 return
        capped_returns = [t["net_bp"] for t in d_ret] + [0.0] * (len(d_all) - len(d_ret))
        capped_day_mean = np.mean(capped_returns) if capped_returns else 0.0
        day_deltas.append(capped_day_mean - base_day_mean)

    day_deltas = np.array(day_deltas)
    mean_day_delta = float(np.mean(day_deltas))
    std_day_delta = float(np.std(day_deltas, ddof=1)) if len(day_deltas) > 1 else 1e-6
    day_t = float(mean_day_delta / (std_day_delta / np.sqrt(len(day_deltas)))) if std_day_delta > 0 else 0.0

    # Two halves for stationarity verification
    mid = len(days) // 2
    h1_days = set(days[:mid])
    h2_days = set(days[mid:])
    h1_deltas = [d for d, day in zip(day_deltas, days) if day in h1_days]
    h2_deltas = [d for d, day in zip(day_deltas, days) if day in h2_days]
    h1_mean = float(np.mean(h1_deltas)) if h1_deltas else 0.0
    h2_mean = float(np.mean(h2_deltas)) if h2_deltas else 0.0

    return {
        "n_total": n_total,
        "n_days": len(days),
        "cap_bp": cap_bp,
        "base_usd_pl": float(np.sum(base_pl)),
        "base_mean_net_bp": float(np.mean(base_net_bp)),
        "base_mean_r": float(np.mean(base_r)),
        "base_win_rate": float(np.mean([x > 0 for x in base_pl]) * 100),
        "n_retained": len(retained),
        "pct_retained": float(len(retained) / n_total * 100),
        "n_rejected": len(rejected),
        "pct_rejected": float(len(rejected) / n_total * 100),
        "rej_usd_pl": float(np.sum(rej_pl)) if rej_pl else 0.0,
        "rej_mean_net_bp": float(np.mean(rej_net_bp)) if rej_net_bp else 0.0,
        "rej_mean_r": float(np.mean(rej_r)) if rej_r else 0.0,
        "rej_win_rate": float(np.mean([x > 0 for x in rej_pl]) * 100) if rej_pl else 0.0,
        "ret_usd_pl": float(np.sum(ret_pl)) if ret_pl else 0.0,
        "ret_mean_net_bp": float(np.mean(ret_net_bp)) if ret_net_bp else 0.0,
        "ret_mean_r": float(np.mean(ret_r)) if ret_r else 0.0,
        "ret_win_rate": float(np.mean([x > 0 for x in ret_pl]) * 100) if ret_pl else 0.0,
        "usd_saved": float(-np.sum(rej_pl)) if rej_pl else 0.0,
        "net_bp_lift": float(np.mean(ret_net_bp) - np.mean(base_net_bp)) if ret_net_bp else 0.0,
        "day_delta_mean": mean_day_delta,
        "day_t": day_t,
        "h1_mean": h1_mean,
        "h2_mean": h2_mean,
    }


def main():
    parser = argparse.ArgumentParser(description="Simulate marketable limit slippage cap on historical trades")
    parser.add_argument("--start", default="2026-09-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2026-09-30", help="End date YYYY-MM-DD")
    parser.add_argument("--cap", type=float, default=5.0, help="Slippage cap in basis points")
    args = parser.parse_args()

    trades = load_trades_for_period(args.start, args.end)
    print(f"Loaded {len(trades)} trades between {args.start} and {args.end}")
    if not trades:
        return 1

    for cap in [8.0, 5.0, 3.0]:
        res = run_simulation(trades, cap_bp=cap)
        print(f"\n=======================================================")
        print(f"Results for Cap = {cap:g} bp ({args.start} to {args.end})")
        print(f"=======================================================")
        print(f"Total Trades: {res['n_total']} across {res['n_days']} sessions")
        print(f"Baseline: ${res['base_usd_pl']:.2f} total, {res['base_mean_net_bp']:+.2f} bp/trade, {res['base_win_rate']:.1f}% win rate")
        print(f"Retained: {res['n_retained']} ({res['pct_retained']:.1f}%) | Rejected: {res['n_rejected']} ({res['pct_rejected']:.1f}%)")
        print(f"Rejected Trades (avoided fills):")
        print(f"  Total PnL: ${res['rej_usd_pl']:.2f} | Mean Net: {res['rej_mean_net_bp']:+.2f} bp | Win Rate: {res['rej_win_rate']:.1f}%")
        print(f"Portfolio Improvement:")
        print(f"  USD PnL: ${res['base_usd_pl']:.2f} -> ${res['ret_usd_pl']:.2f} (Savings: ${res['usd_saved']:+.2f})")
        print(f"  Net Lift: {res['net_bp_lift']:+.2f} bp/trade (Retained vs Base)")
        print(f"  Daily Mean Delta: {res['day_delta_mean']:+.2f} bp/day | day-t: {res['day_t']:.2f}")
        print(f"  Halves: H1 {res['h1_mean']:+.2f} bp | H2 {res['h2_mean']:+.2f} bp")

    return 0


if __name__ == "__main__":
    sys.exit(main())
