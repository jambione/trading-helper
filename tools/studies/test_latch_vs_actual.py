#!/usr/bin/env python3
"""Compare Latch / Dip-Buy rule against actual desk fills on real trade days.

Read-only simulation. Uses ext_bars_cache.pkl on the mini.
Charges 0.20% (20 bp) round-trip cost or the measured half-spread at entry/exit.
"""
from __future__ import annotations

import collections
import datetime
import json
import math
import os
import pickle
import statistics as st
import sys
import zoneinfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not os.path.isdir(os.path.join(ROOT, "ai_reports")):
    ROOT = os.getcwd()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import latch_gate_sim as lgs
import signals

ET = zoneinfo.ZoneInfo("America/New_York")
CACHE_PATH = os.path.join(ROOT, "ai_reports", "ext_bars_cache.pkl")
OUTCOMES_PATH = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")
COST_RT = 0.0020  # 20 bp standard cost


def main():
    if not os.path.isfile(CACHE_PATH):
        raise SystemExit(f"Missing {CACHE_PATH}")

    print("Loading ext_bars_cache.pkl...", file=sys.stderr)
    cache = pickle.load(open(CACHE_PATH, "rb"))

    # Load actual trades
    print("Loading outcomes.jsonl...", file=sys.stderr)
    actual_trades = collections.defaultdict(list)
    total_loaded = 0
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
            actual_trades[(r["symbol"], d)].append({
                "sym": r["symbol"], "day": d, "et": float(et), "xt": float(xt),
                "ep": float(ep), "xp": float(xp), "qty": float(q),
                "act_usd": (float(xp) - float(ep)) * float(q),
                "act_ret": float(xp) / float(ep) - 1.0,
                "reason": r.get("close_reason"), "source": r.get("source"),
            })
            total_loaded += 1

    print(f"Loaded {total_loaded} actual trades across {len(actual_trades)} symbol-days", file=sys.stderr)

    # Run latch simulation across the symbol-days that our desk actually traded
    latch_trades_by_symday = collections.defaultdict(list)
    all_latch_trades = []

    print("Running latch walk across symbol-days...", file=sys.stderr)
    scored_symdays = 0
    for (sym, day), act_list in actual_trades.items():
        frame = cache.get((sym, day))
        if frame is None or len(frame) < 120:
            continue
        scored_symdays += 1
        try:
            bars = lgs.bars_from_frame(frame)
            times = lgs._times(frame)
            opens, closes = lgs._opens_closes(frame)
        except Exception as e:
            continue

        raw_trades = lgs.walk(bars)
        for tr in raw_trades:
            if tr["reason"] == "truncated":
                continue
            fill_ts = times[tr["fill_i"]]
            exit_ts = times[tr["exit_i"]]
            t_obj = {
                "sym": sym, "day": day,
                "fill_i": tr["fill_i"], "exit_i": tr["exit_i"],
                "fill_ts": fill_ts, "exit_ts": exit_ts,
                "entry": tr["entry"], "exit": tr["exit"],
                "gross": tr["gross"], "net": tr["gross"] - COST_RT,
                "reason": tr["reason"], "latch_only": tr["latch_only"],
                "hold_min": round((exit_ts - fill_ts) / 60, 1),
            }
            latch_trades_by_symday[(sym, day)].append(t_obj)
            all_latch_trades.append(t_obj)

    print(f"Scored {scored_symdays} symbol-days; produced {len(all_latch_trades)} latch trades", file=sys.stderr)

    # 1. Match latch trades to actual trades:
    # For each actual trade, find if there was a latch entry within [-30m, +10m] of our fill
    matched_comparisons = []
    for (sym, day), acts in actual_trades.items():
        l_trades = latch_trades_by_symday.get((sym, day), [])
        for act in acts:
            # find closest latch trade
            cands = []
            for lt in l_trades:
                delta_m = (lt["fill_ts"] - act["et"]) / 60.0 # positive = latch was later, negative = latch was earlier
                cands.append((abs(delta_m), delta_m, lt))
            if cands:
                cands.sort(key=lambda x: x[0])
                best_abs, delta_m, best_lt = cands[0]
                if best_abs <= 45.0: # within 45 min window
                    matched_comparisons.append({
                        "sym": sym, "day": day,
                        "act_et": act["et"], "latch_et": best_lt["fill_ts"],
                        "delta_min": delta_m, # negative = latch entered earlier!
                        "act_ep": act["ep"], "latch_ep": best_lt["entry"],
                        "price_adv_bp": (act["ep"] / best_lt["entry"] - 1.0) * 1e4, # positive = latch got cheaper price!
                        "act_ret": act["act_ret"],
                        "latch_gross": best_lt["gross"],
                        "latch_net": best_lt["net"],
                        "latch_reason": best_lt["reason"],
                        "latch_hold": best_lt["hold_min"],
                        "act_usd": act["act_usd"],
                        "latch_usd": best_lt["net"] * (act["ep"] * act["qty"]), # scaled to same dollar position
                    })

    print(f"\n=======================================================")
    print(f"LATCH / DIP-BUY STUDY RESULTS (ext_bars_cache.pkl)")
    print(f"=======================================================\n")

    # Overall Latch Trade Performance (all trades produced by latch rule on traded days)
    n_all = len(all_latch_trades)
    if n_all:
        wins_all = [t for t in all_latch_trades if t["net"] > 0]
        net_rets = [t["net"] for t in all_latch_trades]
        gross_rets = [t["gross"] for t in all_latch_trades]
        print(f"### 1. Overall Latch Strategy Standalone Performance (n={n_all}):")
        print(f"  Win Rate (Net): {100 * len(wins_all) / n_all:.1f}%")
        print(f"  Mean Net Return: {st.mean(net_rets)*1e4:+.1f} bp (after 20 bp round-trip cost)")
        print(f"  Median Net Return: {st.median(net_rets)*1e4:+.1f} bp")
        print(f"  Mean Gross Return: {st.mean(gross_rets)*1e4:+.1f} bp")
        print(f"  Median Hold Time: {st.median([t['hold_min'] for t in all_latch_trades]):.1f} min")
        reasons = collections.Counter(t["reason"] for t in all_latch_trades)
        print(f"  Exit Reasons: {dict(reasons)}")
        print()

    # Matched comparison against desk actual fills
    n_match = len(matched_comparisons)
    print(f"### 2. Matched Trades (Same Symbol & Day, within 45m of actual fill, n={n_match}):")
    if n_match:
        deltas = [m["delta_min"] for m in matched_comparisons]
        earlier = [m for m in matched_comparisons if m["delta_min"] < 0]
        same_or_earlier = [m for m in matched_comparisons if m["delta_min"] <= 1.0]
        print(f"  Trades where Latch entered EARLIER than actual: {len(earlier)} / {n_match} ({100*len(earlier)/n_match:.1f}%)")
        print(f"  Median time difference: {st.median(deltas):+.1f} minutes")
        print(f"  Mean time difference: {st.mean(deltas):+.1f} minutes")
        
        # When Latch entered earlier, how much cheaper was the fill?
        if earlier:
            px_adv = [m["price_adv_bp"] for m in earlier]
            print(f"\n  When Latch entered earlier (n={len(earlier)}):")
            print(f"    Median minutes earlier: {abs(st.median([m['delta_min'] for m in earlier])):.1f} minutes earlier")
            print(f"    Average minutes earlier: {abs(st.mean([m['delta_min'] for m in earlier])):.1f} minutes earlier")
            print(f"    Median price discount: {st.median(px_adv):+.1f} bp (cheaper entry)")
            print(f"    Mean price discount: {st.mean(px_adv):+.1f} bp (cheaper entry)")
            
            act_usd_tot = sum(m["act_usd"] for m in earlier)
            latch_usd_tot = sum(m["latch_usd"] for m in earlier)
            print(f"    Actual Desk Total P&L: ${act_usd_tot:+.2f}")
            print(f"    Latch Strategy Total P&L: ${latch_usd_tot:+.2f}")
            print(f"    Actual Win Rate: {100 * sum(1 for m in earlier if m['act_ret'] > 0) / len(earlier):.1f}%")
            print(f"    Latch Win Rate: {100 * sum(1 for m in earlier if m['latch_net'] > 0) / len(earlier):.1f}%")
            print(f"    Actual Mean Ret: {st.mean([m['act_ret'] for m in earlier])*1e4:+.1f} bp")
            print(f"    Latch Mean Net Ret: {st.mean([m['latch_net'] for m in earlier])*1e4:+.1f} bp")

    # Breakdown by timing buckets
    print(f"\n### 3. P&L by Entry Timing vs Actual Desk:")
    print(f"Timing vs Desk | n   | Actual Ret | Latch Net Ret | Actual $ | Latch $")
    print(f"---------------|-----|------------|---------------|----------|--------")
    buckets = [
        (">= 10m earlier", lambda d: d <= -10),
        ("5m - 10m earlier", lambda d: -10 < d <= -5),
        ("1m - 5m earlier", lambda d: -5 < d <= -1),
        ("About same time (±1m)", lambda d: -1 < d <= 1),
        ("Later than desk", lambda d: d > 1),
    ]
    for lab, fn in buckets:
        sub = [m for m in matched_comparisons if fn(m["delta_min"])]
        if sub:
            act_r = st.mean([m["act_ret"] for m in sub]) * 1e4
            lat_r = st.mean([m["latch_net"] for m in sub]) * 1e4
            act_u = sum(m["act_usd"] for m in sub)
            lat_u = sum(m["latch_usd"] for m in sub)
            print(f"{lab:21s} | {len(sub):3d} | {act_r:+9.1f}bp | {lat_r:+12.1f}bp | ${act_u:+7.2f} | ${lat_u:+7.2f}")


if __name__ == "__main__":
    main()
