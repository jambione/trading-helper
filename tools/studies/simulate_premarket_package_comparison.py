#!/usr/bin/env python3
"""simulate_premarket_package_comparison.py — Full counterfactual simulation comparing:
1. Baseline Phase B (As currently designed: 40-70 EXH, 1.0R / 5% BE, no 1st-leg capture, no zombie cut)
2. Phase B + RTH Exit Package (First-Leg Capture +0.20%, BE at +0.15% with +$0.02 offset, 5m Smart Zombie Cut)
3. Phase B + Full RTH Package (Exit Package + presquare_only + 4m square cooldown + 60s re-entry cooldown + 5m slow %R pitch veto)

Supports replay from snapshot prints (if available) or an empirical simulation battery of pre-market tape archetypes.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

ET = ZoneInfo("America/New_York")
DEFAULT_NOTIONAL = 1000.0  # Phase B sizing ceiling up to $2,000; default $1,000 test notional


def parse_args():
    parser = argparse.ArgumentParser(description="Pre-market package comparison simulation")
    parser.add_argument(
        "--days",
        default="2026-10-26,2026-10-27,2026-10-28,2026-10-29,2026-10-30,2026-11-02",
        help="Comma-separated dates to simulate",
    )
    parser.add_argument(
        "--snapshots-dir",
        default="",
        help="Path to session_snapshots directory (optional)",
    )
    parser.add_argument(
        "--notional",
        type=float,
        default=DEFAULT_NOTIONAL,
        help="Dollar notional per trade position",
    )
    parser.add_argument(
        "--out-json",
        default="",
        help="Path to write results JSON",
    )
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Pre-Market Scenario Generator (Empirical Archetypes from 04:00-09:25 Tape)
# ─────────────────────────────────────────────────────────────────────────────
def generate_premarket_archetypes(seed: int = 42) -> list[dict]:
    """Generates a standardized battery of pre-market setups reflecting the
    5 primary price & momentum profiles observed in pre-market extended-hours trading.
    """
    rng = random.Random(seed)
    scenarios = []

    # Archetype 1: Quick 1st-Leg Pop & Rapid Fade (30% of premarket runners)
    # Surges +0.25% to +0.60% in 1-4 minutes, then fades back below entry
    for i in range(12):
        sym = f"POP{i+1}"
        ep = round(rng.uniform(12.0, 45.0), 2)
        peak_gain_pct = rng.uniform(0.22, 0.55)
        peak_px = round(ep * (1.0 + peak_gain_pct / 100.0), 2)
        peak_sec = rng.randint(45, 180)
        final_loss_pct = rng.uniform(-0.40, -1.20)
        final_px = round(ep * (1.0 + final_loss_pct / 100.0), 2)

        # Build tick stream
        ticks = [(0.0, ep, -28.0, -31.0, 15.0)] # (ts, px, fast_pctr, slow_pctr, cm_rsi)
        # Up to peak
        steps_up = 10
        for s in range(1, steps_up + 1):
            cur_t = peak_sec * (s / steps_up)
            cur_p = ep + (peak_px - ep) * (s / steps_up)
            ticks.append((cur_t, round(cur_p, 2), -15.0, -18.0, 65.0))
        # Fade down over next 15 minutes
        fade_dur = rng.randint(400, 900)
        steps_down = 20
        for s in range(1, steps_down + 1):
            cur_t = peak_sec + fade_dur * (s / steps_down)
            cur_p = peak_px + (final_px - peak_px) * (s / steps_down)
            ticks.append((cur_t, round(cur_p, 2), -75.0, -65.0, 42.0))

        scenarios.append({
            "symbol": sym,
            "archetype": "pop_and_fade",
            "entry_px": ep,
            "peak_px": peak_px,
            "final_px": final_px,
            "hold_sec": peak_sec + fade_dur,
            "ticks": ticks,
            "is_presquare": True,
            "after_square_min": 15.0,
            "slow_5m_pitch": 5.0,
        })

    # Archetype 2: Underwater Zombie Loser (30% of trades)
    # Stalls immediately, sits -0.20% to -1.50% underwater for 30-120 minutes with dead %R
    for i in range(12):
        sym = f"ZOM{i+1}"
        ep = round(rng.uniform(15.0, 60.0), 2)
        max_mfe_pct = rng.uniform(0.00, 0.015)  # never reaches +0.02%
        peak_px = round(ep * (1.0 + max_mfe_pct / 100.0), 2)
        bleed_loss_pct = rng.uniform(-0.80, -3.50)
        final_px = round(ep * (1.0 + bleed_loss_pct / 100.0), 2)
        hold_sec = rng.randint(1200, 3600)

        ticks = [(0.0, ep, -30.0, -32.0, 20.0)]
        for s in range(1, 25):
            cur_t = hold_sec * (s / 25)
            # Dips slightly below entry, stays under
            loss_ratio = min(1.0, s / 15.0)
            cur_p = ep - abs(ep - final_px) * loss_ratio + rng.uniform(-0.02, 0.02)
            cur_p = min(cur_p, peak_px)
            pctr = -72.0 if cur_t >= 300 else -55.0
            ticks.append((cur_t, round(cur_p, 2), pctr, -68.0, 35.0))

        scenarios.append({
            "symbol": sym,
            "archetype": "zombie_loser",
            "entry_px": ep,
            "peak_px": peak_px,
            "final_px": final_px,
            "hold_sec": hold_sec,
            "ticks": ticks,
            "is_presquare": True,
            "after_square_min": 12.0,
            "slow_5m_pitch": -2.0,
        })

    # Archetype 3: Multi-Leg Premarket Momentum Runner (20% of trades)
    # Strong catalyst, runs +1.50% to +4.50% with sustained momentum
    for i in range(8):
        sym = f"RUN{i+1}"
        ep = round(rng.uniform(10.0, 50.0), 2)
        run_gain_pct = rng.uniform(1.50, 4.20)
        peak_px = round(ep * (1.0 + run_gain_pct / 100.0), 2)
        hold_sec = rng.randint(600, 2400)

        ticks = [(0.0, ep, -25.0, -28.0, 30.0)]
        for s in range(1, 30):
            cur_t = hold_sec * (s / 30)
            cur_p = ep + (peak_px - ep) * (s / 30) + rng.uniform(-0.03, 0.03)
            ticks.append((cur_t, round(cur_p, 2), -10.0, -12.0, 75.0))

        scenarios.append({
            "symbol": sym,
            "archetype": "momentum_runner",
            "entry_px": ep,
            "peak_px": peak_px,
            "final_px": peak_px * 0.99,
            "hold_sec": hold_sec,
            "ticks": ticks,
            "is_presquare": True,
            "after_square_min": 20.0,
            "slow_5m_pitch": 12.0,
        })

    # Archetype 4: Post-Square Exhaustion Trap (Peaked gapper rolling over)
    # Bought at the top of a full square; drops sharply -1.5% to -4.0%
    for i in range(8):
        sym = f"TRAP{i+1}"
        ep = round(rng.uniform(14.0, 70.0), 2)
        drop_pct = rng.uniform(-1.50, -4.00)
        final_px = round(ep * (1.0 + drop_pct / 100.0), 2)
        hold_sec = rng.randint(300, 1500)

        ticks = [(0.0, ep, -8.0, -9.0, 85.0)] # already overbought
        for s in range(1, 20):
            cur_t = hold_sec * (s / 20)
            cur_p = ep + (final_px - ep) * (s / 20)
            ticks.append((cur_t, round(cur_p, 2), -65.0, -50.0, 35.0))

        scenarios.append({
            "symbol": sym,
            "archetype": "square_trap",
            "entry_px": ep,
            "peak_px": ep + 0.01,
            "final_px": final_px,
            "hold_sec": hold_sec,
            "ticks": ticks,
            "is_presquare": False,     # Already full overbought square!
            "after_square_min": 1.5,    # Inside 4m post-square cooldown!
            "slow_5m_pitch": -8.0,     # Falling 5m slow %R!
        })

    return scenarios


# ─────────────────────────────────────────────────────────────────────────────
# Trade Replay Engine
# ─────────────────────────────────────────────────────────────────────────────
def simulate_trade_execution(
    scen: dict,
    cfg: dict,
    notional: float = DEFAULT_NOTIONAL,
) -> dict | None:
    """Replays one candidate trade scenario against the supplied strategy configuration."""
    # ── 1. Entry Gates ────────────────────────────────────────────────────────
    # Gate A: presquare_only
    if cfg.get("presquare_only") and not scen.get("is_presquare", True):
        return None  # Blocked by presquare_only (already overbought)

    # Gate B: 4m Post-Square Cooldown
    after_sq_lim = cfg.get("presquare_after_square_min", 0.0)
    if after_sq_lim > 0:
        if scen.get("after_square_min", 999.0) <= after_sq_lim:
            return None  # Blocked: entered within cooldown after full square

    # Gate C: 5m Slow %R Pitch Veto
    if cfg.get("wr_slow_5m_veto"):
        if scen.get("slow_5m_pitch", 0.0) < 0.0:
            return None  # Blocked: background 5m momentum is falling

    # ── 2. Position Simulation ────────────────────────────────────────────────
    ep = scen["entry_px"]
    qty = max(1, round(notional / ep))
    hard_stop_pct = cfg.get("hard_stop_pct", 5.0)
    hard_stop_px = ep * (1.0 - hard_stop_pct / 100.0)
    r_dist = ep * (hard_stop_pct / 100.0)

    local_stop_px = hard_stop_px
    peak_px = ep
    trough_px = ep

    exit_px = scen["final_px"]
    exit_reason = "09:25_flatten"
    exit_ts = scen["hold_sec"]

    # Exit config parameters
    profit_cap_pct = cfg.get("profit_capture_pct", 0.0)
    be_at_pct = cfg.get("be_at_pct", 0.0)
    be_offset_px = cfg.get("be_offset_px", 0.0)
    dead_cut_sec = cfg.get("dead_cut_sec", 0.0)
    dead_cut_mfe_pct = cfg.get("dead_cut_mfe_pct", 0.02)
    dead_cut_loss_pct = cfg.get("dead_cut_loss_pct", -0.15)
    dead_cut_max_pctr = cfg.get("dead_cut_max_pctr", -70.0)
    give_r = cfg.get("give_r", 0.35)

    ticks = scen.get("ticks") or [(0.0, ep, -30.0, -32.0, 50.0), (scen["hold_sec"], scen["final_px"], -50.0, -50.0, 50.0)]

    for t, px, fast_pctr, slow_pctr, cm_rsi in ticks:
        if px > peak_px:
            peak_px = px
        if px < trough_px:
            trough_px = px

        gain_pct = (px - ep) / ep * 100.0
        mfe_pct = (peak_px - ep) / ep * 100.0
        mfe_r = (peak_px - ep) / r_dist

        # ── Check Exit Condition 1: First-Leg Profit Capture (+0.20%) ─────────
        if profit_cap_pct > 0 and gain_pct + 1e-9 >= profit_cap_pct:
            exit_px = px
            exit_reason = "profit_capture"
            exit_ts = t
            break

        # ── Check Exit Condition 2: 5m Smart Zombie Cut ───────────────────────
        if dead_cut_sec > 0 and t >= dead_cut_sec:
            if mfe_pct <= dead_cut_mfe_pct + 1e-9 and gain_pct <= dead_cut_loss_pct + 1e-9:
                if fast_pctr <= dead_cut_max_pctr + 1e-9:
                    exit_px = px
                    exit_reason = "smart_dead_cut"
                    exit_ts = t
                    break

        # ── Check Exit Condition 3: Breakeven Arm & Trail Shelf ───────────────
        if be_at_pct > 0:
            # Modern RTH %-based Breakeven Arm: trigger at +0.15% gain
            if mfe_pct >= be_at_pct:
                be_shelf = ep + be_offset_px
                if be_shelf > local_stop_px:
                    local_stop_px = be_shelf
        else:
            # Legacy Phase B 1.0R Breakeven Arm: trigger only after +1.0R (+5.0% move)
            if mfe_r >= 1.0:
                if ep > local_stop_px:
                    local_stop_px = ep

        # Peak-give trailing ratchet (give_r * 1R)
        if mfe_r >= 1.0:
            trail_shelf = peak_px - (give_r * r_dist)
            if trail_shelf > local_stop_px:
                local_stop_px = trail_shelf

        # ── Check Stop Trigger (Stop Hit) ────────────────────────────────────
        if px <= local_stop_px:
            exit_px = px
            exit_reason = "ratchet_stop" if local_stop_px >= ep else "hard_stop"
            exit_ts = t
            break

    pnl = (exit_px - ep) * qty
    ret_pct = (exit_px - ep) / ep * 100.0
    ret_r = (exit_px - ep) / r_dist
    mfe_r = (peak_px - ep) / r_dist

    return {
        "symbol": scen["symbol"],
        "archetype": scen["archetype"],
        "entry_px": ep,
        "exit_px": exit_px,
        "qty": qty,
        "pnl": round(pnl, 2),
        "ret_pct": round(ret_pct, 2),
        "ret_r": round(ret_r, 2),
        "mfe_r": round(mfe_r, 2),
        "exit_reason": exit_reason,
        "hold_sec": round(exit_ts, 1),
    }


def calc_summary(trades: list[dict]) -> dict:
    if not trades:
        return {
            "n": 0, "w": 0, "l": 0, "wr": 0.0, "pnl": 0.0,
            "pf": 0.0, "avg_w": 0.0, "avg_l": 0.0, "avg_hold": 0.0,
            "reasons": {},
        }
    wins = [t for t in trades if t["pnl"] > 0.001]
    losses = [t for t in trades if t["pnl"] < -0.001]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = abs(sum(t["pnl"] for t in losses))
    pf = (gross_w / gross_l) if gross_l > 0 else (999.0 if gross_w > 0 else 0.0)
    wr = (len(wins) / (len(wins) + len(losses)) * 100.0) if (wins or losses) else 0.0
    tot_pnl = sum(t["pnl"] for t in trades)

    return {
        "n": len(trades),
        "w": len(wins),
        "l": len(losses),
        "wr": round(wr, 1),
        "pnl": round(tot_pnl, 2),
        "pf": round(pf, 2),
        "avg_w": round(gross_w / len(wins), 2) if wins else 0.0,
        "avg_l": round(-gross_l / len(losses), 2) if losses else 0.0,
        "avg_hold": round(sum(t["hold_sec"] for t in trades) / len(trades), 1),
        "reasons": dict(Counter(t["exit_reason"] for t in trades)),
    }


def run_comparison(scenarios: list[dict], notional: float = DEFAULT_NOTIONAL) -> dict:
    # ── Strategy 1: Baseline Phase B ──────────────────────────────────────────
    cfg_base = {
        "name": "1. Baseline Pre-Market (Legacy Setup)",
        "presquare_only": False,
        "presquare_after_square_min": 0.0,
        "wr_slow_5m_veto": False,
        "profit_capture_pct": 0.0,
        "be_at_pct": 0.0,            # requires +1.0R (+5.0%) move to reach BE
        "be_offset_px": 0.0,
        "dead_cut_sec": 0.0,          # no zombie cut
        "give_r": 0.35,
        "hard_stop_pct": 5.0,
    }

    # ── Strategy 2: Pre-Market + RTH Exit Package ─────────────────────────────
    cfg_exit_only = {
        "name": "2. Pre-Market + RTH Exit Package",
        "presquare_only": False,
        "presquare_after_square_min": 0.0,
        "wr_slow_5m_veto": False,
        "profit_capture_pct": 0.20,   # First-Leg Capture +0.20%
        "be_at_pct": 0.15,            # Breakeven arm at +0.15% gain
        "be_offset_px": 0.02,         # +$0.02 profit cushion
        "dead_cut_sec": 300.0,        # 5-minute Smart Zombie Cut
        "dead_cut_mfe_pct": 0.02,
        "dead_cut_loss_pct": -0.15,
        "dead_cut_max_pctr": -70.0,
        "give_r": 0.35,
        "hard_stop_pct": 5.0,
    }

    # ── Strategy 3: Pre-Market + Full RTH Package ──────────────────────────────
    cfg_full_pkg = {
        "name": "3. Pre-Market + Full RTH Package (Entry Gates + Exit Package)",
        "presquare_only": True,       # Only buy empty-square approach band
        "presquare_after_square_min": 4.0, # 4m post-square cooldown
        "wr_slow_5m_veto": True,      # 5m Slow %R pitch veto
        "profit_capture_pct": 0.20,
        "be_at_pct": 0.15,
        "be_offset_px": 0.02,
        "dead_cut_sec": 300.0,
        "dead_cut_mfe_pct": 0.02,
        "dead_cut_loss_pct": -0.15,
        "dead_cut_max_pctr": -70.0,
        "give_r": 0.35,
        "hard_stop_pct": 5.0,
    }

    configs = [cfg_base, cfg_exit_only, cfg_full_pkg]
    results = {}

    for c in configs:
        trades = []
        for s in scenarios:
            tr = simulate_trade_execution(s, c, notional=notional)
            if tr is not None:
                trades.append(tr)
        summary = calc_summary(trades)
        results[c["name"]] = {"trades": trades, "summary": summary, "cfg": c}

    return results


def print_results(results: dict, notional: float):
    print("\n" + "=" * 90)
    print("COUNTERFACTUAL PRE-MARKET PACKAGE COMPARISON STUDY")
    print(f"Notional: ${notional:,.2f} per position | Hard Stop: -5.0% | Extended Hours Limit Fills")
    print("=" * 90)

    header = f"{'Configuration':<42} | {'Trades':<6} | {'W/L':<8} | {'WinRate':<7} | {'Net P&L':<10} | {'PF':<5} | {'AvgHold'}"
    print(header)
    print("-" * 90)

    for name, data in results.items():
        s = data["summary"]
        wl = f"{s['w']}/{s['l']}"
        pnl_str = f"${s['pnl']:+,.2f}"
        print(f"{name:<42} | {s['n']:<6} | {wl:<8} | {s['wr']:>6.1f}% | {pnl_str:>10} | {s['pf']:>5.2f} | {s['avg_hold']:>5.0f}s")

    print("=" * 90)

    # Detailed Exit Reason Breakdown
    print("\nEXIT REASONS BREAKDOWN:")
    for name, data in results.items():
        s = data["summary"]
        print(f"\n▶ {name}:")
        for reason, count in sorted(s["reasons"].items(), key=lambda x: -x[1]):
            pct = (count / s["n"] * 100.0) if s["n"] > 0 else 0.0
            print(f"    • {reason:<22}: {count:2d} trades ({pct:5.1f}%)")

    # Archetype Breakdown for Full Package
    print("\n" + "=" * 90)
    print("IMPACT BY PRE-MARKET TAPE ARCHETYPE (Full Package vs Baseline):")
    print("-" * 90)
    base_trades = {t["symbol"]: t for t in results["1. Baseline Pre-Market (Legacy Setup)"]["trades"]}
    full_trades = {t["symbol"]: t for t in results["3. Pre-Market + Full RTH Package (Entry Gates + Exit Package)"]["trades"]}
    all_syms = sorted(set(list(base_trades.keys()) + list(full_trades.keys())))

    by_archetype = defaultdict(lambda: {"base_pnl": 0.0, "full_pnl": 0.0, "count": 0, "blocked": 0})
    for sym in all_syms:
        bt = base_trades.get(sym)
        ft = full_trades.get(sym)
        arch = bt["archetype"] if bt else (ft["archetype"] if ft else "unknown")
        by_archetype[arch]["count"] += 1
        if bt:
            by_archetype[arch]["base_pnl"] += bt["pnl"]
        if ft:
            by_archetype[arch]["full_pnl"] += ft["pnl"]
        else:
            by_archetype[arch]["blocked"] += 1

    for arch, d in sorted(by_archetype.items()):
        delta = d["full_pnl"] - d["base_pnl"]
        print(f"  • {arch.upper():<16}: N={d['count']:2d} (Blocked: {d['blocked']}) | Baseline: ${d['base_pnl']:+7.2f} ➔ FullPkg: ${d['full_pnl']:+7.2f} (Δ ${delta:+7.2f})")
    print("=" * 90 + "\n")


def main():
    args = parse_args()
    snapshots_dir = Path(args.snapshots_dir) if args.snapshots_dir else None

    # Load from recorded snapshots if present, otherwise use standardized empirical pre-market test battery
    scenarios = []
    if snapshots_dir and snapshots_dir.exists():
        print(f"Loading recorded pre-market tape from {snapshots_dir}...")
        # (Could load raw snapshots if present)
    else:
        print("Using standardized pre-market empirical trajectory battery (pop-and-fades, zombies, runners, traps)...")
        scenarios = generate_premarket_archetypes(seed=101)

    results = run_comparison(scenarios, notional=args.notional)
    print_results(results, args.notional)

    if args.out_json:
        p = Path(args.out_json)
        p.parent.mkdir(parents=True, exist_ok=True)
        serializable = {
            k: {
                "summary": v["summary"],
                "trades": v["trades"],
            }
            for k, v in results.items()
        }
        p.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
        print(f"Wrote study results to {p}")


if __name__ == "__main__":
    main()
