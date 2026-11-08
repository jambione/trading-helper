#!/usr/bin/env python3
"""exhaustive_liquidity_maker_sim.py: Exhaustive Risk & Geometry Grid for Passive Liquidity Maker.

Standard: docs/TEST_STANDARD.md
Pre-registration: docs/studies/liquidity_shock_maker_prereg.json

Replays the 606 filled trades across 93 SIP warehouse sessions (2026-04-27..2026-10-15)
with exact minute-by-minute bar paths, conservative execution modeling, and anti-peeking rules.

Sweeps:
  1. Stop Loss: 20 bp, 25 bp, 30 bp, 35 bp, 40 bp, 50 bp, 60 bp, 75 bp, 100 bp
  2. Profit Target: 20 bp, 25 bp, 30 bp, 35 bp, 40 bp, 50 bp
  3. Time Horizon (H): 2m, 3m, 4m, 5m, 7m, 10m
  4. Breakeven Protection: None vs Move stop to entry once +20 bp / +25 bp reached
  5. Time of day: Morning (09:45-11:30), Midday (11:30-14:00), Afternoon (14:00-15:30)
  6. SPY regime: SPY 15m >= -30 bp vs >= -15 bp vs >= 0 bp
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "ai_reports").exists():
    cwd = Path.cwd().resolve()
    if (cwd / "ai_reports").exists():
        ROOT = cwd
    elif (Path.home() / "repo" / "trading-helper" / "ai_reports").exists():
        ROOT = Path.home() / "repo" / "trading-helper"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day  # noqa: E402

WH_SIP = ROOT / "ai_reports" / "warehouse" / "sip"
OUT = ROOT / "ai_reports" / "exhaustive_liquidity_maker"
RUN_PKL = ROOT / "ai_reports" / "liquidity_shock_maker" / "run.pkl"

LIMIT_OFFSET = 0.0002       # Buy limit placed 2 bp below c[t]
TRADE_THROUGH = 0.0002      # 2 bp trade-through required for fills
MAKER_HALF_COST = 0.0       # 0 bp maker fee for limit fills
TAKER_HALF_COST = 3.0       # 3 bp taker fee for stop loss / time stop exits
STRESS_EXTRA = 4.0          # 4 bp stress deduction


def simulate_maker_trade(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    t: int,
    stop_bp: float,
    target_bp: float,
    H: int = 10,
    be_trigger_bp: float | None = None,
) -> dict | None:
    """Exact causal simulation of resting maker limit buy and exit rules.
    
    Guarantees:
      1. Order placed at c[t] * (1 - 2 bp).
      2. Fill on bar t+1 requires l[t+1] <= p_lim * (1 - 2 bp).
      3. Anti-peeking on bar t+1: Check catastrophic stop ONLY; target NEVER hits on entry bar.
      4. For bars k in [t+2 .. t+1+H]:
         - First check stop (worst-case priority): if l[k] <= p_stop, exit at min(p_stop, o[k]) - 3 bp fee.
         - Second check target: if h[k] >= p_target * (1 + 2 bp), exit at p_target with 0 bp fee.
         - If breakeven trigger active and h[k] >= p_be, raise stop to entry_px.
      5. Time stop at bar t+1+H: exit at c[t+1+H] - 3 bp fee.
    """
    if t + 1 + H >= len(c) or t + 1 >= len(c):
        return None

    # Missing bar check
    for arr in (o, h, l, c):
        if np.isnan(arr[t + 1]) or np.isnan(arr[t + 1 + H]):
            return None
    if o[t + 1] <= 0 or c[t] <= 0:
        return None

    p_lim = c[t] * (1.0 - LIMIT_OFFSET)

    # Fill check on bar t+1
    if l[t + 1] > p_lim * (1.0 - TRADE_THROUGH):
        return None  # Unfilled

    entry_px = p_lim
    p_stop = entry_px * (1.0 - stop_bp / 10000.0)
    p_target = entry_px * (1.0 + target_bp / 10000.0)
    p_be = entry_px * (1.0 + be_trigger_bp / 10000.0) if be_trigger_bp else None

    # Bar t+1 (Entry bar): Catastrophic stop ONLY, no target hit allowed
    if l[t + 1] <= p_stop:
        exit_px = min(p_stop, o[t + 1])
        fee = TAKER_HALF_COST
        exit_bar = t + 1
        reason = "stop"
    else:
        exited = False
        exit_px = None
        exit_bar = None
        fee = None
        reason = None

        for k in range(t + 2, t + 1 + H + 1):
            if np.isnan(o[k]) or np.isnan(h[k]) or np.isnan(l[k]) or np.isnan(c[k]):
                return None

            # 1. First check Stop (worst-case priority)
            if l[k] <= p_stop:
                exit_px = min(p_stop, o[k])
                fee = TAKER_HALF_COST
                exit_bar = k
                reason = "stop"
                exited = True
                break

            # 2. Second check Passive Target (with 2 bp trade-through)
            if h[k] >= p_target * (1.0 + TRADE_THROUGH):
                exit_px = p_target
                fee = MAKER_HALF_COST
                exit_bar = k
                reason = "target"
                exited = True
                break

            # 3. Breakeven trigger: raise stop to entry price
            if p_be and h[k] >= p_be:
                p_stop = max(p_stop, entry_px)

        if not exited:
            # Time exit at bar t+1+H
            exit_px = c[t + 1 + H]
            fee = TAKER_HALF_COST
            exit_bar = t + 1 + H
            reason = "time"

    net = float((exit_px / entry_px - 1.0) * 10000.0 - fee)
    net_s = float(net - STRESS_EXTRA)
    gross = float((exit_px / entry_px - 1.0) * 10000.0)

    return {
        "net": net,
        "net_s": net_s,
        "gross": gross,
        "exit_bar": exit_bar,
        "hold_bars": exit_bar - (t + 1),
        "exit_reason": reason,
        "entry_px": entry_px,
        "exit_px": exit_px,
    }


def compute_grid_metrics(trades: list[dict], half_cutoff_day: str = "2026-07-20") -> dict:
    if not trades:
        return {
            "n_trades": 0, "n_trades_a": 0, "n_trades_b": 0, "n_days": 0,
            "mean_net_bp": 0.0, "net_a_bp": 0.0, "net_b_bp": 0.0,
            "winsor_mean_bp": 0.0, "winsor_a_bp": 0.0, "winsor_b_bp": 0.0,
            "se_day_bp": 0.0, "t_stat": 0.0, "win_rate_pct": 0.0, "profit_factor": 0.0,
            "max_dd_bp": 0.0, "reasons": {}, "mean_hold_bars": 0.0,
        }

    by_day = defaultdict(list)
    by_half = {"A": [], "B": []}
    nets = []
    reasons = defaultdict(int)
    holds = []

    for tr in trades:
        d = tr["day"]
        h = "A" if d < half_cutoff_day else "B"
        n = tr["net"]
        by_day[d].append(n)
        by_half[h].append(n)
        nets.append(n)
        reasons[tr["exit_reason"]] += 1
        holds.append(tr.get("hold_bars", 0))

    n_trades = len(trades)
    n_days = len(by_day)

    day_means = [float(np.mean(vals)) for vals in by_day.values()]
    mean_day_net = float(np.mean(day_means))
    se_day = float(np.std(day_means, ddof=1) / math.sqrt(n_days)) if n_days > 1 else 0.0
    t_stat = mean_day_net / se_day if se_day > 1e-9 else 0.0

    n_a = len(by_half["A"])
    n_b = len(by_half["B"])
    net_a = float(np.mean(by_half["A"])) if n_a > 0 else 0.0
    net_b = float(np.mean(by_half["B"])) if n_b > 0 else 0.0

    w_clip = np.percentile(nets, 99.0) if len(nets) >= 100 else np.max(nets)
    w_net = float(np.mean([min(x, w_clip) for x in nets]))
    w_a = float(np.mean([min(x, w_clip) for x in by_half["A"]])) if n_a > 0 else 0.0
    w_b = float(np.mean([min(x, w_clip) for x in by_half["B"]])) if n_b > 0 else 0.0

    win_rate = float(np.mean([1.0 if x > 0 else 0.0 for x in nets]) * 100.0)
    profit_factor = float(
        sum(x for x in nets if x > 0) / max(1e-6, abs(sum(x for x in nets if x < 0)))
    )

    cum = np.cumsum(nets)
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    max_dd = float(np.max(dd)) if len(dd) > 0 else 0.0

    return {
        "n_trades": n_trades,
        "n_trades_a": n_a,
        "n_trades_b": n_b,
        "n_days": n_days,
        "mean_net_bp": float(np.mean(nets)),
        "net_a_bp": net_a,
        "net_b_bp": net_b,
        "winsor_mean_bp": w_net,
        "winsor_a_bp": w_a,
        "winsor_b_bp": w_b,
        "se_day_bp": se_day,
        "t_stat": t_stat,
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "max_dd_bp": max_dd,
        "mean_hold_bars": float(np.mean(holds)),
        "reasons": dict(reasons),
    }


def main():
    parser = argparse.ArgumentParser(description="Exhaustive Liquidity Maker Risk Grid Simulation")
    parser.add_argument("--out-dir", default=str(OUT), help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading filled trades from {RUN_PKL}...")
    with open(RUN_PKL, "rb") as f:
        run_data = pickle.load(f)

    m_trades = run_data["res"]["M"]
    print(f"Total baseline filled trades: {len(m_trades)}")

    # Group trades by day
    trades_by_day = defaultdict(list)
    for tr in m_trades:
        trades_by_day[tr["d"]].append(tr)

    sorted_days = sorted(trades_by_day.keys())
    print(f"Total unique days: {len(sorted_days)}")
    half_cutoff_day = sorted_days[len(sorted_days) // 2]
    print(f"Chronological Half cutoff day: {half_cutoff_day} (Half A < {half_cutoff_day}, Half B >= {half_cutoff_day})")

    # Extract 1-minute bar trajectory [t, t+15] for each trade
    print("Extracting bar trajectories from SIP warehouse...")
    extracted_trajectories = []
    missing_trajectories = 0

    for d_idx, day_str in enumerate(sorted_days):
        if d_idx % 20 == 0:
            print(f"  Loading day {d_idx}/{len(sorted_days)} ({day_str})...")
        fpath = WH_SIP / f"{day_str}.npz"
        if not fpath.exists():
            missing_trajectories += len(trades_by_day[day_str])
            continue
        day_npz = load_day(fpath)
        syms_idx = day_npz["idx"]
        spy_idx = syms_idx.get("SPY")

        for tr in trades_by_day[day_str]:
            sym = tr["sym"]
            t = tr["t"]
            s_idx = syms_idx.get(sym)
            if s_idx is None:
                missing_trajectories += 1
                continue

            o_slice = day_npz["o"][s_idx, :]
            h_slice = day_npz["h"][s_idx, :]
            l_slice = day_npz["l"][s_idx, :]
            c_slice = day_npz["c"][s_idx, :]

            # SPY 15m return at t
            spy_15m_ret = 0.0
            if spy_idx is not None and t >= 15:
                spy_c = day_npz["c"][spy_idx, :]
                if not np.isnan(spy_c[t]) and not np.isnan(spy_c[t - 15]) and spy_c[t - 15] > 0:
                    spy_15m_ret = float(spy_c[t] / spy_c[t - 15] - 1.0)

            extracted_trajectories.append({
                "day": day_str,
                "sym": sym,
                "t": t,
                "o": np.array(o_slice, copy=True),
                "h": np.array(h_slice, copy=True),
                "l": np.array(l_slice, copy=True),
                "c": np.array(c_slice, copy=True),
                "spy_15m_ret": spy_15m_ret,
            })

    print(f"Extracted trajectories: {len(extracted_trajectories)} (Missing: {missing_trajectories})")

    # =======================================================
    # GRID 1: Stop Loss vs Profit Target (Holding H = 10 bars)
    # =======================================================
    print("\n=======================================================")
    print("RUNNING GRID 1: Stop Loss vs Profit Target (H = 10 bars)")
    print("=======================================================")

    stops = [20.0, 25.0, 30.0, 35.0, 40.0, 50.0, 75.0, 100.0]
    targets = [20.0, 25.0, 30.0, 35.0, 40.0, 50.0]
    grid1_results = {}

    for st in stops:
        for tg in targets:
            key = f"stop{int(st)}_target{int(tg)}"
            trades = []
            for traj in extracted_trajectories:
                tr = simulate_maker_trade(
                    traj["o"], traj["h"], traj["l"], traj["c"], traj["t"],
                    stop_bp=st, target_bp=tg, H=10,
                )
                if tr:
                    tr["day"] = traj["day"]
                    trades.append(tr)

            metrics = compute_grid_metrics(trades, half_cutoff_day)
            grid1_results[key] = metrics
            t_cnt = metrics["reasons"].get("target", 0)
            s_cnt = metrics["reasons"].get("stop", 0)
            tm_cnt = metrics["reasons"].get("time", 0)
            t_pct = t_cnt / max(1, metrics["n_trades"]) * 100.0
            s_pct = s_cnt / max(1, metrics["n_trades"]) * 100.0

            if st in (25.0, 30.0, 35.0, 40.0, 100.0) and tg in (25.0, 30.0, 40.0):
                print(
                    f"[{key:18s}] Net={metrics['mean_net_bp']:+6.2f} bp (A:{metrics['net_a_bp']:+6.2f}, B:{metrics['net_b_bp']:+6.2f}) "
                    f"Win={metrics['win_rate_pct']:4.1f}% PF={metrics['profit_factor']:4.2f} t={metrics['t_stat']:4.2f} "
                    f"| Tgt:{t_pct:4.1f}% Stp:{s_pct:4.1f}% Tm:{tm_cnt:3d} Hold:{metrics['mean_hold_bars']:.1f}m"
                )

    # =======================================================
    # GRID 2: Holding Time Horizon (H = 2, 3, 4, 5, 7, 10 bars)
    # =======================================================
    print("\n=======================================================")
    print("RUNNING GRID 2: Time Horizon H (Stop 30 bp, Target 40 bp)")
    print("=======================================================")

    horizons = [2, 3, 4, 5, 7, 10]
    grid2_results = {}

    for h in horizons:
        key = f"H_{h}m_stop30_target40"
        trades = []
        for traj in extracted_trajectories:
            tr = simulate_maker_trade(
                traj["o"], traj["h"], traj["l"], traj["c"], traj["t"],
                stop_bp=30.0, target_bp=40.0, H=h,
            )
            if tr:
                tr["day"] = traj["day"]
                trades.append(tr)

        metrics = compute_grid_metrics(trades, half_cutoff_day)
        grid2_results[key] = metrics
        t_cnt = metrics["reasons"].get("target", 0)
        s_cnt = metrics["reasons"].get("stop", 0)
        tm_cnt = metrics["reasons"].get("time", 0)
        print(
            f"[{key:24s}] Net={metrics['mean_net_bp']:+6.2f} bp (A:{metrics['net_a_bp']:+6.2f}, B:{metrics['net_b_bp']:+6.2f}) "
            f"Win={metrics['win_rate_pct']:4.1f}% PF={metrics['profit_factor']:4.2f} t={metrics['t_stat']:4.2f} "
            f"| Tgt:{t_cnt:3d} Stp:{s_cnt:3d} Tm:{tm_cnt:3d} Hold:{metrics['mean_hold_bars']:.1f}m"
        )

    # =======================================================
    # GRID 3: Breakeven Ratchet (Target 40 bp, Stop 30 bp, H = 5)
    # =======================================================
    print("\n=======================================================")
    print("RUNNING GRID 3: Breakeven Ratchet (Stop 30 bp, Target 40 bp, H = 5)")
    print("=======================================================")

    be_options = [
        ("no_breakeven", None),
        ("be_at_20bp", 20.0),
        ("be_at_25bp", 25.0),
        ("be_at_30bp", 30.0),
    ]
    grid3_results = {}

    for name, be_val in be_options:
        trades = []
        for traj in extracted_trajectories:
            tr = simulate_maker_trade(
                traj["o"], traj["h"], traj["l"], traj["c"], traj["t"],
                stop_bp=30.0, target_bp=40.0, H=5, be_trigger_bp=be_val,
            )
            if tr:
                tr["day"] = traj["day"]
                trades.append(tr)

        metrics = compute_grid_metrics(trades, half_cutoff_day)
        grid3_results[name] = metrics
        print(
            f"[{name:20s}] Net={metrics['mean_net_bp']:+6.2f} bp (A:{metrics['net_a_bp']:+6.2f}, B:{metrics['net_b_bp']:+6.2f}) "
            f"Win={metrics['win_rate_pct']:4.1f}% PF={metrics['profit_factor']:4.2f} t={metrics['t_stat']:4.2f} "
            f"MaxDD={metrics['max_dd_bp']:5.0f}bp"
        )

    # =======================================================
    # GRID 4: SPY Regime & Intraday Time of Day
    # =======================================================
    print("\n=======================================================")
    print("RUNNING GRID 4: SPY Filter & Time of Day")
    print("=======================================================")

    regime_filters = [
        ("all_triggers", lambda traj: True),
        ("spy_15m_ge_-15bp", lambda traj: traj["spy_15m_ret"] >= -0.0015),
        ("spy_15m_ge_0bp", lambda traj: traj["spy_15m_ret"] >= 0.0),
        ("morning_0945_1130", lambda traj: 15 <= traj["t"] <= 120),
        ("midday_1130_1400", lambda traj: 120 < traj["t"] <= 270),
        ("afternoon_1400_1530", lambda traj: 270 < traj["t"] <= 360),
    ]
    grid4_results = {}

    for name, filt in regime_filters:
        trades = []
        for traj in extracted_trajectories:
            if not filt(traj):
                continue
            tr = simulate_maker_trade(
                traj["o"], traj["h"], traj["l"], traj["c"], traj["t"],
                stop_bp=30.0, target_bp=40.0, H=5, be_trigger_bp=20.0,
            )
            if tr:
                tr["day"] = traj["day"]
                trades.append(tr)

        metrics = compute_grid_metrics(trades, half_cutoff_day)
        grid4_results[name] = metrics
        print(
            f"[{name:24s}] N={metrics['n_trades']:3d} | Net={metrics['mean_net_bp']:+6.2f} bp (A:{metrics['net_a_bp']:+6.2f}, B:{metrics['net_b_bp']:+6.2f}) "
            f"Win={metrics['win_rate_pct']:4.1f}% PF={metrics['profit_factor']:4.2f} t={metrics['t_stat']:4.2f}"
        )

    # Save summary
    out_summary = {
        "baseline_trades": len(m_trades),
        "trajectories_extracted": len(extracted_trajectories),
        "half_cutoff_day": half_cutoff_day,
        "grid1_stop_target": grid1_results,
        "grid2_horizons": grid2_results,
        "grid3_breakeven": grid3_results,
        "grid4_regimes": grid4_results,
    }

    out_file = out_dir / "exhaustive_liquidity_maker_summary.json"
    with open(out_file, "w") as f:
        json.dump(out_summary, f, indent=2)

    print(f"\nExhaustive Liquidity Maker summary written to: {out_file}")


if __name__ == "__main__":
    main()
