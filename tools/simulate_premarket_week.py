#!/usr/bin/env python3
"""Offline Execution Simulation of Phase B Pre-Market Engine.

Replays raw pre-market tape prints (04:00 - 09:25 ET) from session snapshots,
evaluating candidate admission, entry arming, limit order fills, ratchet stop tracking,
and pre-market exits (ratchet stop trigger and 09:25 ET flatten).

Usage:
    python3 tools/simulate_premarket_week.py [--days 2026-10-26,2026-10-27,...] [--snapshots-dir PATH]
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ET = ZoneInfo("America/New_York")


def parse_args():
    parser = argparse.ArgumentParser(description="Pre-market execution replay")
    parser.add_argument(
        "--days",
        default="2026-10-26,2026-10-27,2026-10-28,2026-10-29,2026-10-30",
        help="Comma-separated dates to simulate",
    )
    parser.add_argument(
        "--snapshots-dir",
        default="/Users/jambimac/session_snapshots",
        help="Path to session_snapshots directory",
    )
    parser.add_argument(
        "--min-price",
        type=float,
        default=2.0,
        help="Pre-market price floor",
    )
    parser.add_argument(
        "--max-seats",
        type=int,
        default=6,
        help="Max Phase B seats",
    )
    parser.add_argument(
        "--max-open",
        type=int,
        default=2,
        help="Max concurrent open positions",
    )
    parser.add_argument(
        "--hard-stop-pct",
        type=float,
        default=5.0,
        help="Hard stop percentage",
    )
    parser.add_argument(
        "--give-r",
        type=float,
        default=0.35,
        help="Peak-give R for ratchet stop trail",
    )
    parser.add_argument(
        "--out-json",
        default="",
        help="Path to write full results JSON",
    )
    return parser.parse_args()


def simulate_day(
    day: str,
    snapshots_dir: Path,
    min_price: float = 2.0,
    max_seats: int = 6,
    max_open: int = 2,
    hard_stop_pct: float = 5.0,
    give_r: float = 0.35,
) -> tuple[list[dict], dict[str, Any]]:
    day_dir = snapshots_dir / day
    prints_path = day_dir / "recorder_prints.jsonl.gz"
    if not prints_path.exists():
        print(f"[{day}] Missing prints file: {prints_path}")
        return [], {}

    print(f"[{day}] Loading pre-market prints from {prints_path.name}...")

    # 1. First pass: group pre-market prints by symbol and identify active candidates
    # Pre-market trading window: 04:00:00 <= et < 09:25:00
    prints_by_sym: dict[str, list[dict]] = defaultdict(list)
    initial_prices: dict[str, float] = {}

    with gzip.open(prints_path, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            et = r.get("et", "")
            if not ("04:00:00" <= et <= "09:25:00"):
                if et > "09:25:00":
                    break
                continue
            sym = r.get("symbol")
            px = r.get("price")
            age = r.get("age_sec")
            if not sym or px is None:
                continue
            try:
                px = float(px)
            except (ValueError, TypeError):
                continue
            if px < min_price or px > 100.0:
                continue

            if sym not in initial_prices:
                initial_prices[sym] = px
            prints_by_sym[sym].append(r)

    # Filter symbols that have fresh active trading in pre-market
    active_syms = [
        s for s, prs in prints_by_sym.items()
        if len(prs) >= 50 and any(p.get("age_sec") is not None and float(p.get("age_sec")) <= 15.0 for p in prs)
    ]
    print(f"[{day}] Found {len(active_syms)} active tradable premarket symbols (>= ${min_price:.2f})")

    # 2. Simulate chronological event stream across the candidate symbols
    # Flatten prints chronologically
    all_events = []
    for s in active_syms:
        for p in prints_by_sym[s]:
            all_events.append(p)
    all_events.sort(key=lambda x: (x.get("ts", 0), x.get("et", "")))

    # Phase B Session State
    seated_symbols: set[str] = set()
    open_positions: dict[str, dict] = {}
    pending_orders: dict[str, dict] = {}  # sym -> {lim_px, submit_ts, submit_et}
    closed_trades: list[dict] = []

    # Indicators state: 1-minute high/low window for exhaustion
    minute_bars: dict[str, list[tuple[float, float, float]]] = defaultdict(list) # sym -> [(high, low, close)]
    last_minute_et: dict[str, str] = {}

    def get_exhaustion(sym: str, px: float) -> float:
        bars = minute_bars[sym]
        if not bars:
            return 50.0
        hh = max(b[0] for b in bars[-14:])
        ll = min(b[1] for b in bars[-14:])
        if hh == ll:
            return 50.0
        pctr = (hh - px) / (hh - ll) * -100.0
        exh = 100.0 + pctr
        return max(0.0, min(100.0, exh))

    for p in all_events:
        sym = p.get("symbol")
        px = float(p.get("price"))
        ts = float(p.get("ts", 0))
        et = p.get("et", "")
        age = p.get("age_sec")
        fresh = age is not None and float(age) <= 15.0

        # Update minute bar trackers
        cur_min = et[:5]
        if last_minute_et.get(sym) != cur_min:
            minute_bars[sym].append((px, px, px))
            last_minute_et[sym] = cur_min
            if len(minute_bars[sym]) > 60:
                minute_bars[sym].pop(0)
        else:
            h, l, _ = minute_bars[sym][-1]
            minute_bars[sym][-1] = (max(h, px), min(l, px), px)

        # ── A. Manage Open Positions (Ratchet Stop & Flatten) ─────────
        if sym in open_positions:
            pos = open_positions[sym]
            pos["last_seen_px"] = px

            # Update excursions
            if px > pos["peak_px"]:
                pos["peak_px"] = px
            if px < pos["trough_px"]:
                pos["trough_px"] = px

            favorable_dist = pos["peak_px"] - pos["entry_px"]
            pos["mfe_r"] = favorable_dist / pos["r_dist"]
            pos["mae_r"] = (pos["trough_px"] - pos["entry_px"]) / pos["r_dist"]

            # Ratchet stop update:
            # 1. When MFE >= 1.0R, stop moves to breakeven (entry_px)
            if pos["mfe_r"] >= 1.0 and pos["local_stop_px"] < pos["entry_px"]:
                pos["local_stop_px"] = pos["entry_px"]
                pos["ratchet_state"] = "breakeven"

            # 2. Peak-give trail: peak - (give_r * 1R)
            if pos["mfe_r"] >= 1.0:
                trail_shelf = pos["peak_px"] - (give_r * pos["r_dist"])
                if trail_shelf > pos["local_stop_px"]:
                    pos["local_stop_px"] = trail_shelf
                    pos["ratchet_state"] = "trailing"

            # Check Stop Trigger: print <= local_stop_px
            if px <= pos["local_stop_px"]:
                ret_pct = ((px - pos["entry_px"]) / pos["entry_px"]) * 100.0
                ret_r = (px - pos["entry_px"]) / pos["r_dist"]
                reason = "ratchet_stop" if pos.get("ratchet_state") in ("breakeven", "trailing") else "hard_stop"
                closed_trades.append({
                    "day": day,
                    "symbol": sym,
                    "entry_et": pos["entry_et"],
                    "entry_px": pos["entry_px"],
                    "exit_et": et,
                    "exit_px": px,
                    "stop_shelf": pos["local_stop_px"],
                    "ret_pct": round(ret_pct, 2),
                    "ret_r": round(ret_r, 2),
                    "mfe_r": round(pos["mfe_r"], 2),
                    "mae_r": round(pos["mae_r"], 2),
                    "exit_reason": reason,
                    "hold_sec": round(ts - pos["entry_ts"], 1),
                })
                del open_positions[sym]
                continue

            # Check 09:25:00 ET Flatten
            if et >= "09:25:00":
                ret_pct = ((px - pos["entry_px"]) / pos["entry_px"]) * 100.0
                ret_r = (px - pos["entry_px"]) / pos["r_dist"]
                closed_trades.append({
                    "day": day,
                    "symbol": sym,
                    "entry_et": pos["entry_et"],
                    "entry_px": pos["entry_px"],
                    "exit_et": et,
                    "exit_px": px,
                    "stop_shelf": pos["local_stop_px"],
                    "ret_pct": round(ret_pct, 2),
                    "ret_r": round(ret_r, 2),
                    "mfe_r": round(pos["mfe_r"], 2),
                    "mae_r": round(pos["mae_r"], 2),
                    "exit_reason": "09:25_flatten",
                    "hold_sec": round(ts - pos["entry_ts"], 1),
                })
                del open_positions[sym]
                continue

        # ── B. Check Pending Entry Limit Orders ───────────────────────
        if sym in pending_orders:
            order = pending_orders[sym]
            # Check fill: if print <= limit price
            if px <= order["lim_px"]:
                r_dist = px * (hard_stop_pct / 100.0)
                open_positions[sym] = {
                    "symbol": sym,
                    "entry_ts": ts,
                    "entry_et": et,
                    "entry_px": px,
                    "peak_px": px,
                    "trough_px": px,
                    "local_stop_px": px - r_dist,
                    "r_dist": r_dist,
                    "mfe_r": 0.0,
                    "mae_r": 0.0,
                    "ratchet_state": "initial",
                }
                del pending_orders[sym]
                continue
            # TTL check (45 seconds)
            if (ts - order["submit_ts"]) > 45.0 or et >= "09:20:00":
                del pending_orders[sym]
                continue

        # ── C. Seat Admission & Arming Check ──────────────────────────
        if et >= "09:20:00":
            continue  # Entry cutoff

        if len(open_positions) >= max_open:
            continue

        if sym not in seated_symbols:
            if len(seated_symbols) < max_seats:
                seated_symbols.add(sym)

        if sym in seated_symbols and sym not in open_positions and sym not in pending_orders:
            if fresh:
                exh = get_exhaustion(sym, px)
                # Phase B Arm Criteria: Exhaustion 40-70 rising
                if 40.0 <= exh <= 70.0:
                    pad = 0.0015
                    lim = round(min(px * (1.0 + pad), px + 0.05), 2)
                    pending_orders[sym] = {
                        "lim_px": lim,
                        "submit_ts": ts,
                        "submit_et": et,
                    }

    # Summary Stats for the Day
    n_trades = len(closed_trades)
    wins = [t for t in closed_trades if t["ret_r"] > 0]
    losses = [t for t in closed_trades if t["ret_r"] <= 0]
    win_rate = (len(wins) / n_trades * 100.0) if n_trades > 0 else 0.0
    tot_r = sum(t["ret_r"] for t in closed_trades)
    tot_pct = sum(t["ret_pct"] for t in closed_trades)

    gross_win = sum(t["ret_r"] for t in wins)
    gross_loss = abs(sum(t["ret_r"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)

    summary = {
        "day": day,
        "n_trades": n_trades,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate": round(win_rate, 1),
        "net_r": round(tot_r, 2),
        "net_pct": round(tot_pct, 2),
        "profit_factor": round(profit_factor, 2),
        "ratchet_exits": sum(1 for t in closed_trades if t["exit_reason"] == "ratchet_stop"),
        "hard_stops": sum(1 for t in closed_trades if t["exit_reason"] == "hard_stop"),
        "flattens": sum(1 for t in closed_trades if t["exit_reason"] == "09:25_flatten"),
    }
    return closed_trades, summary


def main():
    args = parse_args()
    days = [d.strip() for d in args.days.split(",") if d.strip()]
    snapshots_dir = Path(args.snapshots_dir)

    print("=" * 80)
    print("PHASE B PRE-MARKET OFFLINE EXECUTION SIMULATION")
    print(f"Days: {days}")
    print(f"Floor: ${args.min_price:.2f} | Max Seats: {args.max_seats} | Max Open: {args.max_open}")
    print(f"Hard Stop: {args.hard_stop_pct}% | Give-R: {args.give_r}R")
    print("=" * 80 + "\n")

    all_trades = []
    day_summaries = []

    for d in days:
        trades, summ = simulate_day(
            d,
            snapshots_dir,
            min_price=args.min_price,
            max_seats=args.max_seats,
            max_open=args.max_open,
            hard_stop_pct=args.hard_stop_pct,
            give_r=args.give_r,
        )
        if summ:
            day_summaries.append(summ)
            all_trades.extend(trades)
            print(f"[{d}] Trades: {summ['n_trades']} | Win Rate: {summ['win_rate']}% | Net R: {summ['net_r']:+.2f}R | Profit Factor: {summ['profit_factor']:.2f}")

    # Weekly Overall Report
    print("\n" + "=" * 80)
    print("WEEKLY SUMMARY REPORT (OCT 26 - OCT 30)")
    print("=" * 80)
    print(f"{'DAY':<12} | {'TRADES':<6} | {'W/L':<8} | {'WIN RATE':<9} | {'NET R':<8} | {'NET %':<8} | {'PF':<6} | {'EXITS (RATCHET / STOP / FLAT)'}")
    print("-" * 80)
    for s in day_summaries:
        wl = f"{s['n_wins']}/{s['n_losses']}"
        exits = f"{s['ratchet_exits']} / {s['hard_stops']} / {s['flattens']}"
        print(f"{s['day']:<12} | {s['n_trades']:<6} | {wl:<8} | {s['win_rate']:<8.1f}% | {s['net_r']:<+7.2f}R | {s['net_pct']:<+7.2f}% | {s['profit_factor']:<6.2f} | {exits}")

    tot_trades = len(all_trades)
    tot_wins = sum(1 for t in all_trades if t["ret_r"] > 0)
    tot_losses = sum(1 for t in all_trades if t["ret_r"] <= 0)
    tot_win_rate = (tot_wins / tot_trades * 100.0) if tot_trades > 0 else 0.0
    tot_r = sum(t["ret_r"] for t in all_trades)
    tot_pct = sum(t["ret_pct"] for t in all_trades)
    gross_w = sum(t["ret_r"] for t in all_trades if t["ret_r"] > 0)
    gross_l = abs(sum(t["ret_r"] for t in all_trades if t["ret_r"] <= 0))
    overall_pf = (gross_w / gross_l) if gross_l > 0 else (999.0 if gross_w > 0 else 0.0)

    print("=" * 80)
    print(f"TOTAL TRADES  : {tot_trades}")
    print(f"WIN RATE      : {tot_win_rate:.1f}% ({tot_wins}W / {tot_losses}L)")
    print(f"NET RETURN    : {tot_r:+.2f}R ({tot_pct:+.2f}%)")
    print(f"PROFIT FACTOR : {overall_pf:.2f}")
    if all_trades:
        med_mfe = sorted(t["mfe_r"] for t in all_trades)[len(all_trades)//2]
        print(f"MEDIAN MFE    : {med_mfe:+.2f}R")
    print("=" * 80)

    # Print top 15 trades sample
    if all_trades:
        print("\nSAMPLE TRADES LOG (First 15):")
        print(f"{'DAY':<11} {'SYM':<6} {'ENTRY ET':<9} {'ENTRY $':<8} {'EXIT ET':<9} {'EXIT $':<8} {'RET %':<7} {'RET R':<7} {'MFE R':<6} {'EXIT REASON'}")
        print("-" * 90)
        for t in all_trades[:15]:
            print(f"{t['day']:<11} {t['symbol']:<6} {t['entry_et']:<9} ${t['entry_px']:<7.2f} {t['exit_et']:<9} ${t['exit_px']:<7.2f} {t['ret_pct']:<+6.2f}% {t['ret_r']:<+6.2f}R {t['mfe_r']:<+5.2f}R {t['exit_reason']}")

    if args.out_json:
        out_p = Path(args.out_json)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "days": days,
            "min_price": args.min_price,
            "max_seats": args.max_seats,
            "max_open": args.max_open,
            "hard_stop_pct": args.hard_stop_pct,
            "give_r": args.give_r,
            "summary": {
                "tot_trades": tot_trades,
                "tot_wins": tot_wins,
                "tot_losses": tot_losses,
                "win_rate": round(tot_win_rate, 1),
                "tot_r": round(tot_r, 2),
                "tot_pct": round(tot_pct, 2),
                "profit_factor": round(overall_pf, 2),
                "ratchet_exits": sum(1 for t in all_trades if t["exit_reason"] == "ratchet_stop"),
                "hard_stops": sum(1 for t in all_trades if t["exit_reason"] == "hard_stop"),
                "flattens": sum(1 for t in all_trades if t["exit_reason"] == "09:25_flatten"),
            },
            "day_summaries": day_summaries,
            "all_trades": all_trades,
        }
        out_p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote full results JSON to {out_p}")


if __name__ == "__main__":
    main()
