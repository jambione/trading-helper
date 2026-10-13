#!/usr/bin/env python3
"""Empirical study of profit capture thresholds superseding the ratchet stop.
Analyzes:
1. Distribution of peak price run-up (MFE in cents and bp) before rollover across all desk trades.
2. What share of trades hit +$0.10, +$0.15, +$0.20, +$0.25, +$0.30, +$0.40, +$0.50 before stopping out.
3. Simulation: Exiting at fixed profit capture (take profit) vs actual desk exit vs ratchet stop.
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

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not os.path.isdir(os.path.join(ROOT, "ai_reports")):
    ROOT = os.getcwd()
sys.path.insert(0, ROOT)

ET = zoneinfo.ZoneInfo("America/New_York")
CACHE_PATH = os.path.join(ROOT, "ai_reports", "ext_bars_cache.pkl")
OUTCOMES_PATH = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")
COST_RT = 0.0020  # 20 bp standard round-trip cost


def load_trades_and_bars():
    print("Loading outcomes and cached bars on mini...", file=sys.stderr)
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
            trades.append({
                "sym": r["symbol"], "day": d, "et": float(et), "xt": float(xt),
                "ep": float(ep), "xp": float(xp), "qty": float(q),
                "act_usd": (float(xp) - float(ep)) * float(q),
                "act_ret": float(xp) / float(ep) - 1.0,
            })
    return cache, trades


def analyze_mfe_and_targets(cache, trades):
    print("\n=========================================================================================")
    print("PART 1: PEAK PROFIT (MFE) DISTRIBUTION BEFORE ROLLOVER ACROSS DESK TRADES")
    print("=========================================================================================\n")

    mfe_cents_all = []
    mfe_pct_all = []
    trade_profiles = []

    for t in trades:
        frame = cache.get((t["sym"], t["day"]))
        if frame is None or len(frame) < 30:
            continue
        times = [ts.timestamp() for ts in frame.index] if hasattr(frame.index[0], "timestamp") else list(frame["time"])
        m_in = int(t["et"] // 60 * 60)
        m_out = int(t["xt"] // 60 * 60)
        if m_in not in times:
            continue
        idx_in = times.index(m_in)
        idx_out = times.index(m_out) if m_out in times else min(len(times) - 1, idx_in + 45)
        if idx_out <= idx_in:
            idx_out = min(len(times) - 1, idx_in + 1)

        highs = frame["high"].to_numpy(dtype=float)
        lows = frame["low"].to_numpy(dtype=float)
        closes = frame["close"].to_numpy(dtype=float)

        # Max high reached during the life of the trade
        sub_highs = highs[idx_in:idx_out + 1]
        sub_lows = lows[idx_in:idx_out + 1]
        peak_px = np.max(sub_highs)
        trough_px = np.min(sub_lows)

        peak_gain_cents = peak_px - t["ep"]
        peak_gain_pct = (peak_px / t["ep"] - 1.0) * 100.0
        mfe_cents_all.append(peak_gain_cents)
        mfe_pct_all.append(peak_gain_pct)

        trade_profiles.append({
            "t": t, "peak_px": peak_px, "trough_px": trough_px,
            "peak_cents": peak_gain_cents, "peak_pct": peak_gain_pct,
            "sub_highs": sub_highs, "sub_lows": sub_lows, "closes": closes[idx_in:idx_out + 1],
        })

    print(f"Analyzed {len(trade_profiles)} trades with complete minute-bar high/low execution.")
    print(f"  Average Peak Run-up (MFE): +${st.mean(mfe_cents_all):.2f} (+{st.mean(mfe_pct_all):.2f}%)")
    print(f"  Median Peak Run-up (MFE):  +${st.median(mfe_cents_all):.2f} (+{st.median(mfe_pct_all):.2f}%)")
    print(f"  25th Percentile Run-up:    +${np.percentile(mfe_cents_all, 25):.2f} (+{np.percentile(mfe_pct_all, 25):.2f}%)")
    print(f"  75th Percentile Run-up:    +${np.percentile(mfe_cents_all, 75):.2f} (+{np.percentile(mfe_pct_all, 75):.2f}%)")
    print(f"  90th Percentile Run-up:    +${np.percentile(mfe_cents_all, 90):.2f} (+{np.percentile(mfe_pct_all, 90):.2f}%)")

    # Share reaching specific cent levels
    print("\n-----------------------------------------------------------------------------------------")
    print("SHARE OF TRADES REACHING TARGET CENTS BEFORE EXIT:")
    print("-----------------------------------------------------------------------------------------")
    for tgt_cents in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.75, 1.00]:
        reached = [p for p in trade_profiles if p["peak_cents"] >= tgt_cents]
        share = 100.0 * len(reached) / len(trade_profiles)
        print(f"  >= +${tgt_cents:4.2f}/share reached: {len(reached):4d} / {len(trade_profiles)} ({share:5.1f}%)")

    print("\n=========================================================================================")
    print("PART 2: SIMULATION — PROFIT CAPTURE TARGET (SUPERSEDING RATCHET STOP)")
    print("If we sell as soon as price hits Target Cents, vs letting the ratchet trail run:")
    print("Charges standard 20 bp round-trip cost on every trade.")
    print("=========================================================================================\n")

    print(f"{'Target Rule':<30} | {'Win %':<7} | {'Net P&L ($)':<12} | {'Avg bp':<8} | {'Winners':<7} | {'Losers':<7} | {'Avg Win ($)':<11} | {'Avg Loss ($)':<11}")
    print("-" * 105)

    # Actual desk performance
    act_wins = [p["t"]["act_usd"] for p in trade_profiles if p["t"]["act_ret"] > 0]
    act_losses = [p["t"]["act_usd"] for p in trade_profiles if p["t"]["act_ret"] <= 0]
    act_win_pct = 100.0 * len(act_wins) / len(trade_profiles)
    act_tot_u = sum(p["t"]["act_usd"] for p in trade_profiles)
    act_avg_bp = st.mean([p["t"]["act_ret"] for p in trade_profiles]) * 1e4
    print(f"{'Baseline (Actual Desk Trail)':<30} | {act_win_pct:5.1f}% | ${act_tot_u:+10.2f} | {act_avg_bp:+6.1f} | {len(act_wins):<7} | {len(act_losses):<7} | ${st.mean(act_wins) if act_wins else 0:+9.2f} | ${st.mean(act_losses) if act_losses else 0:+9.2f}")

    # Evaluate fixed profit capture targets
    for tgt_cents in [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50]:
        sim_results = []
        for p in trade_profiles:
            t = p["t"]
            ep = t["ep"]
            qty = t["qty"]
            tgt_px = ep + tgt_cents
            
            # Check if target price was reached before actual exit
            if p["peak_cents"] >= tgt_cents:
                # Sold at target price
                xp = tgt_px
                ret = xp / ep - 1.0 - COST_RT
                usd = (xp - ep) * qty - (ep * qty * COST_RT)
                sim_results.append({"ret": ret, "usd": usd, "hit": True})
            else:
                # Target not reached, exited at actual exit price
                xp = t["xp"]
                ret = xp / ep - 1.0 - COST_RT
                usd = (xp - ep) * qty - (ep * qty * COST_RT)
                sim_results.append({"ret": ret, "usd": usd, "hit": False})

        n_tot = len(sim_results)
        wins = [x["usd"] for x in sim_results if x["ret"] > 0]
        losses = [x["usd"] for x in sim_results if x["ret"] <= 0]
        w_pct = 100.0 * len(wins) / n_tot
        tot_u = sum(x["usd"] for x in sim_results)
        avg_bp = st.mean([x["ret"] for x in sim_results]) * 1e4
        avg_w = st.mean(wins) if wins else 0.0
        avg_l = st.mean(losses) if losses else 0.0

        label = f"Capture at +${tgt_cents:.2f}/sh"
        print(f"{label:<30} | {w_pct:5.1f}% | ${tot_u:+10.2f} | {avg_bp:+6.1f} | {len(wins):<7} | {len(losses):<7} | ${avg_w:+9.2f} | ${avg_l:+9.2f}")



def analyze_presquare_targets(cache, trades):
    print("\n=========================================================================================")
    print("PART 3: PROFIT CAPTURE ON DUAL-LINE PRE-SQUARE (▢) SETUPS")
    print("When entering earlier via Dual Pre-Square (both %R >= -35 & tight <= 15):")
    print("=========================================================================================\n")

    from tools.studies.sim_compare_entry_arms import compute_indicators

    profiles = []
    for t in trades:
        frame = cache.get((t["sym"], t["day"]))
        if frame is None or len(frame) < 60:
            continue
        times = [ts.timestamp() for ts in frame.index] if hasattr(frame.index[0], "timestamp") else list(frame["time"])
        m_in = int(t["et"] // 60 * 60)
        m_out = int(t["xt"] // 60 * 60)
        if m_in not in times:
            continue
        idx_0 = times.index(m_in)
        idx_out = times.index(m_out) if m_out in times else min(len(times) - 1, idx_0 + 45)
        if idx_0 < 25 or idx_out <= idx_0:
            continue

        opens = frame["open"].to_numpy(dtype=float)
        highs = frame["high"].to_numpy(dtype=float)
        lows = frame["low"].to_numpy(dtype=float)
        closes = frame["close"].to_numpy(dtype=float)
        fast, slow, vpace, rsi2 = compute_indicators(frame)

        # Pre-square entry
        pre_idx = None
        for k in range(max(20, idx_0 - 15), idx_0 + 1):
            if not np.isnan(fast[k]) and not np.isnan(slow[k]) and not np.isnan(fast[k-1]):
                if fast[k] >= -35 and slow[k] >= -35 and abs(fast[k] - slow[k]) <= 15:
                    if fast[k] > fast[k-1]:
                        if fast[k-1] >= -35 and slow[k-1] >= -35 and abs(fast[k-1] - slow[k-1]) <= 15:
                            pre_idx = k
                            break
        if pre_idx is not None:
            fill_k = min(pre_idx + 1, len(opens) - 1)
            ep = opens[fill_k] * 1.00025
            sub_highs = highs[fill_k:idx_out + 1]
            sub_lows = lows[fill_k:idx_out + 1]
            peak_px = np.max(sub_highs)
            peak_cents = peak_px - ep
            profiles.append({
                "t": t, "ep": ep, "xp_act": t["xp"], "qty": t["qty"],
                "peak_cents": peak_cents, "peak_px": peak_px,
                "highs": sub_highs, "lows": sub_lows, "closes": closes[fill_k:idx_out + 1],
            })

    print(f"Total Pre-Square Setups: {len(profiles)}")
    cents = [p["peak_cents"] for p in profiles]
    print(f"  Median Peak Run-up: +${np.median(cents):.2f}/sh | Mean Peak Run-up: +${np.mean(cents):.2f}/sh")
    for tc in [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]:
        hit_n = sum(p["peak_cents"] >= tc for p in profiles)
        print(f"  >= +${tc:.2f}/sh hit: {hit_n:3d} / {len(profiles)} ({100*hit_n/len(profiles):.1f}%)")

    print("\nProfit Capture Simulation on Pre-Square Setups (selling at target, or double-check stop):")
    print(f"{'Target Rule':<30} | {'Win %':<7} | {'Net P&L ($)':<12} | {'Avg bp':<8} | {'Winners':<7} | {'Losers':<7}")
    print("-" * 85)

    for tc in [0.10, 0.15, 0.20, 0.25, 0.30, 0.40]:
        sims = []
        for p in profiles:
            ep = p["ep"]
            qty = p["qty"]
            tgt_px = ep + tc
            if p["peak_cents"] >= tc:
                xp = tgt_px
                ret = xp / ep - 1.0 - COST_RT
                usd = (xp - ep) * qty - (ep * qty * COST_RT)
                sims.append({"ret": ret, "usd": usd})
            else:
                # did not hit target -> exit at desk exit or -0.15 stop
                xp = p["xp_act"]
                ret = xp / ep - 1.0 - COST_RT
                usd = (xp - ep) * qty - (ep * qty * COST_RT)
                sims.append({"ret": ret, "usd": usd})

        n_s = len(sims)
        wins = [x for x in sims if x["ret"] > 0]
        losses = [x for x in sims if x["ret"] <= 0]
        w_p = 100 * len(wins) / n_s
        tot_u = sum(x["usd"] for x in sims)
        m_bp = np.mean([x["ret"] for x in sims]) * 1e4
        print(f"Capture at +${tc:.2f}/sh{'':<15} | {w_p:5.1f}% | ${tot_u:+10.2f} | {m_bp:+6.1f} | {len(wins):<7} | {len(losses):<7}")


def main():
    cache, trades = load_trades_and_bars()
    analyze_mfe_and_targets(cache, trades)
    analyze_presquare_targets(cache, trades)


if __name__ == "__main__":
    main()

