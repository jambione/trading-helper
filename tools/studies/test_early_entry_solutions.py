#!/usr/bin/env python3
"""Test earlier entry solutions on ext_bars_cache.pkl:
1. Filter out exhausted entries (RSI-2 > 65 at entry)
2. Fast %R (21) turn-up trigger (replaces 112 slow line lag)
3. Volume pace surge (>2x 15m volume pace + breaking 3m high)

Read-only simulation on the mini.
"""
from __future__ import annotations

import collections
import datetime
import json
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
CACHE_PATH = os.path.join(ROOT, "ai_reports", "ext_bars_cache.pkl")
OUTCOMES_PATH = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")
COST_RT = 0.0020  # 20 bp standard round-trip cost


def load_data():
    cache = pickle.load(open(CACHE_PATH, "rb"))
    trades = []
    for line in open(OUTCOMES_PATH):
        try:
            r = json.loads(line)
        except Exception:
            continue
        et, xt = r.get("entry_time"), r.get("exit_time")
        ep, xp = r.get("entry_price"), r.get("exit_price")
        q = r.get("total_qty")
        if et and xt and ep and xp and q and float(q) > 0 and float(et) >= 1789344000:
            d = datetime.datetime.fromtimestamp(float(et), datetime.timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
            f = r.get("features") or {}
            trades.append({
                "sym": r["symbol"], "day": d, "et": float(et), "xt": float(xt),
                "ep": float(ep), "xp": float(xp), "qty": float(q),
                "act_usd": (float(xp) - float(ep)) * float(q),
                "act_ret": float(xp) / float(ep) - 1.0,
                "cm_rsi": float(f.get("cm_rsi")) if f.get("cm_rsi") is not None else None,
                "pctr": float(f.get("pctr")) if f.get("pctr") is not None else None,
                "source": r.get("source"),
            })
    return cache, trades


def test_1_rsi_filter(trades):
    print("\n=======================================================")
    print("TEST 1: REFUSING EXHAUSTED ENTRIES (CM RSI-2 CUTS)")
    print("=======================================================")
    print("What if we refused trades where CM RSI-2 was already overbought?\n")
    
    valid = [t for t in trades if t["cm_rsi"] is not None]
    print(f"Total trades with RSI-2: {len(valid)} / {len(trades)}")
    
    for cap in [50, 60, 65, 70, 75, 80]:
        kept = [t for t in valid if t["cm_rsi"] <= cap]
        blocked = [t for t in valid if t["cm_rsi"] > cap]
        
        k_usd = sum(t["act_usd"] for t in kept)
        k_n = len(kept)
        k_win = 100 * sum(t["act_ret"] > 0 for t in kept) / k_n if k_n else 0
        k_ret = st.mean([t["act_ret"] for t in kept]) * 1e4 if k_n else 0
        
        b_usd = sum(t["act_usd"] for t in blocked)
        b_n = len(blocked)
        b_win = 100 * sum(t["act_ret"] > 0 for t in blocked) / b_n if b_n else 0
        b_ret = st.mean([t["act_ret"] for t in blocked]) * 1e4 if b_n else 0
        
        print(f"RSI <= {cap:2d}: Kept {k_n:3d} (${k_usd:+7.2f}, {k_win:4.1f}% win, {k_ret:+5.1f} bp) | "
              f"Blocked {b_n:3d} (${b_usd:+7.2f}, {b_win:4.1f}% win, {b_ret:+5.1f} bp)")


def test_2_fast_pctr_trigger(cache, trades):
    print("\n=======================================================")
    print("TEST 2: FAST %R (21) TURN-UP TRIGGER (REPLACING 112 SLOW LAG)")
    print("=======================================================")
    print("Trigger when Fast %R (21) turns up from <= -60, without waiting for Slow %R (112) to reach -20.\n")
    
    CFG = {
        "cm_rsi_length": 2,
        "rte_fast_length": 21,
        "rte_fast_ewm_span": 7,
        "rte_slow_native_length": 112,
        "rte_slow_ewm_span": 3,
        "rte_minute_grid": True,
        "rte_slow_timeframe": None,
    }
    
    comparisons = []
    
    for t in trades:
        frame = cache.get((t["sym"], t["day"]))
        if frame is None or len(frame) < 60:
            continue
            
        times = [ts.timestamp() for ts in frame.index] if hasattr(frame.index[0], "timestamp") else list(frame["time"])
        m_in = int(t["et"] // 60 * 60)
        if m_in not in times:
            continue
        idx_0 = times.index(m_in)
        if idx_0 < 15:
            continue
            
        df = signals.compute_percent_r_exhaustion(frame.copy(), CFG)
        fast = df["s_percentR"].to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)
        opens = df["open"].to_numpy(dtype=float)
        
        # Look back up to 10 minutes before actual entry for the first bar where fast %R turned up
        earliest_turn = None
        for k in range(max(1, idx_0 - 10), idx_0 + 1):
            # Fast line turned up: fast[k] > fast[k-1] and fast[k-1] was oversold/dipping (<= -50)
            if fast[k] > fast[k-1] and fast[k-1] <= -50.0:
                earliest_turn = k
                break
                
        if earliest_turn is not None and earliest_turn <= idx_0:
            delta_m = (times[earliest_turn] - times[idx_0]) / 60.0 # negative = earlier!
            entry_early = opens[earliest_turn] * 1.00025 # buy open of that bar + half spread
            # Compare exit: if sold at actual exit price
            exit_px = t["xp"]
            act_ret = t["act_ret"]
            early_ret = exit_px / entry_early - 1.0
            early_usd = (exit_px - entry_early) * t["qty"]
            
            comparisons.append({
                "delta_m": delta_m,
                "ep_early": entry_early,
                "ep_act": t["ep"],
                "act_ret": act_ret,
                "early_ret": early_ret,
                "act_usd": t["act_usd"],
                "early_usd": early_usd,
            })
            
    print(f"Trades with a detectable fast %R turn within 10m before fill: {len(comparisons)} / {len(trades)}")
    earlier = [c for c in comparisons if c["delta_m"] < 0]
    print(f"Fired earlier than desk: {len(earlier)} / {len(comparisons)} ({100*len(earlier)/len(comparisons):.1f}%)")
    if earlier:
        deltas = [c["delta_m"] for c in earlier]
        print(f"  Average minutes earlier: {abs(st.mean(deltas)):.1f} minutes")
        print(f"  Median minutes earlier: {abs(st.median(deltas)):.1f} minutes")
        print(f"  Actual desk P&L on these: ${sum(c['act_usd'] for c in earlier):+.2f} ({100*sum(c['act_ret']>0 for c in earlier)/len(earlier):.1f}% win, {st.mean([c['act_ret'] for c in earlier])*1e4:+.1f} bp)")
        print(f"  Fast %R Early P&L on these: ${sum(c['early_usd'] for c in earlier):+.2f} ({100*sum(c['early_ret']>0 for c in earlier)/len(earlier):.1f}% win, {st.mean([c['early_ret'] for c in earlier])*1e4:+.1f} bp)")


def test_3_volume_surge_trigger(cache, trades):
    print("\n=======================================================")
    print("TEST 3: VOLUME PACE SURGE TRIGGER (>2x 15m PACE + 3m BREAK)")
    print("=======================================================")
    print("Trigger on the first bar where volume >= 2.0x 15m average volume pace AND price > prior 3m high.\n")
    
    comparisons = []
    
    for t in trades:
        frame = cache.get((t["sym"], t["day"]))
        if frame is None or len(frame) < 60:
            continue
            
        times = [ts.timestamp() for ts in frame.index] if hasattr(frame.index[0], "timestamp") else list(frame["time"])
        m_in = int(t["et"] // 60 * 60)
        if m_in not in times:
            continue
        idx_0 = times.index(m_in)
        if idx_0 < 20:
            continue
            
        closes = frame["close"].to_numpy(dtype=float)
        opens = frame["open"].to_numpy(dtype=float)
        vols = frame["volume"].to_numpy(dtype=float)
        highs = frame["high"].to_numpy(dtype=float)
        
        # Scan from idx_0 - 10 to idx_0 for volume surge + breakout
        surge_idx = None
        for k in range(max(15, idx_0 - 10), idx_0 + 1):
            prior_avg_vol = np.mean(vols[k-15:k])
            if prior_avg_vol <= 0:
                continue
            vol_pace = vols[k] / prior_avg_vol
            broke_3m = closes[k] > np.max(highs[k-4:k]) # breaks above prior 3 bars high
            
            if vol_pace >= 2.0 and broke_3m:
                surge_idx = k
                break
                
        if surge_idx is not None and surge_idx <= idx_0:
            delta_m = (times[surge_idx] - times[idx_0]) / 60.0
            # enter on next open
            fill_i = min(surge_idx + 1, len(opens) - 1)
            entry_early = opens[fill_i] * 1.00025
            exit_px = t["xp"]
            act_ret = t["act_ret"]
            early_ret = exit_px / entry_early - 1.0
            early_usd = (exit_px - entry_early) * t["qty"]
            
            comparisons.append({
                "delta_m": delta_m,
                "ep_early": entry_early,
                "ep_act": t["ep"],
                "act_ret": act_ret,
                "early_ret": early_ret,
                "act_usd": t["act_usd"],
                "early_usd": early_usd,
            })
            
    print(f"Trades with a detectable volume surge within 10m before fill: {len(comparisons)} / {len(trades)}")
    earlier = [c for c in comparisons if c["delta_m"] < 0]
    print(f"Fired earlier than desk: {len(earlier)} / {len(comparisons)} ({100*len(earlier)/len(comparisons):.1f}%)")
    if earlier:
        deltas = [c["delta_m"] for c in earlier]
        print(f"  Average minutes earlier: {abs(st.mean(deltas)):.1f} minutes")
        print(f"  Median minutes earlier: {abs(st.median(deltas)):.1f} minutes")
        print(f"  Actual desk P&L on these: ${sum(c['act_usd'] for c in earlier):+.2f} ({100*sum(c['act_ret']>0 for c in earlier)/len(earlier):.1f}% win, {st.mean([c['act_ret'] for c in earlier])*1e4:+.1f} bp)")
        print(f"  Volume Surge Early P&L on these: ${sum(c['early_usd'] for c in earlier):+.2f} ({100*sum(c['early_ret']>0 for c in earlier)/len(earlier):.1f}% win, {st.mean([c['early_ret'] for c in earlier])*1e4:+.1f} bp)")


def main():
    print("Loading cache and outcomes on mini...", file=sys.stderr)
    cache, trades = load_data()
    print(f"Loaded {len(trades)} trades and {len(cache)} cached frames", file=sys.stderr)
    
    test_1_rsi_filter(trades)
    test_2_fast_pctr_trigger(cache, trades)
    test_3_volume_surge_trigger(cache, trades)


if __name__ == "__main__":
    main()
