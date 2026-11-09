#!/usr/bin/env python3
"""recent_months_high_trust_sim.py — High-Trust Simulation on Recent Months (2026).

Standard: docs/TEST_STANDARD.md
Pre-registration: docs/studies/catalyst_thrust_family_prereg.json

Evaluates the proposed Moving + Trending + Holding synergy against:
  1. Last Couple Months (2026-08-01 .. 2026-09-25)
  2. Full Recent Quarter / Q3 2026 (2026-07-01 .. 2026-09-25)
  3. 2026 Year-to-Date (2026-01-02 .. 2026-09-25)

Architectures Evaluated:
  - 1_INCEPTION_RATCHET_15M: Bar 0 green -> enter 09:31 ET, trail 1m prior-low ratchet (bars 1..15).
  - 2_INCEPTION_FIXED_3M: Bar 0 green -> enter 09:31 ET, flat 3-minute exit (testing the 3m choke).
  - 3_INCEPTION_FIXED_5M: Bar 0 green -> enter 09:31 ET, flat 5-minute exit.
  - 4_ORB_BREAKOUT: Wait for price to break strictly above Leg 1 High (H1).
  - 5_MICRO_PULLBACK: Wait for 1-2 bar pause holding above VWAP/open, enter on high break.
  - CONTROL_NON_CATALYST: Run same inception ratchet on non-catalyst gappers to isolate catalyst lift.

Statistical Protections:
  - Day-clustered standard errors and t-statistics.
  - Top 1% winsorization (protecting against outlier skew).
  - Standard (12.0 bp) and Stress (16.0 bp) round-trip friction.
  - MFE and MAE markout distributions.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "ai_reports" / "catalyst_pullback"
BARS_DIR = DATA_DIR / "bars"
NEWS_DIR = DATA_DIR / "news"
CANDS_FILE = DATA_DIR / "classified_candidates.json"
OUT_DIR = ROOT / "ai_reports" / "recent_months_study"

ET = ZoneInfo("America/New_York")
COST_STD_BP = 12.0
COST_STRESS_BP = 16.0


def parse_iso_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_premarket_catalyst(news_dt: datetime, session_date_str: str) -> bool:
    dt_et = news_dt.astimezone(ET)
    day_et = dt_et.strftime("%Y-%m-%d")
    if day_et < session_date_str:
        return True
    if day_et == session_date_str:
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


# ─────────────────────────────────────────────────────────────────────────────
# SIMULATOR CORE
# ─────────────────────────────────────────────────────────────────────────────

def simulate_trade(
    bars: list[dict],
    entry_bar_idx: int,
    entry_px: float,
    initial_stop_px: float,
    max_hold_bars: int = 15,
    fixed_exit_bars: int | None = None,
    breakeven_trigger_bp: float | None = None,
    cost_bp: float = COST_STD_BP,
) -> dict | None:
    if entry_bar_idx >= len(bars) or entry_px <= 0 or initial_stop_px <= 0:
        return None

    s = initial_stop_px
    hold_limit = fixed_exit_bars if fixed_exit_bars is not None else max_hold_bars
    end_idx = min(entry_bar_idx + hold_limit - 1, len(bars) - 1)
    if end_idx < entry_bar_idx:
        return None

    exit_px = None
    exit_bar_idx = None
    exit_reason = None
    mfe_px = entry_px
    mae_px = entry_px
    be_active = False

    for k in range(entry_bar_idx, end_idx + 1):
        b = bars[k]
        o_k, h_k, l_k, c_k = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])

        if math.isnan(o_k) or math.isnan(h_k) or math.isnan(l_k) or math.isnan(c_k):
            return None

        mfe_px = max(mfe_px, h_k)
        mae_px = min(mae_px, l_k)

        # Breakeven lock check: if price touches +be_trigger_bp, ratchet stop to entry + 1c
        if breakeven_trigger_bp is not None and breakeven_trigger_bp > 0:
            if h_k >= entry_px * (1.0 + breakeven_trigger_bp / 10000.0) - 1e-9:
                be_active = True
                s = max(s, entry_px * (1.0 + 1.0 / 10000.0))

        # 1. Stop loss hit check
        if l_k <= s:
            exit_px = min(s, o_k)
            exit_bar_idx = k
            exit_reason = "breakeven" if be_active else "stop"
            break

        # 2. Final hold bar exit (time stop)
        if k == end_idx:
            exit_px = c_k
            exit_bar_idx = k
            exit_reason = "time"
            break

        # 3. Structural ratchet update for bar k+1
        if fixed_exit_bars is None:
            s = max(s, l_k)
            if be_active:
                s = max(s, entry_px * (1.0 + 1.0 / 10000.0))

    if exit_px is None or exit_px <= 0:
        return None

    gross_bp = ((exit_px - entry_px) / entry_px) * 10000.0
    net_bp = gross_bp - cost_bp
    net_stress_bp = gross_bp - COST_STRESS_BP
    mfe_bp = ((mfe_px - entry_px) / entry_px) * 10000.0
    mae_bp = ((mae_px - entry_px) / entry_px) * 10000.0

    return {
        "entry_px": entry_px,
        "entry_bar": entry_bar_idx,
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


def sim_raw_thrust_15m(bars: list[dict]) -> dict | None:
    """Setup 1: Raw Inception Thrust (15m prior-low ratchet, no wick filter, no BE lock)."""
    if float(bars[0]["c"]) <= float(bars[0]["o"]):
        return None
    entry_px = float(bars[1]["o"])
    b0_low = float(bars[0]["l"])
    initial_stop = max(b0_low, entry_px * 0.98)
    return simulate_trade(bars, entry_bar_idx=1, entry_px=entry_px, initial_stop_px=initial_stop, max_hold_bars=15)


def sim_hardened_thrust_15m(bars: list[dict]) -> dict | None:
    """Setup 2: Hardened Inception Thrust with Condition 1 (Wick Filter >= 0.65) and Condition 2 (BE Lock at +30 bp)."""
    b0 = bars[0]
    b0_o, b0_h, b0_l, b0_c = float(b0["o"]), float(b0["h"]), float(b0["l"]), float(b0["c"])
    if b0_c <= b0_o:
        return None
    rng = b0_h - b0_l
    if rng > 0:
        close_pos = (b0_c - b0_l) / rng
        if close_pos < 0.65:
            return None  # Veto blow-off wick
    entry_px = float(bars[1]["o"])
    initial_stop = max(b0_l, entry_px * 0.98)
    return simulate_trade(
        bars, entry_bar_idx=1, entry_px=entry_px, initial_stop_px=initial_stop,
        max_hold_bars=15, breakeven_trigger_bp=30.0
    )


def sim_hardened_thrust_no_be(bars: list[dict]) -> dict | None:
    """Setup 3: Hardened Inception Thrust with Condition 1 only (Wick Filter >= 0.65, NO BE Lock)."""
    b0 = bars[0]
    b0_o, b0_h, b0_l, b0_c = float(b0["o"]), float(b0["h"]), float(b0["l"]), float(b0["c"])
    if b0_c <= b0_o:
        return None
    rng = b0_h - b0_l
    if rng > 0:
        close_pos = (b0_c - b0_l) / rng
        if close_pos < 0.65:
            return None  # Veto blow-off wick
    entry_px = float(bars[1]["o"])
    initial_stop = max(b0_l, entry_px * 0.98)
    return simulate_trade(
        bars, entry_bar_idx=1, entry_px=entry_px, initial_stop_px=initial_stop,
        max_hold_bars=15, breakeven_trigger_bp=None
    )


def sim_hardened_thrust_fixed_3m(bars: list[dict]) -> dict | None:
    """Setup 4: Hardened Inception Thrust (Wick Filter >= 0.65) with strict 3-minute exit."""
    b0 = bars[0]
    b0_o, b0_h, b0_l, b0_c = float(b0["o"]), float(b0["h"]), float(b0["l"]), float(b0["c"])
    if b0_c <= b0_o:
        return None
    rng = b0_h - b0_l
    if rng > 0:
        close_pos = (b0_c - b0_l) / rng
        if close_pos < 0.65:
            return None
    entry_px = float(bars[1]["o"])
    initial_stop = max(b0_l, entry_px * 0.98)
    return simulate_trade(
        bars, entry_bar_idx=1, entry_px=entry_px, initial_stop_px=initial_stop,
        fixed_exit_bars=3, breakeven_trigger_bp=30.0
    )


def sim_raw_fixed_3m(bars: list[dict]) -> dict | None:
    """Setup 5: Raw Inception Thrust with strict 3-minute exit (no wick check, no BE lock)."""
    if float(bars[0]["c"]) <= float(bars[0]["o"]):
        return None
    entry_px = float(bars[1]["o"])
    b0_low = float(bars[0]["l"])
    initial_stop = max(b0_low, entry_px * 0.98)
    return simulate_trade(bars, entry_bar_idx=1, entry_px=entry_px, initial_stop_px=initial_stop, fixed_exit_bars=3)


def sim_inception_fixed_5m(bars: list[dict]) -> dict | None:
    if float(bars[0]["c"]) <= float(bars[0]["o"]):
        return None
    entry_px = float(bars[1]["o"])
    b0_low = float(bars[0]["l"])
    initial_stop = max(b0_low, entry_px * 0.98)
    return simulate_trade(bars, entry_bar_idx=1, entry_px=entry_px, initial_stop_px=initial_stop, fixed_exit_bars=5)


def sim_orb_breakout(bars: list[dict], max_arm_bars: int = 5) -> dict | None:
    if float(bars[0]["c"]) <= float(bars[0]["o"]):
        return None
    b0_high = float(bars[0]["h"])
    b0_low = float(bars[0]["l"])

    for k in range(1, min(max_arm_bars + 1, len(bars))):
        o_k, h_k, l_k = float(bars[k]["o"]), float(bars[k]["h"]), float(bars[k]["l"])
        if h_k > b0_high:
            entry_px = max(b0_high, o_k)
            initial_stop = max(b0_low, entry_px * 0.98)
            return simulate_trade(bars, entry_bar_idx=k, entry_px=entry_px, initial_stop_px=initial_stop, max_hold_bars=15)
        if l_k < b0_low:
            return None
    return None


def sim_micro_pullback(bars: list[dict], max_lookback_bars: int = 4) -> dict | None:
    if float(bars[0]["c"]) <= float(bars[0]["o"]):
        return None
    b0_low = float(bars[0]["l"])
    b0_open = float(bars[0]["o"])

    for pause_idx in range(1, min(max_lookback_bars, len(bars) - 1)):
        p_o, p_h, p_l, p_c = float(bars[pause_idx]["o"]), float(bars[pause_idx]["h"]), float(bars[pause_idx]["l"]), float(bars[pause_idx]["c"])
        if p_l < b0_low or p_l < b0_open:
            return None
        prior_h = float(bars[pause_idx - 1]["h"])
        is_pause = (p_c <= p_o) or (p_h <= prior_h) or (p_l < float(bars[pause_idx - 1]["l"]))
        if is_pause:
            next_idx = pause_idx + 1
            if next_idx >= len(bars):
                return None
            n_o, n_h, n_l = float(bars[next_idx]["o"]), float(bars[next_idx]["h"]), float(bars[next_idx]["l"])
            if n_l < b0_low:
                return None
            if n_h > p_h:
                entry_px = max(p_h, n_o)
                initial_stop = max(p_l, entry_px * 0.98)
                return simulate_trade(bars, entry_bar_idx=next_idx, entry_px=entry_px, initial_stop_px=initial_stop, max_hold_bars=15)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HIGH-TRUST STATS
# ─────────────────────────────────────────────────────────────────────────────

def compute_high_trust_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {
            "n_trades": 0, "n_days": 0, "mean_net_bp": 0.0, "net_stress_bp": 0.0,
            "winsor_net_bp": 0.0, "se_day_bp": 0.0, "t_stat": 0.0, "win_rate_pct": 0.0,
            "profit_factor": 0.0, "max_dd_bp": 0.0, "mfe_p50_bp": 0.0, "mfe_p90_bp": 0.0,
            "mae_p50_bp": 0.0,
        }

    by_day = defaultdict(list)
    nets = []
    nets_stress = []
    mfes = []
    maes = []

    for tr in trades:
        d = tr["day"]
        n = tr["net"]
        ns = tr["net_s"]
        by_day[d].append(n)
        nets.append(n)
        nets_stress.append(ns)
        mfes.append(tr.get("mfe_bp", 0.0))
        maes.append(tr.get("mae_bp", 0.0))

    n_trades = len(trades)
    n_days = len(by_day)

    day_means = [float(np.mean(vals)) for vals in by_day.values()]
    mean_day_net = float(np.mean(day_means))
    se_day = float(np.std(day_means, ddof=1) / math.sqrt(n_days)) if n_days > 1 else 0.0
    t_stat = (mean_day_net / se_day) if se_day > 1e-9 else 0.0

    # 1% Winsorization (clip top 1% outliers)
    clip_val = np.percentile(nets, 99.0) if len(nets) >= 100 else np.max(nets)
    winsor_nets = [min(x, clip_val) for x in nets]
    winsor_mean = float(np.mean(winsor_nets))

    wins = [x for x in nets if x > 0]
    losses = [abs(x) for x in nets if x < 0]
    win_rate = (len(wins) / n_trades) * 100.0 if n_trades > 0 else 0.0
    pf = (sum(wins) / sum(losses)) if sum(losses) > 0 else 999.0

    cum = np.cumsum(nets)
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    max_dd = float(np.max(dd)) if len(dd) > 0 else 0.0

    return {
        "n_trades": n_trades,
        "n_days": n_days,
        "mean_net_bp": float(np.mean(nets)),
        "net_stress_bp": float(np.mean(nets_stress)),
        "winsor_net_bp": winsor_mean,
        "se_day_bp": se_day,
        "t_stat": t_stat,
        "win_rate_pct": win_rate,
        "profit_factor": pf,
        "max_dd_bp": max_dd,
        "mfe_p50_bp": float(np.median(mfes)),
        "mfe_p90_bp": float(np.percentile(mfes, 90.0)),
        "mae_p50_bp": float(np.median(maes)),
    }


def evaluate_window(primaries: list[dict], controls: list[dict], start_date: str, end_date: str, label: str):
    p_window = [c for c in primaries if start_date <= c["day"] <= end_date]
    c_window = [c for c in controls if start_date <= c["day"] <= end_date]

    print(f"\n{'='*75}")
    print(f"WINDOW: {label} [{start_date} to {end_date}]")
    print(f"Candidates: {len(p_window)} Primaries, {len(c_window)} Controls")
    print(f"{'='*75}")

    setups = [
        ("1_RAW_THRUST_15M", sim_raw_thrust_15m),
        ("2_HARDENED_THRUST_15M", sim_hardened_thrust_15m),
        ("3_HARDENED_THRUST_NO_BE", sim_hardened_thrust_no_be),
        ("4_HARDENED_FIXED_3M", sim_hardened_thrust_fixed_3m),
        ("5_RAW_FIXED_3M", sim_raw_fixed_3m),
    ]

    out_summary = {}

    for name, fn in setups:
        trades = []
        for c in p_window:
            res = fn(c["bars"])
            if res:
                res["day"] = c["day"]
                res["sym"] = c["sym"]
                trades.append(res)
        m = compute_high_trust_metrics(trades)
        out_summary[name] = m
        print(f"--- {name} ---")
        print(f"  Trades: {m['n_trades']} over {m['n_days']} sessions | Win Rate: {m['win_rate_pct']:.1f}% | PF: {m['profit_factor']:.2f}")
        print(f"  Net P&L: {m['mean_net_bp']:+.2f} bp (Stress Cost: {m['net_stress_bp']:+.2f} bp) | Winsorized: {m['winsor_net_bp']:+.2f} bp")
        print(f"  Day t-stat: {m['t_stat']:+.2f} (SE: {m['se_day_bp']:.1f} bp) | Max DD: {m['max_dd_bp']:.0f} bp")
        print(f"  Median MFE: {m['mfe_p50_bp']:+.1f} bp | 90th pct MFE: {m['mfe_p90_bp']:+.1f} bp | Median MAE: {m['mae_p50_bp']:+.1f} bp\n")

    # Non-catalyst control benchmark on Setup 1
    ctrl_trades = []
    for c in c_window:
        res = sim_raw_thrust_15m(c["bars"])
        if res:
            res["day"] = c["day"]
            res["sym"] = c["sym"]
            ctrl_trades.append(res)
    ctrl_m = compute_high_trust_metrics(ctrl_trades)
    out_summary["CONTROL_NON_CATALYST"] = ctrl_m
    print(f"--- CONTROL_NON_CATALYST (Inception Ratchet on Non-News Gappers) ---")
    print(f"  Trades: {ctrl_m['n_trades']} over {ctrl_m['n_days']} sessions | Net: {ctrl_m['mean_net_bp']:+.2f} bp | PF: {ctrl_m['profit_factor']:.2f}")
    lift = out_summary["2_HARDENED_THRUST_15M"]["mean_net_bp"] - ctrl_m["mean_net_bp"]
    print(f"  >>> HARDENED THRUST CATALYST LIFT VS CONTROLS: {lift:+.2f} bp <<<\n")

    return out_summary


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Loading candidate records from {CANDS_FILE}...")
    with open(CANDS_FILE) as f:
        cands = json.load(f)

    primaries = [c for c in cands if c.get("is_primary")]
    controls = [c for c in cands if not c.get("is_primary")]

    print("Verifying news and caching bars for primaries...")
    verified_primaries = []
    for c in primaries:
        if verify_premarket_news(c["day"], c["sym"]):
            b = load_candidate_bars(c)
            if b:
                c_copy = dict(c)
                c_copy["bars"] = b
                verified_primaries.append(c_copy)

    print("Caching bars for controls...")
    verified_controls = []
    for c in controls:
        b = load_candidate_bars(c)
        if b:
            c_copy = dict(c)
            c_copy["bars"] = b
            verified_controls.append(c_copy)

    print(f"Loaded {len(verified_primaries)} verified primaries and {len(verified_controls)} controls.")

    results_all = {}

    # 1. Last Couple Months (August 1, 2026 to September 25, 2026)
    results_all["LAST_COUPLE_MONTHS"] = evaluate_window(
        verified_primaries, verified_controls,
        start_date="2026-08-01", end_date="2026-09-25",
        label="LAST_COUPLE_MONTHS (Aug - Sep 2026)"
    )

    # 2. Recent Quarter / Q3 2026 (July 1, 2026 to September 25, 2026)
    results_all["Q3_2026"] = evaluate_window(
        verified_primaries, verified_controls,
        start_date="2026-07-01", end_date="2026-09-25",
        label="Q3_2026 (July - Sep 2026)"
    )

    # 3. Full 2026 Year-to-Date (January 1, 2026 to September 25, 2026)
    results_all["FULL_YEAR_2026"] = evaluate_window(
        verified_primaries, verified_controls,
        start_date="2026-01-02", end_date="2026-09-25",
        label="FULL_YEAR_2026 (YTD 2026)"
    )

    # 4. Full Historical Sample (January 2, 2024 to September 25, 2026) - 684 Sessions
    results_all["FULL_HISTORICAL_SAMPLE"] = evaluate_window(
        verified_primaries, verified_controls,
        start_date="2024-01-02", end_date="2026-09-25",
        label="FULL_HISTORICAL_SAMPLE (2024 - 2026, 684 Sessions)"
    )

    out_file = OUT_DIR / "recent_months_high_trust_results.json"
    with open(out_file, "w") as f:
        json.dump(results_all, f, indent=2)
    print(f"\nAll high-trust results saved to {out_file}")


if __name__ == "__main__":
    main()
