#!/usr/bin/env python3
"""exhaustive_catalyst_thrust_sim.py: Exhaustive High-Trust Simulation of Catalyst Open Thrust.

Standard: docs/TEST_STANDARD.md
Pre-registration: docs/studies/catalyst_thrust_family_prereg.json

Evaluates:
  1. Gap thresholds: 5%, 8%, 10%, 12%
  2. Price floors: $10, $20, $30
  3. Bar 0 filter: Green (c[0] > o[0]) vs Strong Body (ratio >= 0.5)
  4. Exit architectures:
     - 1m prior-low ratchet (bars 1..15)
     - 2m prior-low ratchet (bars 1..15)
     - Fixed holds (1m, 2m, 3m, 5m, 10m, 15m)
     - Profit target caps (+40 bp, +60 bp, +80 bp, +100 bp) with ratchet stop
     - Breakeven protection (move stop to entry after +30 bp)
  5. Initial stop loss bounds: -1.0%, -1.5%, -2.0%, unconstrained prior low
  6. MFE (Max Favorable Excursion) and MAE (Max Adverse Excursion) distributions.
  7. Strict chronological half validation (Half A: 2024-01-02..2025-05-31, Half B: 2025-06-01..2026-10-30),
     day-clustered t-statistics, and 1% winsorization.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import random
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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

DATA_DIR = ROOT / "ai_reports" / "catalyst_pullback"
BARS_DIR = DATA_DIR / "bars"
NEWS_DIR = DATA_DIR / "news"
CANDS_FILE = DATA_DIR / "classified_candidates.json"
OUT = ROOT / "ai_reports" / "exhaustive_catalyst_thrust"

ET = ZoneInfo("America/New_York")
COST_STD_BP = 12.0
COST_STRESS_BP = 16.0


def parse_iso_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_premarket_catalyst(news_dt: datetime, session_date_str: str) -> bool:
    """Checks if a catalyst timestamp was published strictly before 09:25:00 ET on session day."""
    dt_et = news_dt.astimezone(ET)
    day_et = dt_et.strftime("%Y-%m-%d")
    if day_et < session_date_str:
        return True
    if day_et == session_date_str:
        # Strictly before 09:25:00 ET
        return (dt_et.hour * 60 + dt_et.minute < 9 * 60 + 25) or (
            dt_et.hour * 60 + dt_et.minute == 9 * 60 + 25 and dt_et.second == 0 and dt_et.microsecond == 0
        )
    return False


def verify_premarket_news(day_str: str, sym: str) -> bool:
    npath = NEWS_DIR / f"{day_str}_{sym}.json"
    if not npath.exists():
        return False
    try:
        with open(npath) as f:
            items = json.load(f)
        if not isinstance(items, list):
            return False
        for it in items:
            t_str = it.get("created_at") or it.get("created")
            if t_str and is_premarket_catalyst(parse_iso_dt(t_str), day_str):
                return True
        return False
    except Exception:
        return False


def load_candidate_bars(c: dict) -> list[dict] | None:
    day = c["day"]
    sym = c["sym"]
    bpath = BARS_DIR / f"{day}_{sym}.json"
    if not bpath.exists():
        return None
    try:
        with open(bpath) as f:
            bars = json.load(f)
        if not isinstance(bars, list) or len(bars) < 16:
            return None
        return bars
    except Exception:
        return None


def seeded_rng(label: str, *parts) -> random.Random:
    seed_str = f"{label}|" + "|".join(str(p) for p in parts)
    seed_int = int(hashlib.sha256(seed_str.encode("utf-8")).hexdigest()[:16], 16)
    return random.Random(seed_int)


def causal_control_pool(ctrl_pool: list[dict], trig_day: str, trig_sym: str, max_lookback_days: int = 60) -> list[dict]:
    same_day = [c for c in ctrl_pool if c["day"] == trig_day and c["sym"] != trig_sym]
    if len(same_day) >= 5:
        return same_day

    dt_trig = datetime.strptime(trig_day, "%Y-%m-%d").date()
    trailing = []
    for c in ctrl_pool:
        if c["sym"] == trig_sym or c["day"] > trig_day:
            continue
        dt_c = datetime.strptime(c["day"], "%Y-%m-%d").date()
        diff_days = (dt_trig - dt_c).days
        if 0 <= diff_days <= max_lookback_days:
            trailing.append((diff_days, c))

    trailing.sort(key=lambda x: x[0])
    trailing_cands = [x[1] for x in trailing]
    if len(trailing_cands) >= 5:
        return trailing_cands

    all_prior = [c for c in ctrl_pool if c["sym"] != trig_sym and c["day"] <= trig_day]
    all_prior.sort(key=lambda c: c["day"], reverse=True)
    return all_prior


def simulate_thrust_trade(
    bars: list[dict],
    start_idx: int = 0,
    H: int = 15,
    ratchet_lookback: int = 1,
    max_loss_frac: float = 0.0200,
    target_bp: float | None = None,
    breakeven_trigger_bp: float | None = None,
    cost_bp: float = COST_STD_BP,
) -> dict | None:
    """Simulates thrust entry at open of bar 1 (09:31 ET) with exact causal stop/target logic."""
    entry_bar_idx = start_idx + 1
    if entry_bar_idx >= len(bars):
        return None

    entry_px = float(bars[entry_bar_idx]["o"])
    if entry_px <= 0.0 or math.isnan(entry_px):
        return None

    trig_low = float(bars[start_idx]["l"])
    if trig_low <= 0.0 or math.isnan(trig_low):
        return None

    initial_stop = trig_low
    if max_loss_frac is not None and max_loss_frac > 0:
        initial_stop = max(initial_stop, entry_px * (1.0 - max_loss_frac))

    s = initial_stop
    target_px = entry_px * (1.0 + target_bp / 10000.0) if target_bp else None
    be_px = entry_px * (1.0 + breakeven_trigger_bp / 10000.0) if breakeven_trigger_bp else None

    exit_px = None
    exit_bar_idx = None
    exit_reason = None

    end_idx = min(start_idx + H, len(bars) - 1)
    if end_idx < entry_bar_idx:
        return None

    mfe_px = entry_px
    mae_px = entry_px

    for k in range(entry_bar_idx, end_idx + 1):
        b = bars[k]
        o_k, h_k, l_k, c_k = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])

        if math.isnan(o_k) or math.isnan(h_k) or math.isnan(l_k) or math.isnan(c_k):
            return None

        mfe_px = max(mfe_px, h_k)
        mae_px = min(mae_px, l_k)

        # 1. Stop loss check (worst-case priority)
        if l_k <= s:
            exit_px = min(s, o_k)  # Gap-through fills at open
            exit_bar_idx = k
            exit_reason = "stop"
            break

        # 2. Profit target check (if active)
        if target_px and h_k >= target_px:
            exit_px = target_px
            exit_bar_idx = k
            exit_reason = "target"
            break

        # 3. Breakeven ratchet trigger (if active)
        if be_px and h_k >= be_px:
            s = max(s, entry_px)

        # 4. Final hold bar exit
        if k == end_idx:
            exit_px = c_k
            exit_bar_idx = k
            exit_reason = "time"
            break

        # 5. Causal ratchet update for bar k+1 (only after bar k completes)
        if ratchet_lookback == 1:
            s = max(s, l_k)
        elif ratchet_lookback == 2 and k > entry_bar_idx:
            s = max(s, min(float(bars[k - 1]["l"]), l_k))

    if exit_px is None or exit_px <= 0.0:
        return None

    gross_bp = ((exit_px - entry_px) / entry_px) * 10000.0
    net_bp = gross_bp - cost_bp
    net_stress_bp = gross_bp - COST_STRESS_BP
    mfe_bp = ((mfe_px - entry_px) / entry_px) * 10000.0
    mae_bp = ((mae_px - entry_px) / entry_px) * 10000.0

    return {
        "entry_px": entry_px,
        "exit_px": exit_px,
        "exit_bar": exit_bar_idx,
        "hold_bars": exit_bar_idx - entry_bar_idx + 1,
        "exit_reason": exit_reason,
        "gross_bp": gross_bp,
        "net": net_bp,
        "net_s": net_stress_bp,
        "mfe_bp": mfe_bp,
        "mae_bp": mae_bp,
    }


def fixed_hold_trade(bars: list[dict], start_idx: int = 0, H: int = 3, cost_bp: float = COST_STD_BP) -> dict | None:
    entry_idx = start_idx + 1
    exit_idx = start_idx + H
    if exit_idx >= len(bars):
        return None
    entry_px = float(bars[entry_idx]["o"])
    exit_px = float(bars[exit_idx]["c"])
    if entry_px <= 0.0 or exit_px <= 0.0 or math.isnan(entry_px) or math.isnan(exit_px):
        return None

    mfe_px = max(float(bars[k]["h"]) for k in range(entry_idx, exit_idx + 1))
    mae_px = min(float(bars[k]["l"]) for k in range(entry_idx, exit_idx + 1))

    gross_bp = ((exit_px - entry_px) / entry_px) * 10000.0
    net_bp = gross_bp - cost_bp
    return {
        "entry_px": entry_px,
        "exit_px": exit_px,
        "exit_bar": exit_idx,
        "hold_bars": H,
        "exit_reason": "time",
        "gross_bp": gross_bp,
        "net": net_bp,
        "net_s": gross_bp - COST_STRESS_BP,
        "mfe_bp": ((mfe_px - entry_px) / entry_px) * 10000.0,
        "mae_bp": ((mae_px - entry_px) / entry_px) * 10000.0,
    }


def compute_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {
            "n_trades": 0, "n_trades_a": 0, "n_trades_b": 0, "n_days": 0,
            "mean_net_bp": 0.0, "net_a_bp": 0.0, "net_b_bp": 0.0,
            "winsor_mean_bp": 0.0, "winsor_a_bp": 0.0, "winsor_b_bp": 0.0,
            "se_day_bp": 0.0, "t_stat": 0.0, "win_rate_pct": 0.0, "profit_factor": 0.0,
            "max_dd_bp": 0.0, "mean_mfe_bp": 0.0, "mfe_p50_bp": 0.0, "mfe_p75_bp": 0.0,
            "mfe_p90_bp": 0.0, "mean_mae_bp": 0.0, "mae_p50_bp": 0.0, "reasons": {},
        }

    by_day = defaultdict(list)
    by_half = {"A": [], "B": []}
    by_day_half = {"A": defaultdict(list), "B": defaultdict(list)}

    nets = []
    lifts = []
    mfes = []
    maes = []
    reasons = defaultdict(int)

    for tr in trades:
        d = tr["day"]
        h = tr["half"]
        n = tr["net"]
        l = tr.get("lift", 0.0)
        by_day[d].append(n)
        by_half[h].append(n)
        by_day_half[h][d].append(n)
        nets.append(n)
        lifts.append(l)
        mfes.append(tr.get("mfe_bp", 0.0))
        maes.append(tr.get("mae_bp", 0.0))
        reasons[tr.get("exit_reason", "unknown")] += 1

    n_trades = len(trades)
    n_days = len(by_day)

    # Day-clustered stats
    day_means = [float(np.mean(vals)) for vals in by_day.values()]
    mean_day_net = float(np.mean(day_means))
    se_day = float(np.std(day_means, ddof=1) / math.sqrt(n_days)) if n_days > 1 else 0.0
    t_stat = mean_day_net / se_day if se_day > 1e-9 else 0.0

    # Half stats
    n_a = len(by_half["A"])
    n_b = len(by_half["B"])
    net_a = float(np.mean(by_half["A"])) if n_a > 0 else 0.0
    net_b = float(np.mean(by_half["B"])) if n_b > 0 else 0.0

    # Winsorized (clip top 1%)
    w_clip = np.percentile(nets, 99.0) if len(nets) >= 100 else np.max(nets)
    w_nets = [min(x, w_clip) for x in nets]
    w_net = float(np.mean(w_nets))

    # Winsorized halves
    w_a = float(np.mean([min(x, w_clip) for x in by_half["A"]])) if n_a > 0 else 0.0
    w_b = float(np.mean([min(x, w_clip) for x in by_half["B"]])) if n_b > 0 else 0.0

    win_rate = float(np.mean([1.0 if x > 0 else 0.0 for x in nets]) * 100.0)
    profit_factor = float(
        sum(x for x in nets if x > 0) / max(1e-6, abs(sum(x for x in nets if x < 0)))
    )

    # Max Drawdown in bp
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
        "mean_mfe_bp": float(np.mean(mfes)),
        "mfe_p50_bp": float(np.median(mfes)),
        "mfe_p75_bp": float(np.percentile(mfes, 75.0)),
        "mfe_p90_bp": float(np.percentile(mfes, 90.0)),
        "mean_mae_bp": float(np.mean(maes)),
        "mae_p50_bp": float(np.median(maes)),
        "reasons": dict(reasons),
    }


def main():
    parser = argparse.ArgumentParser(description="Exhaustive Catalyst Open Thrust Simulation Grid")
    parser.add_argument("--out-dir", default=str(OUT), help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading classified candidates from {CANDS_FILE}...")
    with open(CANDS_FILE) as f:
        cands = json.load(f)

    # Separate primary fundamental catalysts from non-catalyst controls
    primary_cands = [c for c in cands if c.get("is_primary")]
    control_cands = [c for c in cands if not c.get("is_primary")]

    print(f"Total candidates: {len(cands)} (Primaries: {len(primary_cands)}, Controls: {len(control_cands)})")

    # Pre-load bars and verify premarket news for all primaries
    verified_primaries = []
    print("Verifying premarket news and caching bars for primaries...")
    for idx, c in enumerate(primary_cands):
        if idx % 200 == 0:
            print(f"  Processed {idx}/{len(primary_cands)} primaries...")
        day = c["day"]
        sym = c["sym"]
        if not verify_premarket_news(day, sym):
            continue
        bars = load_candidate_bars(c)
        if not bars:
            continue
        c_copy = dict(c)
        c_copy["bars"] = bars
        verified_primaries.append(c_copy)

    print(f"Verified fundamental primaries with bars: {len(verified_primaries)}")

    # Pre-cache bars for controls
    verified_controls = []
    print("Caching bars for causal controls...")
    for idx, c in enumerate(control_cands):
        if idx % 500 == 0:
            print(f"  Processed {idx}/{len(control_cands)} controls...")
        bars = load_candidate_bars(c)
        if not bars:
            continue
        c_copy = dict(c)
        c_copy["bars"] = bars
        verified_controls.append(c_copy)
    print(f"Verified controls with bars: {len(verified_controls)}")

    # Define Simulation Grids
    # 1. Parameter sweep on Gap & Price thresholds (with default 1m Ratchet stop)
    print("\n=======================================================")
    print("RUNNING GRID 1: Gap & Price Thresholds (1m Ratchet Stop)")
    print("=======================================================")

    gap_thresholds = [0.05, 0.08, 0.10, 0.12]
    price_floors = [10.0, 20.0, 30.0]
    grid1_results = {}

    for gap in gap_thresholds:
        for min_px in price_floors:
            key = f"gap_{int(gap*100)}pct_px{int(min_px)}"
            trades = []
            for c in verified_primaries:
                if c["gap"] < gap or c["open_px"] < min_px:
                    continue
                bars = c["bars"]
                # Bar 0 green requirement
                if float(bars[0]["c"]) <= float(bars[0]["o"]):
                    continue
                tr = simulate_thrust_trade(bars, start_idx=0, H=15, ratchet_lookback=1, max_loss_frac=0.0200)
                if tr:
                    tr["day"] = c["day"]
                    tr["half"] = c["half"]
                    trades.append(tr)
            metrics = compute_metrics(trades)
            grid1_results[key] = metrics
            print(
                f"[{key:18s}] N={metrics['n_trades']:3d} (A:{metrics['n_trades_a']:3d}, B:{metrics['n_trades_b']:3d}) "
                f"Days={metrics['n_days']:3d} | Net={metrics['mean_net_bp']:+6.2f} bp (A:{metrics['net_a_bp']:+6.2f}, B:{metrics['net_b_bp']:+6.2f}) "
                f"WinRate={metrics['win_rate_pct']:4.1f}% PF={metrics['profit_factor']:4.2f} t={metrics['t_stat']:4.2f} "
                f"MFE_p50={metrics['mfe_p50_bp']:+5.1f}bp"
            )

    # 2. Parameter sweep on Exit Architectures (with Gap >= 8%, P >= $20, Bar 0 green)
    print("\n=======================================================")
    print("RUNNING GRID 2: Exit Architectures (Gap >= 8%, P >= $20)")
    print("=======================================================")

    exit_variants = [
        ("ratchet_1m_trail", {"H": 15, "ratchet_lookback": 1, "max_loss_frac": 0.0200}),
        ("ratchet_2m_trail", {"H": 15, "ratchet_lookback": 2, "max_loss_frac": 0.0200}),
        ("ratchet_1m_target40", {"H": 15, "ratchet_lookback": 1, "max_loss_frac": 0.0200, "target_bp": 40.0}),
        ("ratchet_1m_target60", {"H": 15, "ratchet_lookback": 1, "max_loss_frac": 0.0200, "target_bp": 60.0}),
        ("ratchet_1m_target80", {"H": 15, "ratchet_lookback": 1, "max_loss_frac": 0.0200, "target_bp": 80.0}),
        ("ratchet_1m_target100", {"H": 15, "ratchet_lookback": 1, "max_loss_frac": 0.0200, "target_bp": 100.0}),
        ("ratchet_1m_be30", {"H": 15, "ratchet_lookback": 1, "max_loss_frac": 0.0200, "breakeven_trigger_bp": 30.0}),
        ("ratchet_tight_stop100bp", {"H": 15, "ratchet_lookback": 1, "max_loss_frac": 0.0100}),
        ("ratchet_wide_stop300bp", {"H": 15, "ratchet_lookback": 1, "max_loss_frac": 0.0300}),
        ("ratchet_unconstrained_stop", {"H": 15, "ratchet_lookback": 1, "max_loss_frac": None}),
    ]

    grid2_results = {}
    for name, params in exit_variants:
        trades = []
        for c in verified_primaries:
            if c["gap"] < 0.08 or c["open_px"] < 20.0:
                continue
            bars = c["bars"]
            if float(bars[0]["c"]) <= float(bars[0]["o"]):
                continue
            tr = simulate_thrust_trade(bars, start_idx=0, **params)
            if tr:
                tr["day"] = c["day"]
                tr["half"] = c["half"]
                trades.append(tr)
        metrics = compute_metrics(trades)
        grid2_results[name] = metrics
        reasons_str = " ".join(f"{k}:{v}" for k, v in metrics.get("reasons", {}).items())
        print(
            f"[{name:28s}] Net={metrics['mean_net_bp']:+6.2f} bp (A:{metrics['net_a_bp']:+6.2f}, B:{metrics['net_b_bp']:+6.2f}) "
            f"WinRate={metrics['win_rate_pct']:4.1f}% PF={metrics['profit_factor']:4.2f} t={metrics['t_stat']:4.2f} "
            f"MaxDD={metrics['max_dd_bp']:5.0f}bp | {reasons_str}"
        )

    # Fixed Horizons Comparison
    print("\n--- Fixed Horizons Comparison (No Ratchet Stop) ---")
    fixed_horizons = [1, 2, 3, 5, 10, 15]
    for h in fixed_horizons:
        name = f"fixed_{h}m_hold"
        trades = []
        for c in verified_primaries:
            if c["gap"] < 0.08 or c["open_px"] < 20.0:
                continue
            bars = c["bars"]
            if float(bars[0]["c"]) <= float(bars[0]["o"]):
                continue
            tr = fixed_hold_trade(bars, start_idx=0, H=h)
            if tr:
                tr["day"] = c["day"]
                tr["half"] = c["half"]
                trades.append(tr)
        metrics = compute_metrics(trades)
        grid2_results[name] = metrics
        print(
            f"[{name:28s}] Net={metrics['mean_net_bp']:+6.2f} bp (A:{metrics['net_a_bp']:+6.2f}, B:{metrics['net_b_bp']:+6.2f}) "
            f"WinRate={metrics['win_rate_pct']:4.1f}% PF={metrics['profit_factor']:4.2f} t={metrics['t_stat']:4.2f} "
            f"MaxDD={metrics['max_dd_bp']:5.0f}bp"
        )

    # 3. Bar 0 Quality Filter: Green vs Strong Body
    print("\n=======================================================")
    print("RUNNING GRID 3: Bar 0 Body Quality (Gap >= 8%, P >= $20)")
    print("=======================================================")
    grid3_results = {}
    bar0_filters = [
        ("bar0_any_green", lambda o, h, l, c: c > o),
        ("bar0_strong_body_ratio_ge_0_5", lambda o, h, l, c: c > o and (c - o) / max(0.01, h - l) >= 0.5),
        ("bar0_large_range_ge_1pct", lambda o, h, l, c: c > o and (h - l) / o >= 0.0100),
    ]

    for name, filt in bar0_filters:
        trades = []
        for c in verified_primaries:
            if c["gap"] < 0.08 or c["open_px"] < 20.0:
                continue
            bars = c["bars"]
            b0 = bars[0]
            if not filt(float(b0["o"]), float(b0["h"]), float(b0["l"]), float(b0["c"])):
                continue
            tr = simulate_thrust_trade(bars, start_idx=0, H=15, ratchet_lookback=1, max_loss_frac=0.0200)
            if tr:
                tr["day"] = c["day"]
                tr["half"] = c["half"]
                trades.append(tr)
        metrics = compute_metrics(trades)
        grid3_results[name] = metrics
        print(
            f"[{name:32s}] N={metrics['n_trades']:3d} | Net={metrics['mean_net_bp']:+6.2f} bp (A:{metrics['net_a_bp']:+6.2f}, B:{metrics['net_b_bp']:+6.2f}) "
            f"WinRate={metrics['win_rate_pct']:4.1f}% PF={metrics['profit_factor']:4.2f} t={metrics['t_stat']:4.2f}"
        )

    # Save complete simulation report
    output_summary = {
        "timestamp": datetime.now(ET).isoformat(),
        "primary_candidates_verified": len(verified_primaries),
        "control_candidates_verified": len(verified_controls),
        "grid1_gap_price": grid1_results,
        "grid2_exit_architectures": grid2_results,
        "grid3_bar0_filters": grid3_results,
    }

    out_file = out_dir / "exhaustive_catalyst_thrust_summary.json"
    with open(out_file, "w") as f:
        json.dump(output_summary, f, indent=2)

    print(f"\nExhaustive simulation summary written to: {out_file}")


if __name__ == "__main__":
    main()
