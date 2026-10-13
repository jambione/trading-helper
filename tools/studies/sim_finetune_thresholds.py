#!/usr/bin/env python3
"""Simulate fine-tuning combinations of Breakeven Floor and Profit Capture thresholds."""
import json
from pathlib import Path

OUTCOMES_PATH = Path("ai_reports/outcomes.jsonl")

def main():
    trades = []
    with OUTCOMES_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                ent = d.get("entry_price")
                pk = d.get("peak_price")
                ext = d.get("exit_price") or d.get("exit_price_approx")
                qty = d.get("total_qty") or 1.0
                pnl = d.get("realized_pl_usd")
                arm = d.get("arm_why") or (d.get("features") or {}).get("arm_why") or ""
                seat = d.get("exh_seat_class_fill") or (d.get("features") or {}).get("exh_seat_class") or ""
                if ent and pk and ext and ent > 0:
                    trades.append({
                        "entry": ent, "peak": pk, "exit": ext, "qty": qty,
                        "orig_pnl": pnl if pnl is not None else (ext - ent)*qty,
                        "zenith_pct": (pk - ent) / ent * 100.0,
                        "zenith_cents": pk - ent,
                        "is_square": "square" in arm.lower() or "square" in seat.lower(),
                    })
            except Exception:
                pass

    print(f"Total historical trades analyzed: {len(trades)}")

    configs = [
        ("Current (BE=0.25%, Cap=0.50%)", 0.25, 0.50),
        ("Option 1: Balanced (BE=0.15%, Cap=0.30%)", 0.15, 0.30),
        ("Option 2: High Win-Rate (BE=0.15%, Cap=0.25%)", 0.15, 0.25),
        ("Option 3: Quick Capture (BE=0.18%, Cap=0.35%)", 0.18, 0.35),
    ]

    for subset_name, subset in [("ALL HISTORICAL TRADES", trades), ("ACTIVE DUAL PRE-SQUARE & SQUARE SETUPS", [t for t in trades if t["is_square"]])]:
        print("\n" + "="*88)
        print(f"SUBSET: {subset_name} (N = {len(subset)})")
        print(f"{'Configuration':<38} | {'Win Rate':<10} | {'Loss Rate':<10} | {'Rescued Losses':<15} | {'Total PnL':<10}")
        print("="*88)
        for name, be_pct, cap_pct in configs:
            wins = 0
            losses = 0
            scratches = 0
            total_pl = 0.0
            rescued = 0

            for t in subset:
                ent = t["entry"]
                ext = t["exit"]
                qty = t["qty"]
                orig_pnl = t["orig_pnl"]
                z_pct = t["zenith_pct"]

                sim_exit_px = ext
                if z_pct >= cap_pct:
                    sim_exit_px = ent * (1.0 + cap_pct / 100.0)
                    if orig_pnl < -0.05:
                        rescued += 1
                elif z_pct >= be_pct:
                    be_px = ent + 0.01
                    if ext < be_px:
                        sim_exit_px = be_px
                        if orig_pnl < -0.05:
                            rescued += 1

                sim_pl = (sim_exit_px - ent) * qty
                total_pl += sim_pl

                if sim_pl > 0.05:
                    wins += 1
                elif sim_pl < -0.05:
                    losses += 1
                else:
                    scratches += 1

            win_rate = wins / len(subset) * 100.0
            loss_rate = losses / len(subset) * 100.0
            print(f"{name:<38} | {win_rate:5.1f}%     | {loss_rate:5.1f}%     | {rescued:5d} trades    | ${total_pl:+8.2f}")
        print("="*88)
    return

if __name__ == "__main__":
    main()
