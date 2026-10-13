#!/usr/bin/env python3
"""Large-scale simulation of Fast %R (21) Early Entry across 47 sessions (Aug 4 - Oct 12).

Part A: All 1,617 historical desk trades in outcomes.jsonl (matched against actual execution).
Part B: Systematic walk across all 2,547 admitted symbol-days in runway_bars_cache.pkl.

Read-only study on the mini. Charges 20 bp standard round-trip cost.
"""
from __future__ import annotations

import collections
import datetime
import math
import os
import pickle
import statistics as st
import sys
import zoneinfo
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not os.path.isdir(os.path.join(ROOT, "ai_reports")):
    ROOT = os.getcwd()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))

import signals

ET = zoneinfo.ZoneInfo("America/New_York")
RUNWAY_PATH = os.path.join(ROOT, "ai_reports", "runway_bars_cache.pkl")
OUTCOMES_PATH = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")
COST_RT = 0.0020  # 20 bp standard round-trip cost

CFG = {
    "cm_rsi_length": 2,
    "rte_fast_length": 21,
    "rte_fast_ewm_span": 7,
    "rte_slow_native_length": 112,
    "rte_slow_ewm_span": 3,
    "rte_minute_grid": True,
    "rte_slow_timeframe": None,
}


def compute_fast_pctr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, length: int = 21, span: float = 7.0) -> np.ndarray:
    """Compute fast %R with EWM smoothing on numpy arrays."""
    n = len(closes)
    out = np.full(n, np.nan)
    raw = np.full(n, np.nan)
    for i in range(length - 1, n):
        hh = np.max(highs[i - length + 1:i + 1])
        ll = np.min(lows[i - length + 1:i + 1])
        rng = hh - ll
        if rng > 0:
            raw[i] = -100.0 * (hh - closes[i]) / rng

    # EWM smooth
    alpha = 2.0 / (span + 1.0)
    val = np.nan
    for i in range(n):
        if not np.isnan(raw[i]):
            if np.isnan(val):
                val = raw[i]
            else:
                val = alpha * raw[i] + (1.0 - alpha) * val
            out[i] = val
    return out


def run_part_a(cache, trades):
    print("\n=======================================================")
    print("PART A: FAST %R (21) EARLY ENTRY ON ALL HISTORICAL TRADES")
    print(f"Dataset: outcomes.jsonl (47 sessions, Aug 4 to Oct 12, n={len(trades)})")
    print("=======================================================\n")

    comparisons = []
    
    for t in trades:
        frame_tup = cache.get((t["sym"], t["day"]))
        if frame_tup is None or len(frame_tup[0]) < 30:
            continue
            
        times = frame_tup[0]
        opens = np.array(frame_tup[1], dtype=float)
        highs = np.array(frame_tup[2], dtype=float)
        lows = np.array(frame_tup[3], dtype=float)
        closes = np.array(frame_tup[4], dtype=float)
        
        m_in = int(t["et"] // 60 * 60)
        # find closest index in times
        idx_cands = [i for i, ts in enumerate(times) if abs(ts - m_in) < 60]
        if not idx_cands:
            continue
        idx_0 = idx_cands[0]
        if idx_0 < 15:
            continue
            
        fast = compute_fast_pctr(highs, lows, closes, length=21, span=7.0)
        
        # Scan back up to 15 minutes before actual entry for fast line turn-up
        earliest_turn = None
        for k in range(max(1, idx_0 - 15), idx_0 + 1):
            if not np.isnan(fast[k]) and not np.isnan(fast[k-1]):
                if fast[k] > fast[k-1] and fast[k-1] <= -50.0:
                    earliest_turn = k
                    break
                    
        if earliest_turn is not None and earliest_turn <= idx_0:
            delta_m = (times[earliest_turn] - times[idx_0]) / 60.0
            fill_k = min(earliest_turn + 1, len(opens) - 1)
            entry_early = opens[fill_k] * 1.00025 # enter next open + half spread
            exit_act = t["xp"]
            act_ret = t["act_ret"]
            early_ret = exit_act / entry_early - 1.0
            act_usd = t["act_usd"]
            early_usd = (exit_act - entry_early) * t["qty"]
            
            # Alternative exit: when fast %R reaches >= -20 (overbought) or session flatten
            exit_ob_k = None
            for j in range(fill_k + 1, len(closes)):
                if times[j] - times[fill_k] > 60 * 60: # 60m max hold
                    exit_ob_k = j
                    break
                if fast[j] >= -20.0:
                    exit_ob_k = j
                    break
            if exit_ob_k is None:
                exit_ob_k = len(closes) - 1
            exit_ob_px = closes[exit_ob_k] * 0.99975
            ob_ret = exit_ob_px / entry_early - 1.0
            ob_usd = (exit_ob_px - entry_early) * t["qty"]
            
            comparisons.append({
                "sym": t["sym"], "day": t["day"],
                "delta_m": delta_m,
                "ep_early": entry_early, "ep_act": t["ep"],
                "act_ret": act_ret, "act_usd": act_usd,
                "early_ret": early_ret, "early_usd": early_usd,
                "ob_ret": ob_ret, "ob_usd": ob_usd,
                "hold_ob_min": round((times[exit_ob_k] - times[fill_k]) / 60, 1),
            })

    n_comp = len(comparisons)
    print(f"Total trades matched: {n_comp} / {len(trades)}")
    earlier = [c for c in comparisons if c["delta_m"] < 0]
    print(f"Fired EARLIER than desk: {len(earlier)} / {n_comp} ({100*len(earlier)/n_comp:.1f}%)")
    
    if earlier:
        deltas = [c["delta_m"] for c in earlier]
        print(f"  Median minutes earlier: {abs(st.median(deltas)):.1f} min")
        print(f"  Average minutes earlier: {abs(st.mean(deltas)):.1f} min\n")
        
        print("--- FULL HISTORY COMPARISON (Aug 4 - Oct 12, n=%d) ---" % len(earlier))
        print(f"  Actual Desk P&L:             ${sum(c['act_usd'] for c in earlier):+8.2f} | Win: {100*sum(c['act_ret']>0 for c in earlier)/len(earlier):5.1f}% | Mean: {st.mean([c['act_ret'] for c in earlier])*1e4:+6.1f} bp | Med: {st.median([c['act_ret'] for c in earlier])*1e4:+6.1f} bp")
        print(f"  Fast %R (Sell at Actual Exit): ${sum(c['early_usd'] for c in earlier):+8.2f} | Win: {100*sum(c['early_ret']>0 for c in earlier)/len(earlier):5.1f}% | Mean: {st.mean([c['early_ret'] for c in earlier])*1e4:+6.1f} bp | Med: {st.median([c['early_ret'] for c in earlier])*1e4:+6.1f} bp")
        print(f"  Fast %R (Sell at %R >= -20):  ${sum(c['ob_usd'] for c in earlier):+8.2f} | Win: {100*sum(c['ob_ret']>0 for c in earlier)/len(earlier):5.1f}% | Mean: {st.mean([c['ob_ret'] for c in earlier])*1e4:+6.1f} bp | Med: {st.median([c['ob_ret'] for c in earlier])*1e4:+6.1f} bp")

        # By Month
        for m_name, start_d, end_d in [("August 2026", "2026-08-01", "2026-08-31"),
                                       ("September 2026", "2026-09-01", "2026-09-30"),
                                       ("October 2026", "2026-10-01", "2026-10-31")]:
            sub = [c for c in earlier if start_d <= c["day"] <= end_d]
            if sub:
                act_u = sum(c["act_usd"] for c in sub)
                ea_u = sum(c["early_usd"] for c in sub)
                ob_u = sum(c["ob_usd"] for c in sub)
                print(f"\n  [{m_name}] (n={len(sub)}):")
                print(f"    Actual Desk:   ${act_u:+8.2f} ({100*sum(c['act_ret']>0 for c in sub)/len(sub):4.1f}% win, {st.mean([c['act_ret'] for c in sub])*1e4:+5.1f} bp)")
                print(f"    Fast %R Early: ${ea_u:+8.2f} ({100*sum(c['early_ret']>0 for c in sub)/len(sub):4.1f}% win, {st.mean([c['early_ret'] for c in sub])*1e4:+5.1f} bp)")
                print(f"    Fast %R at -20:${ob_u:+8.2f} ({100*sum(c['ob_ret']>0 for c in sub)/len(sub):4.1f}% win, {st.mean([c['ob_ret'] for c in sub])*1e4:+5.1f} bp)")

        # Split: First Half vs Second Half (Train vs Test)
        days_all = sorted({c["day"] for c in earlier})
        mid_day = days_all[len(days_all) // 2]
        print(f"\n--- A/B CALENDAR SPLIT (Cut: {mid_day}) ---")
        for half_name, cond in [("First Half (Train)", lambda d: d <= mid_day),
                                ("Second Half (Test)", lambda d: d > mid_day)]:
            sub = [c for c in earlier if cond(c["day"])]
            act_u = sum(c["act_usd"] for c in sub)
            ea_u = sum(c["early_usd"] for c in sub)
            ob_u = sum(c["ob_usd"] for c in sub)
            print(f"  {half_name} (n={len(sub)} trades, {len({c['day'] for c in sub})} days):")
            print(f"    Actual Desk:   ${act_u:+8.2f} ({100*sum(c['act_ret']>0 for c in sub)/len(sub):4.1f}% win, {st.mean([c['act_ret'] for c in sub])*1e4:+5.1f} bp)")
            print(f"    Fast %R Early: ${ea_u:+8.2f} ({100*sum(c['early_ret']>0 for c in sub)/len(sub):4.1f}% win, {st.mean([c['early_ret'] for c in sub])*1e4:+5.1f} bp)")
            print(f"    Fast %R at -20:${ob_u:+8.2f} ({100*sum(c['ob_ret']>0 for c in sub)/len(sub):4.1f}% win, {st.mean([c['ob_ret'] for c in sub])*1e4:+5.1f} bp)")


def run_part_b(cache):
    print("\n=======================================================")
    print("PART B: SYSTEMATIC FAST %R (21) SWING ACROSS ALL ADMITTED NAMES")
    print(f"Dataset: runway_bars_cache.pkl (2,547 symbol-days, independent walk)")
    print("Rule: Buy open after Fast %R pivots up from <= -50. Sell at %R >= -20 or 15:50.")
    print("Cost: 20 bp round-trip deduction charged on EVERY trade.")
    print("=======================================================\n")

    all_trades = []
    days_set = sorted({d for s, d in cache.keys() if cache.get((s, d)) is not None})
    
    for (sym, day), tup in cache.items():
        if tup is None or len(tup[0]) < 60:
            continue
        times = tup[0]
        opens = np.array(tup[1], dtype=float)
        highs = np.array(tup[2], dtype=float)
        lows = np.array(tup[3], dtype=float)
        closes = np.array(tup[4], dtype=float)
        
        fast = compute_fast_pctr(highs, lows, closes, length=21, span=7.0)
        
        pos = None
        for i in range(21, len(closes)):
            t_min = datetime.datetime.fromtimestamp(times[i], datetime.timezone.utc).astimezone(ET)
            minute_et = t_min.hour * 60 + t_min.minute
            
            # EOD Flatten
            if pos is not None and minute_et >= 15 * 60 + 50:
                exit_px = closes[i] * 0.99975
                ret = exit_px / pos["entry"] - 1.0 - COST_RT
                all_trades.append({
                    "sym": sym, "day": day, "entry": pos["entry"], "exit": exit_px,
                    "ret": ret, "reason": "eod", "hold_min": i - pos["i"],
                })
                pos = None
                continue
                
            # Exit at %R >= -20
            if pos is not None:
                if fast[i] >= -20.0 or minute_et >= 15 * 60 + 50:
                    exit_px = opens[i] * 0.99975
                    ret = exit_px / pos["entry"] - 1.0 - COST_RT
                    all_trades.append({
                        "sym": sym, "day": day, "entry": pos["entry"], "exit": exit_px,
                        "ret": ret, "reason": "pctr20", "hold_min": i - pos["i"],
                    })
                    pos = None
                    continue
                    
            # Entry: 09:40 to 15:30 ET
            if pos is None and 9 * 60 + 40 <= minute_et <= 15 * 60 + 30:
                # Fast %R turned up from <= -50
                if fast[i] > fast[i-1] and fast[i-1] <= -50.0:
                    nxt = i + 1
                    if nxt < len(opens):
                        pos = {
                            "i": nxt,
                            "entry": opens[nxt] * 1.00025,
                        }

    n_tot = len(all_trades)
    print(f"Total systematic trades generated: {n_tot} across {len(days_set)} days")
    if n_tot:
        rets = [t["ret"] for t in all_trades]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        
        # Day-equal stats
        by_day = collections.defaultdict(list)
        for t in all_trades:
            by_day[t["day"]].append(t["ret"])
        day_means = [st.mean(v) for v in by_day.values()]
        mu_day = st.mean(day_means)
        se_day = st.stdev(day_means) / math.sqrt(len(day_means)) if len(day_means) > 1 else 1.0
        t_stat = mu_day / se_day if se_day > 0 else 0.0
        
        print(f"  Win Rate (Net of 20 bp): {100 * len(wins) / n_tot:.1f}%")
        print(f"  Mean Net Return per trade: {st.mean(rets)*1e4:+.1f} bp")
        print(f"  Median Net Return: {st.median(rets)*1e4:+.1f} bp")
        print(f"  Day-Equal Mean Return: {mu_day*1e4:+.1f} bp")
        print(f"  Day-Clustered t-statistic: {t_stat:+.2f} ({len(day_means)} trading days)")
        print(f"  Average Winner: {st.mean(wins)*1e4:+.1f} bp | Average Loser: {st.mean(losses)*1e4:+.1f} bp")
        print(f"  Median Hold Time: {st.median([t['hold_min'] for t in all_trades]):.1f} minutes")
        print(f"  Exit Reasons: {dict(collections.Counter(t['reason'] for t in all_trades))}")


def main():
    print("Loading runway_bars_cache.pkl and outcomes.jsonl on mini...", file=sys.stderr)
    cache = pickle.load(open(RUNWAY_PATH, "rb"))
    trades = []
    for line in open(OUTCOMES_PATH):
        try:
            r = json.loads(line)
        except Exception:
            continue
        et = r.get("entry_time") or r.get("ts")
        xt = r.get("exit_time") or r.get("ts")
        ep = r.get("entry_price")
        xp = r.get("exit_price") or r.get("exit_price_approx")
        q = r.get("total_qty")
        if et and ep and xp and float(ep) > 0 and float(xp) > 0:
            qty = float(q) if q and float(q) > 0 else 1000.0 / float(ep)
            d = datetime.datetime.fromtimestamp(float(et), datetime.timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
            trades.append({
                "sym": r["symbol"], "day": d, "et": float(et), "xt": float(xt),
                "ep": float(ep), "xp": float(xp), "qty": qty,
                "act_usd": (float(xp) - float(ep)) * qty,
                "act_ret": float(xp) / float(ep) - 1.0,
            })
            
    print(f"Loaded {len(cache)} cached symbol-days and {len(trades)} trades", file=sys.stderr)
    
    run_part_a(cache, trades)
    run_part_b(cache)


if __name__ == "__main__":
    main()
