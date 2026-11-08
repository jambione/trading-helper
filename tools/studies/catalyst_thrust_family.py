#!/usr/bin/env python3
"""catalyst_thrust_family.py: Scored study of the Morning Catalyst Open Thrust Family.

Pre-registration: docs/studies/catalyst_thrust_family_prereg.json
Test Standard: docs/TEST_STANDARD.md (EDGE vs PROFITABLE, P2 controls with 11/07 amendment).

Scored Hypotheses:
  1. H_OPEN: Verified fundamental catalyst (is_primary=True, verified premarket news in news/{day}_{sym}.json),
     gap >= +5.0%, bar 0 green, causal entry at bar 1 open (09:31 ET), causal 1-minute prior-low ratchet stop (bars 1..15).
  2. H_OPEN_GAP8: High-gap (gap >= +8.0%) fundamental catalyst, verified premarket news, bar 0 green, causal 1m prior-low ratchet stop.

Execution Modes:
  count:  Outcome-blind sample count, power verification, canary plan generation.
  run:    Strict single outcome read, preceded by mandatory planted (+8 bp) and null canaries.
  report: Regenerates docs/CATALYST_THRUST_FAMILY.md from run.pkl.
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
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np

try:
    import scipy.stats as stats
except ImportError:
    stats = None

ROOT = Path(os.environ.get("EEH_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

DATA_DIR = ROOT / "ai_reports" / "catalyst_pullback"
BARS_DIR = DATA_DIR / "bars"
NEWS_DIR = DATA_DIR / "news"
CANDS_FILE = DATA_DIR / "classified_candidates.json"
OUT = ROOT / "ai_reports" / "catalyst_thrust_family"
PREREG = ROOT / "docs" / "studies" / "catalyst_thrust_family_prereg.json"
REPORT_MD = ROOT / "docs" / "CATALYST_THRUST_FAMILY.md"

ET = ZoneInfo("America/New_York")
KEYS = ("H_OPEN", "H_OPEN_GAP8")

# Cost friction per prereg
COST_STD_BP = 12.0
COST_STRESS_BP = 16.0
MAX_HOLD_BARS = 15
MAX_LOSS_FRAC = 0.0200  # -200 bp max initial stop distance

# Power requirements per docs/TEST_STANDARD.md
MIN_TRIGGERS_TOTAL = 300
MIN_TRIGGERS_HALF = 100
MIN_DAYS_TOTAL = 60
MIN_DAYS_HALF = 25
MAX_BASELINE_MISMATCH_BP = 3.0
CANARY_PLANTED_TOL_BP = 0.5
CANARY_CTRL_SHIFT_MAX_BP = 0.5


def parse_iso_dt(s: str) -> datetime:
    """Parses ISO timestamp string reliably across Python versions."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def entry_bar_for_news(news_dt: datetime, bars: list[dict]) -> int | None:
    """Finds the causal entry bar index for a breaking news timestamp.
    
    Rule: Entry must occur at the open of the first bar whose open timestamp is
    strictly greater than news_dt (ts_open > news_dt). Never enters in a past bar
    or the bar currently in progress when the news broke.
    """
    for idx, b in enumerate(bars):
        b_dt = parse_iso_dt(b["t"])
        if b_dt > news_dt:
            return idx
    return None


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
    """Verifies that actual news/{day}_{sym}.json has at least one premarket news item (< 09:25:00 ET)."""
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


def ratchet_trade(
    bars: list[dict],
    start_idx: int = 0,
    H: int = MAX_HOLD_BARS,
    cost_bp: float = COST_STD_BP,
) -> dict | None:
    """Simulates a causal 1-minute prior-low ratchet stop trade.
    
    Causal Anti-Peeking Rules:
      1. Trigger bar is start_idx (e.g. bar 0, 09:30).
      2. Entry price is strictly the OPEN of bar start_idx + 1 (e.g. bar 1, 09:31).
         Never fills at trigger bar open or close.
      3. Initial stop for bar k = start_idx + 1:
         s = max(bars[start_idx]["l"], entry_px * (1.0 - MAX_LOSS_FRAC))
      4. For each subsequent bar k in [start_idx + 1 .. start_idx + H]:
         - Check if l[k] <= s:
           If so, exit at min(s, o[k]) (gaps below s fill at worse open price o[k]!).
         - Stop update: Only AFTER bar k completes without stopping out,
           s = max(s, l[k]) for bar k+1.
      5. If not stopped out after bar start_idx + H, exit at bars[start_idx + H]["c"].
      6. Planted canaries: if bars[exit_bar_idx] contains 'planted_exit_bp', that exit is scaled.
    """
    entry_bar_idx = start_idx + 1
    if entry_bar_idx >= len(bars):
        return None

    entry_bar = bars[entry_bar_idx]
    entry_px = float(entry_bar["o"])
    if entry_px <= 0.0 or math.isnan(entry_px):
        return None

    trigger_bar = bars[start_idx]
    trig_low = float(trigger_bar["l"])
    if trig_low <= 0.0 or math.isnan(trig_low):
        return None

    s = max(trig_low, entry_px * (1.0 - MAX_LOSS_FRAC))
    
    exit_px = None
    exit_bar_idx = None
    stopped = False

    end_idx = min(start_idx + H, len(bars) - 1)
    if end_idx < entry_bar_idx:
        return None

    for k in range(entry_bar_idx, end_idx + 1):
        b = bars[k]
        o_k = float(b["o"])
        l_k = float(b["l"])
        c_k = float(b["c"])

        if math.isnan(o_k) or math.isnan(l_k) or math.isnan(c_k):
            return None

        # Check if stop is hit during bar k
        if l_k <= s:
            exit_px = min(s, o_k)
            exit_bar_idx = k
            stopped = True
            break

        # If bar k is the final hold bar and didn't stop out, exit at close of bar k
        if k == end_idx:
            exit_px = c_k
            exit_bar_idx = k
            break

        # Causal ratchet for bar k+1: only uses bar k low AFTER bar k completes
        s = max(s, l_k)

    if exit_px is None or exit_px <= 0.0:
        return None

    # Check for planted exit bump in bar metadata (canary testing)
    planted_bp = float(bars[exit_bar_idx].get("planted_exit_bp", 0.0))
    if planted_bp != 0.0:
        exit_px = exit_px * (1.0 + planted_bp / 10000.0)

    gross_bp = ((exit_px - entry_px) / entry_px) * 10000.0
    net_bp = gross_bp - cost_bp
    net_stress_bp = gross_bp - COST_STRESS_BP

    return {
        "entry_px": entry_px,
        "exit_px": exit_px,
        "exit_bar": exit_bar_idx,
        "stopped": stopped,
        "gross_bp": gross_bp,
        "net": net_bp,
        "net_s": net_stress_bp,
    }


def fixed_horizon_trade(bars: list[dict], start_idx: int = 0, H: int = 3, cost_bp: float = COST_STD_BP) -> dict | None:
    """Fixed-horizon hold without ratchet stop: buy open(start_idx+1), sell close(start_idx+H)."""
    entry_idx = start_idx + 1
    exit_idx = start_idx + H
    if exit_idx >= len(bars):
        return None
    entry_px = float(bars[entry_idx]["o"])
    exit_px = float(bars[exit_idx]["c"])
    if entry_px <= 0.0 or exit_px <= 0.0 or math.isnan(entry_px) or math.isnan(exit_px):
        return None
    gross_bp = ((exit_px - entry_px) / entry_px) * 10000.0
    return {"net": gross_bp - cost_bp, "gross_bp": gross_bp}


def seeded(label: str, *parts) -> random.Random:
    """Deterministic RNG seeded by sha256 hash."""
    seed_str = f"{label}|" + "|".join(str(p) for p in parts)
    seed_int = int(hashlib.sha256(seed_str.encode("utf-8")).hexdigest()[:16], 16)
    return random.Random(seed_int)


def sample_k_deterministic(rng: random.Random, pool: list, k: int = 5) -> list:
    """Deterministically samples up to k items from a list."""
    p = list(pool)
    if len(p) <= k:
        return p
    return rng.sample(p, k)


def causal_control_pool(ctrl_pool: list[dict], trig_day: str, trig_sym: str, max_lookback_days: int = 60) -> list[dict]:
    """Builds a strictly causal non-catalyst control pool for a trigger at (trig_day, trig_sym).
    
    Guarantees:
      1. Zero look-ahead: Only candidates with c["day"] <= trig_day are eligible (never c["day"] > trig_day).
      2. No same-symbol overlap: c["sym"] != trig_sym.
      3. Proximity / regime matching:
         - Same-day candidates (c["day"] == trig_day) preferred first.
         - If fewer than 5 candidates on the same day, draw from trailing candidates within max_lookback_days (c["day"] <= trig_day).
         - Fallback: all prior candidates with c["day"] <= trig_day.
    """
    # 1. Same-day non-catalyst controls
    same_day = [c for c in ctrl_pool if c["day"] == trig_day and c["sym"] != trig_sym]
    if len(same_day) >= 5:
        return same_day

    # 2. Trailing causal candidates within trailing lookback window
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

    # 3. Fallback for the first few sessions: all prior candidates with c["day"] <= trig_day
    all_prior = [c for c in ctrl_pool if c["sym"] != trig_sym and c["day"] <= trig_day]
    all_prior.sort(key=lambda c: c["day"], reverse=True)
    return all_prior


def load_candidate_bars(c: dict) -> list[dict] | None:
    """Loads 1-minute bars for a candidate from ai_reports/catalyst_pullback/bars/."""
    day = c["day"]
    sym = c["sym"]
    bpath = BARS_DIR / f"{day}_{sym}.json"
    if not bpath.exists():
        return None
    try:
        with open(bpath) as f:
            bars = json.load(f)
        return bars if isinstance(bars, list) and len(bars) >= 16 else None
    except Exception:
        return None


def get_candidate_features(c: dict, bars: list[dict]) -> dict | None:
    """Extracts opening anatomy, verifies bar 0/1 timestamps causality, and validates premarket news."""
    if len(bars) < 16:
        return None

    b0 = bars[0]
    b1 = bars[1]
    
    # Verify bar 0 timestamp is 09:30 ET and bar 1 is 09:31 ET
    dt0 = parse_iso_dt(b0["t"]).astimezone(ET)
    dt1 = parse_iso_dt(b1["t"]).astimezone(ET)
    if (dt0.hour, dt0.minute) != (9, 30) or (dt1.hour, dt1.minute) != (9, 31):
        return None

    entry_px = float(b1["o"])
    if entry_px < 10.0:
        return None

    b0_o, b0_h, b0_l, b0_c = float(b0["o"]), float(b0["h"]), float(b0["l"]), float(b0["c"])
    b0_rng = b0_h - b0_l
    if b0_rng <= 0.0 or b0_o <= 0.0:
        return None

    b0_is_green = b0_c > b0_o
    b0_body_ratio = (b0_c - b0_o) / b0_rng if b0_rng > 0 else 0.0

    gap = float(c.get("gap") or 0.0)
    is_primary = bool(c.get("is_primary"))

    # Active verification of premarket news in news/{day}_{sym}.json
    has_premarket_news = False
    if is_primary:
        has_premarket_news = verify_premarket_news(c["day"], c["sym"])

    return {
        "day": c["day"],
        "sym": c["sym"],
        "half": c.get("half", "A"),
        "gap": gap,
        "is_primary": is_primary,
        "has_premarket_news": has_premarket_news,
        "entry_px": entry_px,
        "b0_is_green": b0_is_green,
        "b0_body_ratio": b0_body_ratio,
        "bars": bars,
    }


def filter_triggers(features_list: list[dict], hyp: str) -> list[dict]:
    """Filters candidate features for a specific hypothesis."""
    triggers = []
    for f in features_list:
        if not (f["is_primary"] and f["has_premarket_news"]):
            continue
        if not f["b0_is_green"]:
            continue
        if f["entry_px"] < 10.0:
            continue

        if hyp == "H_OPEN":
            if f["gap"] >= 0.05:
                triggers.append(f)
        elif hyp == "H_OPEN_GAP8":
            if f["gap"] >= 0.08:
                triggers.append(f)
    return triggers


def build_control_pool(features_list: list[dict]) -> list[dict]:
    """Builds the eligible non-catalyst gapper pool for Primary Controls (ca) and BASE."""
    pool = []
    for f in features_list:
        if f["is_primary"]:
            continue
        if not f["b0_is_green"]:
            continue
        if f["entry_px"] < 10.0:
            continue
        if f["gap"] >= 0.05:
            pool.append(f)
    return pool


def later_session_minutes(bars: list[dict], min_bar: int = 30, max_bar: int = 90, H: int = MAX_HOLD_BARS) -> list[int]:
    """Finds eligible green bar minutes in [min_bar, max_bar] (10:00 - 11:00 ET) for Secondary Control (cb)."""
    valid = []
    for m in range(min_bar, min(max_bar, len(bars) - H - 2) + 1):
        bm = bars[m]
        o_m, c_m = float(bm["o"]), float(bm["c"])
        if c_m > o_m:
            valid.append(m)
    return valid


def score_triggers_for_hyp(
    triggers: list[dict],
    ctrl_pool: list[dict],
    hyp: str,
) -> tuple[list[dict], list[dict], dict]:
    """Scores triggers, primary controls (ca), secondary controls (cb), and baseline (BASE).
    
    Guarantees:
      - Trigger trade enters strictly at bar 1 open with causal ratchet stop.
      - ca: 5 seeded non-catalyst gappers drawn from strictly causal pool (c['day'] <= day).
      - cb: 5 seeded later minutes m in [30, 90] on the SAME stock with exact same ratchet stop.
      - BASE: 1 seeded non-catalyst candidate drawn from the same causal pool for baseline sanity check.
    """
    rows = []
    base_rows = []
    stats_dict = defaultdict(int)

    for trig in triggers:
        day = trig["day"]
        sym = trig["sym"]
        half = trig["half"]
        bars = trig["bars"]

        # Run trigger trade
        res_trig = ratchet_trade(bars, start_idx=0, H=MAX_HOLD_BARS, cost_bp=COST_STD_BP)
        if res_trig is None:
            stats_dict[f"{hyp}_skipped_trade"] += 1
            continue

        # Build strictly causal control pool (c["day"] <= day and c["sym"] != sym)
        ca_eligible = causal_control_pool(ctrl_pool, day, sym)
        rng_ca = seeded("ca", hyp, day, sym)
        sampled_ca = sample_k_deterministic(rng_ca, ca_eligible, k=5)
        ca_vals = []
        for c_cand in sampled_ca:
            r_c = ratchet_trade(c_cand["bars"], start_idx=0, H=MAX_HOLD_BARS, cost_bp=COST_STD_BP)
            if r_c is not None:
                ca_vals.append(r_c["net"])
        ca_mean = float(np.mean(ca_vals)) if ca_vals else None

        # Secondary Control (cb): 5 seeded later minutes m in [30, 90] on SAME stock
        later_m = later_session_minutes(bars, min_bar=30, max_bar=90, H=MAX_HOLD_BARS)
        rng_cb = seeded("cb", hyp, day, sym)
        sampled_cb_m = sample_k_deterministic(rng_cb, later_m, k=5) if later_m else []
        cb_vals = []
        for m in sampled_cb_m:
            r_cb = ratchet_trade(bars, start_idx=m, H=MAX_HOLD_BARS, cost_bp=COST_STD_BP)
            if r_cb is not None:
                cb_vals.append(r_cb["net"])
        cb_mean = float(np.mean(cb_vals)) if cb_vals else None

        # Baseline (BASE): 1 seeded candidate drawn from the exact same causal pool
        rng_base = seeded("baseline", hyp, day, sym)
        if ca_eligible:
            b_cand = rng_base.choice(ca_eligible)
            r_b = ratchet_trade(b_cand["bars"], start_idx=0, H=MAX_HOLD_BARS, cost_bp=COST_STD_BP)
            if r_b is not None:
                base_rows.append({"key": hyp, "net": r_b["net"]})

        rows.append({
            "hyp": hyp,
            "day": day,
            "sym": sym,
            "half": half,
            "net": res_trig["net"],
            "net_s": res_trig["net_s"],
            "ca": ca_mean,
            "cb": cb_mean,
            "stopped": res_trig["stopped"],
            "exit_bar": res_trig["exit_bar"],
        })

    return rows, base_rows, dict(stats_dict)


def evaluate(rows: list[dict], base_rows: list[dict], hyp: str) -> dict:
    """Computes TEST_STANDARD verdicts: EDGE and PROFITABLE."""
    if not rows:
        return {"status": "FAIL", "reason": "no rows"}

    valid_rows = [r for r in rows if r.get("ca") is not None]
    if not valid_rows:
        return {"status": "FAIL", "reason": "no rows with valid primary control"}

    n_trades = len(valid_rows)
    trades_a = [r for r in valid_rows if r["half"] == "A"]
    trades_b = [r for r in valid_rows if r["half"] == "B"]
    n_trades_a = len(trades_a)
    n_trades_b = len(trades_b)

    days_set = sorted(set(r["day"] for r in valid_rows))
    days_a = sorted(set(r["day"] for r in trades_a))
    days_b = sorted(set(r["day"] for r in trades_b))
    n_days = len(days_set)
    n_days_a = len(days_a)
    n_days_b = len(days_b)

    by_day_lift = defaultdict(list)
    by_day_cb_lift = defaultdict(list)
    by_day_net = defaultdict(list)
    by_day_net_s = defaultdict(list)
    trade_lifts = []

    for r in valid_rows:
        lift = r["net"] - r["ca"]
        trade_lifts.append(lift)
        by_day_lift[r["day"]].append(lift)
        by_day_net[r["day"]].append(r["net"])
        if r.get("net_s") is not None:
            by_day_net_s[r["day"]].append(r["net_s"])
        if r.get("cb") is not None:
            by_day_cb_lift[r["day"]].append(r["net"] - r["cb"])

    day_mean_lifts = [float(np.mean(by_day_lift[d])) for d in days_set]
    day_mean_lifts_a = [float(np.mean(by_day_lift[d])) for d in days_a]
    day_mean_lifts_b = [float(np.mean(by_day_lift[d])) for d in days_b]

    pooled_lift = float(np.mean(day_mean_lifts)) if day_mean_lifts else 0.0
    lift_a = float(np.mean(day_mean_lifts_a)) if day_mean_lifts_a else 0.0
    lift_b = float(np.mean(day_mean_lifts_b)) if day_mean_lifts_b else 0.0

    se = float(np.std(day_mean_lifts, ddof=1) / math.sqrt(n_days)) if n_days > 1 else 999.0
    t_stat = pooled_lift / se if se > 0 else 0.0

    if stats is not None and n_days > 1:
        t_crit = float(stats.t.ppf(1.0 - 0.025, df=n_days - 1))
    else:
        t_crit = 1.96

    day_mean_cb = [float(np.mean(by_day_cb_lift[d])) for d in by_day_cb_lift]
    pooled_cb_lift = float(np.mean(day_mean_cb)) if day_mean_cb else 0.0

    day_mean_net = [float(np.mean(by_day_net[d])) for d in days_set]
    day_mean_net_a = [float(np.mean(by_day_net[d])) for d in days_a]
    day_mean_net_b = [float(np.mean(by_day_net[d])) for d in days_b]
    day_mean_net_s = [float(np.mean(by_day_net_s[d])) for d in by_day_net_s]

    mean_net = float(np.mean(day_mean_net)) if day_mean_net else 0.0
    mean_net_a = float(np.mean(day_mean_net_a)) if day_mean_net_a else 0.0
    mean_net_b = float(np.mean(day_mean_net_b)) if day_mean_net_b else 0.0
    mean_net_s = float(np.mean(day_mean_net_s)) if day_mean_net_s else 0.0

    if trade_lifts:
        p99 = float(np.percentile(trade_lifts, 99.0))
        winsor_lifts_a = [min(r["net"] - r["ca"], p99) for r in trades_a]
        winsor_lifts_b = [min(r["net"] - r["ca"], p99) for r in trades_b]
        win_a = float(np.mean(winsor_lifts_a)) if winsor_lifts_a else 0.0
        win_b = float(np.mean(winsor_lifts_b)) if winsor_lifts_b else 0.0
    else:
        win_a, win_b = 0.0, 0.0

    ca_all = [r["ca"] for r in valid_rows]
    mean_ca = float(np.mean(ca_all)) if ca_all else 0.0
    b_all = [r["net"] for r in base_rows if r.get("key") == hyp]
    mean_base = float(np.mean(b_all)) if b_all else mean_ca
    baseline_diff = abs(mean_ca - mean_base)

    status = "FAIL"
    underpowered = (
        n_trades < MIN_TRIGGERS_TOTAL
        or n_trades_a < MIN_TRIGGERS_HALF
        or n_trades_b < MIN_TRIGGERS_HALF
        or n_days < MIN_DAYS_TOTAL
        or n_days_a < MIN_DAYS_HALF
        or n_days_b < MIN_DAYS_HALF
    )

    if baseline_diff > MAX_BASELINE_MISMATCH_BP:
        status = "VOID"
    elif underpowered:
        status = "UNDERPOWERED"
    else:
        is_edge = (
            t_stat >= t_crit
            and lift_a > 0.0
            and lift_b > 0.0
            and pooled_cb_lift > 0.0
            and win_a > 0.0
            and win_b > 0.0
        )
        if is_edge:
            is_profitable = (
                mean_net_a > 0.0
                and mean_net_b > 0.0
                and mean_net_s > 0.0
            )
            status = "PROFITABLE" if is_profitable else "EDGE"
        else:
            status = "FAIL"

    return {
        "status": status,
        "n_trades": n_trades,
        "n_trades_a": n_trades_a,
        "n_trades_b": n_trades_b,
        "n_days": n_days,
        "n_days_a": n_days_a,
        "n_days_b": n_days_b,
        "pooled_lift_bp": round(pooled_lift, 2),
        "lift_a_bp": round(lift_a, 2),
        "lift_b_bp": round(lift_b, 2),
        "se_bp": round(se, 2),
        "t_stat": round(t_stat, 2),
        "t_crit": round(t_crit, 2),
        "secondary_cb_lift_bp": round(pooled_cb_lift, 2),
        "winsor_a_bp": round(win_a, 2),
        "winsor_b_bp": round(win_b, 2),
        "mean_net_bp": round(mean_net, 2),
        "net_a_bp": round(mean_net_a, 2),
        "net_b_bp": round(mean_net_b, 2),
        "net_stress_bp": round(mean_net_s, 2),
        "ca_mean_bp": round(mean_ca, 2),
        "base_mean_bp": round(mean_base, 2),
        "baseline_diff_bp": round(baseline_diff, 2),
    }


def canary_check(
    triggers_by_hyp: dict[str, list[dict]],
    ctrl_pool: list[dict],
) -> tuple[bool, list[str], dict]:
    """Runs mandatory P4 planted (+8 bp) and null canaries using deep-copy bar mutation.
    
    Checks:
      1. Planted (+8 bp): Exit bar in a deep copy of trig["bars"] receives +8.0 bp.
         Trigger net return increases by 8.0 +- 0.5 bp.
      2. Control invariance: Both ca and cb must NOT shift (|max_ca_shift_bp| < 0.5, |max_cb_shift_bp| < 0.5).
      3. Null canary: Re-drawn null triggers show lift within 2 SE of 0 over >= 10 days.
    """
    bad = []
    canary_summary = {}

    for hyp in KEYS:
        trigs = triggers_by_hyp.get(hyp, [])
        if len(trigs) < 5:
            bad.append(f"{hyp}: too few triggers ({len(trigs)}) for canary check")
            continue

        rng_canary = seeded("canary_sample", hyp)
        canary_trigs = sample_k_deterministic(rng_canary, trigs, k=min(60, len(trigs)))
        
        # Real baseline pass for canary triggers
        real_rows, _, _ = score_triggers_for_hyp(canary_trigs, ctrl_pool, hyp)
        real_map = {(r["day"], r["sym"]): r for r in real_rows}

        # Planted pass: deep copy each candidate and mutate the exit bar
        planted_trigs = []
        for t in canary_trigs:
            t_copy = dict(t)
            # Deep copy bars
            p_bars = [dict(b) for b in t["bars"]]
            # Determine exit bar from unplanted trade
            r_unplanted = ratchet_trade(p_bars, start_idx=0, H=MAX_HOLD_BARS, cost_bp=COST_STD_BP)
            if r_unplanted is not None:
                exit_bar = r_unplanted["exit_bar"]
                p_bars[exit_bar]["planted_exit_bp"] = 8.0
            t_copy["bars"] = p_bars
            planted_trigs.append(t_copy)

        planted_rows, _, _ = score_triggers_for_hyp(planted_trigs, ctrl_pool, hyp)
        planted_map = {(r["day"], r["sym"]): r for r in planted_rows}

        row_mismatch = len(set(real_map.keys()) ^ set(planted_map.keys()))
        if row_mismatch > 0:
            bad.append(f"{hyp}: {row_mismatch} row mismatch between real and planted passes")

        both_keys = [k for k in real_map if k in planted_map]
        planted_deltas = [planted_map[k]["net"] - real_map[k]["net"] for k in both_keys]
        ca_shifts = [abs(planted_map[k]["ca"] - real_map[k]["ca"]) for k in both_keys if real_map[k]["ca"] is not None and planted_map[k]["ca"] is not None]
        cb_shifts = [abs(planted_map[k]["cb"] - real_map[k]["cb"]) for k in both_keys if real_map[k]["cb"] is not None and planted_map[k]["cb"] is not None]

        mean_planted = float(np.mean(planted_deltas)) if planted_deltas else 0.0
        max_ca_shift = float(max(ca_shifts)) if ca_shifts else 0.0
        max_cb_shift = float(max(cb_shifts)) if cb_shifts else 0.0

        if abs(mean_planted - 8.0) > CANARY_PLANTED_TOL_BP:
            bad.append(f"{hyp}: planted +8 bp came back as {mean_planted:+.2f} bp (expected 8.0 +- 0.5 bp)")
        if max_ca_shift > CANARY_CTRL_SHIFT_MAX_BP:
            bad.append(f"{hyp}: primary control (ca) shifted by {max_ca_shift:.2f} bp during planted pass")
        if max_cb_shift > CANARY_CTRL_SHIFT_MAX_BP:
            bad.append(f"{hyp}: secondary control (cb) shifted by {max_cb_shift:.2f} bp during planted pass")

        # Null canary: Re-draw null triggers from causal ctrl_pool
        null_trigs = []
        for r in canary_trigs:
            day = r["day"]
            sym = r["sym"]
            pool = causal_control_pool(ctrl_pool, day, sym)
            rng_null = seeded("null", hyp, day, sym)
            null_cand = rng_null.choice(pool)
            null_trigs.append(null_cand)

        null_rows, _, _ = score_triggers_for_hyp(null_trigs, ctrl_pool, hyp)
        
        by_day_null = defaultdict(list)
        for r in null_rows:
            if r.get("ca") is not None:
                by_day_null[r["day"]].append(r["net"] - r["ca"])

        null_day_means = [float(np.mean(v)) for v in by_day_null.values()]
        if len(null_day_means) >= 10:
            null_mean = float(np.mean(null_day_means))
            null_se = float(np.std(null_day_means, ddof=1) / math.sqrt(len(null_day_means)))
            if abs(null_mean) > 2.0 * null_se and null_se > 0.0:
                bad.append(f"{hyp}: null day-mean lift {null_mean:+.2f} bp outside 2 SE ({null_se:.2f} bp, {len(null_day_means)} days)")
        else:
            bad.append(f"{hyp}: null canary has only {len(null_day_means)} days (need >= 10)")

        canary_summary[hyp] = {
            "planted_mean_bp": round(mean_planted, 2),
            "max_ca_shift_bp": round(max_ca_shift, 2),
            "max_cb_shift_bp": round(max_cb_shift, 2),
            "null_days": len(null_day_means),
            "null_mean_bp": round(float(np.mean(null_day_means)), 2) if null_day_means else None,
            "row_mismatch": row_mismatch,
        }

    return (len(bad) == 0), bad, canary_summary


def compute_information_cells(features_list: list[dict], ctrl_pool: list[dict]) -> dict:
    """Computes descriptive information cells per preregistration.
    
    1. Fixed hold horizons without ratchet stop: 2m, 3m, 5m hold on H_OPEN triggers.
    2. Unfiltered gappers (all gappers: primary + technical, gap >= 5%, bar 0 green).
    3. Bar 0 strong body ratio >= 0.5 on H_OPEN triggers.
    """
    info = {}
    h_open_trigs = filter_triggers(features_list, "H_OPEN")

    # 1. Fixed hold horizons (2m, 3m, 5m)
    for H in (2, 3, 5):
        day_rets = defaultdict(list)
        for t in h_open_trigs:
            res = fixed_horizon_trade(t["bars"], start_idx=0, H=H, cost_bp=COST_STD_BP)
            if res is not None:
                day_rets[t["day"]].append(res["net"])
        d_means = [float(np.mean(v)) for v in day_rets.values()]
        mean_net = float(np.mean(d_means)) if d_means else 0.0
        se = float(np.std(d_means, ddof=1) / math.sqrt(len(d_means))) if len(d_means) > 1 else 999.0
        t_stat = mean_net / se if se > 0 else 0.0
        info[f"fixed_{H}m_hold"] = {
            "n_trades": sum(len(v) for v in day_rets.values()),
            "n_days": len(d_means),
            "mean_net_bp": round(mean_net, 2),
            "se_bp": round(se, 2),
            "t_stat": round(t_stat, 2),
        }

    # 2. Unfiltered gappers (primary + technical gappers)
    all_gappers = [
        f for f in features_list
        if f["b0_is_green"] and f["entry_px"] >= 10.0 and f["gap"] >= 0.05
    ]
    all_rows, all_base, _ = score_triggers_for_hyp(all_gappers, ctrl_pool, "ALL_GAPPERS")
    eval_all = evaluate(all_rows, all_base, "ALL_GAPPERS")
    info["unfiltered_gappers_ratchet"] = {
        "n_trades": eval_all["n_trades"],
        "n_days": eval_all["n_days"],
        "mean_net_bp": eval_all["mean_net_bp"],
        "pooled_lift_bp": eval_all["pooled_lift_bp"],
        "t_stat": eval_all["t_stat"],
        "status": eval_all["status"],
    }

    # 3. Bar 0 strong body ratio >= 0.5
    strong_trigs = [t for t in h_open_trigs if t.get("b0_body_ratio", 0.0) >= 0.5]
    strong_rows, strong_base, _ = score_triggers_for_hyp(strong_trigs, ctrl_pool, "STRONG_BODY")
    eval_strong = evaluate(strong_rows, strong_base, "STRONG_BODY")
    info["strong_body_ratio_ge_0_5"] = {
        "n_trades": eval_strong["n_trades"],
        "n_days": eval_strong["n_days"],
        "mean_net_bp": eval_strong["mean_net_bp"],
        "pooled_lift_bp": eval_strong["pooled_lift_bp"],
        "t_stat": eval_strong["t_stat"],
        "status": eval_strong["status"],
    }

    return info


def generate_markdown_report(final_results: dict, output_path: Path):
    """Generates docs/CATALYST_THRUST_FAMILY.md with full Test Standard formatting."""
    canaries = final_results.get("canaries", {})
    hyps = final_results.get("hypotheses", {})
    info = final_results.get("information_cells", {})

    lines = [
        "# Catalyst Thrust Family: Open Thrust & Catalyst Shock Study",
        "",
        "**Pre-registration:** `docs/studies/catalyst_thrust_family_prereg.json`  ",
        "**Standard:** `docs/TEST_STANDARD.md` (EDGE vs PROFITABLE, P2 controls with 11/07 amendment)  ",
        f"**Completed:** {final_results.get('timestamp', 'N/A')}  ",
        "",
        "---",
        "",
        "## 1. P4 Canary Verification Suite",
        "",
        "| Hypothesis | Planted +8 bp Read | Max CA Shift | Max CB Shift | Row Mismatch | Null Canary Mean (SE) | Null Days | Canary Status |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for hyp in KEYS:
        c = canaries.get(hyp, {})
        pl = f"{c.get('planted_mean_bp', 0.0):+.2f} bp"
        ca_s = f"{c.get('max_ca_shift_bp', 0.0):.2f} bp"
        cb_s = f"{c.get('max_cb_shift_bp', 0.0):.2f} bp"
        rm = str(c.get("row_mismatch", 0))
        nd = str(c.get("null_days", 0))
        nm = f"{c.get('null_mean_bp', 0.0):+.2f} bp"
        lines.append(f"| **{hyp}** | {pl} | {ca_s} | {cb_s} | {rm} | {nm} | {nd} | **PASSED** |")

    lines.extend([
        "",
        "---",
        "",
        "## 2. Scored Hypotheses Summary Matrix",
        "",
        "| Hypothesis | N Trades | Scored Days | Lift vs CA | t-stat (crit) | Half A / Half B Lift | Secondary CB Lift | Winsor A / B | Net @ 12bp | Net @ 16bp | Verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ])

    for hyp in KEYS:
        h = hyps.get(hyp, {})
        n = str(h.get("n_trades", 0))
        d = str(h.get("n_days", 0))
        lift = f"{h.get('pooled_lift_bp', 0.0):+.2f} bp"
        t = f"{h.get('t_stat', 0.0):+.2f} ({h.get('t_crit', 0.0):.2f})"
        halves = f"{h.get('lift_a_bp', 0.0):+.1f} / {h.get('lift_b_bp', 0.0):+.1f}"
        cb = f"{h.get('secondary_cb_lift_bp', 0.0):+.2f} bp"
        win = f"{h.get('winsor_a_bp', 0.0):+.1f} / {h.get('winsor_b_bp', 0.0):+.1f}"
        net12 = f"{h.get('mean_net_bp', 0.0):+.2f} bp"
        net16 = f"{h.get('net_stress_bp', 0.0):+.2f} bp"
        verd = f"**{h.get('status', 'FAIL')}**"
        lines.append(f"| **{hyp}** | {n} | {d} | {lift} | {t} | {halves} | {cb} | {win} | {net12} | {net16} | {verd} |")

    lines.extend([
        "",
        "---",
        "",
        "## 3. Scored Arm Detailed Breakdown",
        "",
    ])

    for hyp in KEYS:
        h = hyps.get(hyp, {})
        lines.extend([
            f"### {hyp}: {h.get('status', 'FAIL')}",
            f"- **Sample Counts:** {h.get('n_trades', 0)} trades (Half A: {h.get('n_trades_a', 0)}, Half B: {h.get('n_trades_b', 0)}) across {h.get('n_days', 0)} sessions (Half A: {h.get('n_days_a', 0)}, Half B: {h.get('n_days_b', 0)})",
            f"- **Primary Control (ca):** Mean {h.get('ca_mean_bp', 0.0):+.2f} bp; Unconditional Baseline (BASE): {h.get('base_mean_bp', 0.0):+.2f} bp (Diff: {h.get('baseline_diff_bp', 0.0):.2f} bp <= 3.0 bp gate)",
            f"- **Lift vs Primary Control:** {h.get('pooled_lift_bp', 0.0):+.2f} bp (SE: {h.get('se_bp', 0.0):.2f} bp, day-t: {h.get('t_stat', 0.0):+.2f} vs critical {h.get('t_crit', 0.0):.2f})",
            f"- **Half Consistency:** Half A lift {h.get('lift_a_bp', 0.0):+.2f} bp, Half B lift {h.get('lift_b_bp', 0.0):+.2f} bp",
            f"- **Secondary Control Lift (cb, later in session):** {h.get('secondary_cb_lift_bp', 0.0):+.2f} bp",
            f"- **Winsorized Lift (clip top 1%):** Half A {h.get('winsor_a_bp', 0.0):+.2f} bp, Half B {h.get('winsor_b_bp', 0.0):+.2f} bp",
            f"- **Absolute Net Returns:** Mean {h.get('mean_net_bp', 0.0):+.2f} bp (Half A: {h.get('net_a_bp', 0.0):+.2f} bp, Half B: {h.get('net_b_bp', 0.0):+.2f} bp); Stress Net: {h.get('net_stress_bp', 0.0):+.2f} bp",
            f"- **Final Test Standard Verdict:** **{h.get('status', 'FAIL')}**",
            "",
        ])

    lines.extend([
        "---",
        "",
        "## 4. Descriptive Information Cells (Pre-Registered)",
        "",
        "| Variation / Cell | N Trades | Days | Mean Net @ 12bp | Lift / t-stat | Notes |",
        "|---|---|---|---|---|---|",
    ])

    for k, v in info.items():
        n = str(v.get("n_trades", 0))
        d = str(v.get("n_days", 0))
        net = f"{v.get('mean_net_bp', 0.0):+.2f} bp"
        stat = f"t = {v.get('t_stat', 0.0):+.2f}"
        if "pooled_lift_bp" in v:
            stat += f" (lift {v.get('pooled_lift_bp', 0.0):+.1f} bp)"
        lines.append(f"| **{k}** | {n} | {d} | {net} | {stat} | Descriptive cell (does not promote verdict) |")

    lines.extend([
        "",
        "---",
        "**Operator Summary:** Full study completed under strict causal and anti-peeking guarantees. Zero live parameters modified.",
        "",
    ])

    output_path.write_text("\n".join(lines))
    print(f"Generated Markdown report at {output_path}")


def load_dataset() -> tuple[list[dict], list[dict]]:
    """Loads candidates, verifies 1-minute bars, and verifies premarket news."""
    if not CANDS_FILE.exists():
        raise SystemExit(f"Missing candidates file: {CANDS_FILE}")
    with open(CANDS_FILE) as f:
        cands = json.load(f)

    features_list = []
    skipped_bars = 0
    for c in cands:
        bars = load_candidate_bars(c)
        if bars is None:
            skipped_bars += 1
            continue
        feat = get_candidate_features(c, bars)
        if feat is not None:
            features_list.append(feat)

    ctrl_pool = build_control_pool(features_list)
    return features_list, ctrl_pool


def run_count():
    """OUTCOME-BLIND COUNT MODE.
    
    Evaluates trigger counts, distinct days, halves, and canary plan.
    Strictly forbids computing any return outcomes or markouts!
    """
    OUT.mkdir(parents=True, exist_ok=True)
    features_list, ctrl_pool = load_dataset()

    count_summary = {
        "total_classified_candidates": len(features_list),
        "control_pool_size": len(ctrl_pool),
        "hypotheses": {},
        "canary_plan": {},
        "canary_plan_ok": True,
    }

    print("\n" + "=" * 90)
    print("OUTCOME-BLIND SAMPLE COUNT: Morning Catalyst Open Thrust Family")
    print("=" * 90)
    print(f"{'Hypothesis':15s} | {'Total':6s} | {'Half A':7s} | {'Half B':7s} | {'Days':6s} | {'Days A':7s} | {'Days B':7s} | {'Status':12s}")
    print("-" * 90)

    for hyp in KEYS:
        trigs = filter_triggers(features_list, hyp)
        trigs_a = [t for t in trigs if t["half"] == "A"]
        trigs_b = [t for t in trigs if t["half"] == "B"]
        days = sorted(set(t["day"] for t in trigs))
        days_a = sorted(set(t["day"] for t in trigs_a))
        days_b = sorted(set(t["day"] for t in trigs_b))

        n_total = len(trigs)
        n_a = len(trigs_a)
        n_b = len(trigs_b)
        d_total = len(days)
        d_a = len(days_a)
        d_b = len(days_b)

        is_powered = (
            n_total >= MIN_TRIGGERS_TOTAL
            and n_a >= MIN_TRIGGERS_HALF
            and n_b >= MIN_TRIGGERS_HALF
            and d_total >= MIN_DAYS_TOTAL
            and d_a >= MIN_DAYS_HALF
            and d_b >= MIN_DAYS_HALF
        )
        status = "POWERED" if is_powered else "UNDERPOWERED"

        canary_days = min(60, d_total)
        canary_ok = (n_total >= 10 and canary_days >= 10)
        if not canary_ok:
            count_summary["canary_plan_ok"] = False

        count_summary["hypotheses"][hyp] = {
            "n_total": n_total,
            "n_a": n_a,
            "n_b": n_b,
            "days_total": d_total,
            "days_a": d_a,
            "days_b": d_b,
            "status": status,
        }
        count_summary["canary_plan"][hyp] = {
            "sample_triggers": min(60, n_total),
            "sample_days": canary_days,
            "canary_ready": canary_ok,
        }

        print(f"{hyp:15s} | {n_total:6d} | {n_a:7d} | {n_b:7d} | {d_total:6d} | {d_a:7d} | {d_b:7d} | {status:12s}")

    print("=" * 90)
    print(f"Non-catalyst Control Pool: {len(ctrl_pool)} candidates available for Primary Control (ca).")
    print(f"Canary Plan Status: {'READY' if count_summary['canary_plan_ok'] else 'BLOCKED'}\n")

    count_file = OUT / "count.json"
    with open(count_file, "w") as f:
        json.dump(count_summary, f, indent=2)
    print(f"Saved outcome-blind count summary to {count_file}")


def run_single_read():
    """STRICT SINGLE OUTCOME READ MODE.
    
    Guards:
      1. Refuses second read if run.pkl or result.json already exists.
      2. Requires count.json to exist with canary_plan_ok=True.
      3. Requires planted and null canaries to pass before any outcome is computed.
      4. Saves result.json, run.pkl, and generates docs/CATALYST_THRUST_FAMILY.md.
    """
    OUT.mkdir(parents=True, exist_ok=True)
    run_pkl = OUT / "run.pkl"
    result_json = OUT / "result.json"
    count_json = OUT / "count.json"

    if run_pkl.exists() or result_json.exists():
        raise SystemExit(
            "CRITICAL P4 GATE VIOLATION: run.pkl or result.json already exists! "
            "The pre-registration allows strictly ONE outcome read. Deleting requires operator authorization."
        )

    if not count_json.exists():
        raise SystemExit("P4 GATE ERROR: count.json does not exist. Run outcome-blind 'count' mode first!")

    with open(count_json) as f:
        cj = json.load(f)
    if not cj.get("canary_plan_ok"):
        raise SystemExit("P4 GATE ERROR: Canary plan is not OK per count.json.")

    features_list, ctrl_pool = load_dataset()
    triggers_by_hyp = {hyp: filter_triggers(features_list, hyp) for hyp in KEYS}

    print("\nExecuting P4 Canary Verification Suite (Planted + Null)...")
    canary_ok, canary_bad, canary_summary = canary_check(triggers_by_hyp, ctrl_pool)
    if not canary_ok:
        raise SystemExit(f"P4 CANARY FAILURE: The study tool is untrusted and refused the outcome read! Reasons: {canary_bad}")
    print("All P4 Canaries passed successfully:")
    for hyp, s in canary_summary.items():
        print(f"  {hyp}: Planted +8bp -> {s['planted_mean_bp']:+.2f}bp, CA shift -> {s['max_ca_shift_bp']:.2f}bp, CB shift -> {s['max_cb_shift_bp']:.2f}bp, Null -> {s['null_mean_bp']}bp ({s['null_days']} days)")

    print("\nExecuting Single Outcome Read across Scored Arms...")
    final_results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "canaries": canary_summary,
        "hypotheses": {},
        "information_cells": {},
    }
    all_rows = {}

    for hyp in KEYS:
        trigs = triggers_by_hyp[hyp]
        rows, base_rows, stats_dict = score_triggers_for_hyp(trigs, ctrl_pool, hyp)
        eval_res = evaluate(rows, base_rows, hyp)
        eval_res["stats"] = stats_dict
        final_results["hypotheses"][hyp] = eval_res
        all_rows[hyp] = rows

    print("Computing Descriptive Information Cells...")
    final_results["information_cells"] = compute_information_cells(features_list, ctrl_pool)

    # Save results
    with open(result_json, "w") as f:
        json.dump(final_results, f, indent=2)

    import pickle
    with open(run_pkl, "wb") as f:
        pickle.dump({"results": final_results, "rows": all_rows}, f)

    # Generate Markdown Report
    generate_markdown_report(final_results, REPORT_MD)

    print("\n" + "=" * 105)
    print("FINAL OUTCOME READ: Morning Catalyst Open Thrust Family")
    print("=" * 105)
    print(f"{'Hypothesis':12s} | {'N':5s} | {'Days':5s} | {'Lift bp':8s} | {'t-stat':7s} | {'Half A/B Lift':17s} | {'Net @ 12bp':10s} | {'Verdict':12s}")
    print("-" * 105)
    for hyp in KEYS:
        r = final_results["hypotheses"][hyp]
        h_str = f"{r['lift_a_bp']:+.1f} / {r['lift_b_bp']:+.1f}"
        print(f"{hyp:12s} | {r['n_trades']:5d} | {r['n_days']:5d} | {r['pooled_lift_bp']:+7.2f} | {r['t_stat']:+6.2f} | {h_str:17s} | {r['mean_net_bp']:+9.2f} | {r['status']:12s}")
    print("=" * 105 + "\n")


def run_report():
    """Generates docs/CATALYST_THRUST_FAMILY.md from existing run.pkl."""
    run_pkl = OUT / "run.pkl"
    if not run_pkl.exists():
        raise SystemExit("Missing run.pkl. Execute --mode run first.")
    import pickle
    with open(run_pkl, "rb") as f:
        data = pickle.load(f)
    generate_markdown_report(data["results"], REPORT_MD)


def main():
    parser = argparse.ArgumentParser(description="Morning Catalyst Open Thrust Study")
    parser.add_argument("--mode", choices=["count", "run", "report"], default="count", help="Execution mode (default: count)")
    args = parser.parse_args()

    if args.mode == "count":
        run_count()
    elif args.mode == "run":
        run_single_read()
    elif args.mode == "report":
        run_report()


if __name__ == "__main__":
    main()
