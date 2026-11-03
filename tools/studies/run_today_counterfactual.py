#!/usr/bin/env python3
"""Counterfactual study on today's actual pre-market data (2026-11-02, 04:00 - 09:30 ET).

Reads exact prints from ~/session_snapshots/2026-11-02/recorder_prints.jsonl.gz across all
Phase B candidates admitted today and compares:
1. Baseline Pre-Market (Old Setup from this morning)
2. Exit Package Only (+0.20% Profit Capture, BE @ +0.15% + $0.02, 5m Smart Zombie Cut)
3. Full Package (Presquare-Only Gates, 4m Sq Cooldown, 5m Slow %R Veto, Cooldown + Exit Package)
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SNAPSHOT_DIR = Path(os.path.expanduser("~/session_snapshots/2026-11-02"))

SLOT_NOTIONAL = 1000.0  # $1,000 per slot
HARD_STOP_PCT = 0.05    # -5% disaster stop

SYMBOLS = [
    "EVGO", "CLRO", "CHOW", "ULH", "GDYN", "FMC", "ASUR", "NFE",
    "BTOC", "INLF", "PMN", "QXL", "RBNE", "ELAB", "ITOC", "INO",
    "CALC", "RUBI", "CDT", "CMCSA", "T", "AIOK", "OSPN", "CDNA"
]

def main():
    gz_path = SNAPSHOT_DIR / "recorder_prints.jsonl.gz"
    if not gz_path.exists():
        print(f"Error: {gz_path} not found.")
        sys.exit(1)

    print(f"Loading premarket prints for today (2026-11-02) from {gz_path}...")
    t_0400 = datetime(2026, 11, 2, 4, 0, tzinfo=ET).timestamp()
    t_0925 = datetime(2026, 11, 2, 9, 25, tzinfo=ET).timestamp()
    t_0930 = datetime(2026, 11, 2, 9, 30, tzinfo=ET).timestamp()

    sym_prints = {s: [] for s in SYMBOLS}
    sym_set = set(SYMBOLS)

    with gzip.open(gz_path, "rt") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            sym = r.get("symbol")
            if sym in sym_set:
                t = r.get("ts") or r.get("t")
                if t and t_0400 <= t <= t_0930:
                    p = r.get("price") or r.get("p")
                    if p and float(p) > 0:
                        sym_prints[sym].append((float(t), float(p)))

    for s in sym_prints:
        sym_prints[s].sort()

    active_syms = {s: pts for s, pts in sym_prints.items() if len(pts) >= 5}
    print(f"Loaded prints for {len(active_syms)} active premarket symbols.")

    def compute_pct_r(prices, window=21):
        if len(prices) < 2:
            return -50.0
        sub = prices[-window:]
        hh = max(sub)
        ll = min(sub)
        if hh <= ll:
            return -50.0
        return -100.0 * (hh - prices[-1]) / (hh - ll)

    def simulate_trade(sym, entry_idx, pts, package="baseline"):
        t_entry, p_entry = pts[entry_idx]
        qty = max(1, int(SLOT_NOTIONAL / p_entry))
        hard_stop_px = p_entry * (1.0 - HARD_STOP_PCT)
        peak_px = p_entry
        local_stop_px = hard_stop_px
        be_active = False

        t_exit = None
        p_exit = None
        why_exit = None

        history = [p[1] for p in pts[:entry_idx+1]]

        for i in range(entry_idx + 1, len(pts)):
            t_cur, p_cur = pts[i]
            history.append(p_cur)
            age_sec = t_cur - t_entry

            if p_cur > peak_px:
                peak_px = p_cur

            gain_pct = (p_cur - p_entry) / p_entry * 100.0
            mfe_pct = (peak_px - p_entry) / p_entry * 100.0

            # Hard stop (-5.0%)
            if p_cur <= hard_stop_px + 1e-9:
                t_exit, p_exit, why_exit = t_cur, hard_stop_px, "hard_stop"
                break

            # Mandatory flatten at 09:25 ET
            if t_cur >= t_0925:
                t_exit, p_exit, why_exit = t_cur, p_cur, "flatten_0925"
                break

            # Exit package logic
            if package in ("exit_only", "full_package"):
                # 1. First-leg profit capture (+0.20%)
                if gain_pct >= 0.20:
                    t_exit, p_exit, why_exit = t_cur, p_cur, "profit_capture"
                    break

                # 2. Breakeven jump at +0.15% with +$0.02 offset
                if not be_active and gain_pct >= 0.15:
                    be_active = True
                    local_stop_px = max(local_stop_px, p_entry + 0.02)

                # 3. Smart 5m Zombie Cut (300s, underwater <= -0.15%, never green <= +0.02%, %R <= -70)
                if age_sec >= 300.0:
                    pctr = compute_pct_r(history, window=21)
                    if mfe_pct <= 0.02 and gain_pct <= -0.15 and pctr <= -70.0:
                        t_exit, p_exit, why_exit = t_cur, p_cur, "smart_dead_cut"
                        break
            else:
                # Baseline ratchet trail
                if gain_pct >= 0.50:
                    local_stop_px = max(local_stop_px, p_entry)
                if gain_pct >= 1.00:
                    local_stop_px = max(local_stop_px, peak_px * 0.99)

                # Baseline dead trade (22 min = 1320s)
                if age_sec >= 1320.0 and mfe_pct < 0.20:
                    t_exit, p_exit, why_exit = t_cur, p_cur, "dead_trade_22m"
                    break

            # Stop shelf breach
            if p_cur <= local_stop_px + 1e-9 and local_stop_px > hard_stop_px:
                t_exit, p_exit, why_exit = t_cur, local_stop_px, "trail_stop"
                break

        if p_exit is None:
            t_exit, p_exit, why_exit = pts[-1][0], pts[-1][1], "session_end"

        pnl = qty * (p_exit - p_entry)
        ret_pct = (p_exit - p_entry) / p_entry * 100.0
        hold_sec = t_exit - t_entry

        return {
            "symbol": sym,
            "entry_time": datetime.fromtimestamp(t_entry, tz=ET).strftime("%H:%M:%S"),
            "exit_time": datetime.fromtimestamp(t_exit, tz=ET).strftime("%H:%M:%S"),
            "entry_px": p_entry,
            "exit_px": p_exit,
            "qty": qty,
            "pnl": pnl,
            "ret_pct": ret_pct,
            "hold_sec": hold_sec,
            "why": why_exit,
            "mfe_pct": (peak_px - p_entry) / p_entry * 100.0,
        }

    # Run counterfactual across all candidates
    results = {"baseline": [], "exit_only": [], "full_package": []}

    for sym, pts in sorted(active_syms.items()):
        prices = [p[1] for p in pts]
        for i in range(25, len(pts) - 10, 40):
            t_now, px = pts[i]
            pct_r_fast = compute_pct_r(prices[:i+1], window=21)
            pct_r_slow = compute_pct_r(prices[:i+1], window=112)
            gap = abs(pct_r_fast - pct_r_slow)

            # Baseline entry: legacy 40-70 band (exhaustion 40-70 -> pctr -60 to -30)
            exh = 100.0 + pct_r_fast
            baseline_arm = 40.0 <= exh <= 70.0 and prices[i] > prices[i-1]

            # Full package entry: Presquare only (-35 to -20, tight <= 15, rising), no square trap
            is_square = pct_r_fast >= -20.0 and pct_r_slow >= -20.0 and gap <= 15.0
            is_presquare = pct_r_fast >= -35.0 and pct_r_slow >= -35.0 and gap <= 15.0 and (prices[i] >= prices[i-1])

            # 5m Slow %R pitch check
            slow_5m_falling = len(prices) >= i and compute_pct_r(prices[:max(0, i-10)], 112) > pct_r_slow + 2.0

            full_arm = is_presquare and not is_square and not slow_5m_falling

            if baseline_arm:
                t_base = simulate_trade(sym, i, pts, package="baseline")
                results["baseline"].append(t_base)

                t_exit = simulate_trade(sym, i, pts, package="exit_only")
                results["exit_only"].append(t_exit)

            if full_arm:
                t_full = simulate_trade(sym, i, pts, package="full_package")
                results["full_package"].append(t_full)

    def summarize(trades, name):
        n = len(trades)
        if n == 0:
            print(f"\n{name}: No trades triggered.")
            return {}
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        total_pnl = sum(t["pnl"] for t in trades)
        wr = len(wins) / n * 100.0
        tot_win = sum(t["pnl"] for t in wins)
        tot_loss = abs(sum(t["pnl"] for t in losses))
        pf = (tot_win / tot_loss) if tot_loss > 0 else 999.0
        avg_hold = sum(t["hold_sec"] for t in trades) / n
        exits = {}
        for t in trades:
            exits[t["why"]] = exits.get(t["why"], 0) + 1

        print(f"\n{'='*70}")
        print(f"▶ {name.upper()}")
        print(f"{'='*70}")
        print(f"Total Trades:     {n}")
        print(f"Win Rate:         {wr:.1f}% ({len(wins)}W / {len(losses)}L)")
        print(f"Net P&L:          ${total_pnl:+.2f}")
        print(f"Profit Factor:    {pf:.2f}")
        print(f"Avg Hold Time:    {avg_hold:.0f}s ({avg_hold/60.0:.1f}m)")
        print(f"Exit Breakdown:   {json.dumps(exits, indent=2)}")
        print(f"Sample Trades:")
        for t in trades[:8]:
            print(f"  {t['symbol']:5s} | in={t['entry_time']} @ {t['entry_px']:.2f} | out={t['exit_time']} @ {t['exit_px']:.2f} | PnL={t['pnl']:+7.2f} ({t['ret_pct']:+5.2f}%) | why={t['why']}")

        return {
            "name": name,
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": wr,
            "pnl": total_pnl,
            "profit_factor": pf,
            "avg_hold": avg_hold,
            "exits": exits,
        }

    summary = {
        "baseline": summarize(results["baseline"], "1. Baseline Pre-Market (Old Setup from this morning)"),
        "exit_only": summarize(results["exit_only"], "2. Exit Package Only (+0.20% Profit Capture, BE, 5m Zombie Cut)"),
        "full_package": summarize(results["full_package"], "3. Full Package (Presquare-Only Gates + Exit Package)"),
    }

    out_json = Path("/tmp/today_premarket_counterfactual_summary.json")
    out_json.write_text(json.dumps(summary, indent=2))
    print(f"\nSaved summary to {out_json}")

if __name__ == "__main__":
    main()
