#!/usr/bin/env python3
"""sim_buystop_pullback.py — Simulation of Buy-Stop Above Pullback High.

Evaluates entering an uptrend stock via a Buy-Stop placed at the high of the
pullback bar (+ $0.01) with a structural stop under the pullback low.

Measures:
  - Fill rate (does the stop trigger, or does it keep falling?)
  - Win rate (hits +1R before stop)
  - Gross & net return (bp)
  - MFE / MAE profile
  - Comparison to buying the climax (empty square)
"""
import sys, os, math, statistics
from collections import defaultdict, Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))

import indicator_confirm_hist as H

def run_day(day: str, syms: list[str] = None):
    try:
        raw_sip = H.load_raw("sip", day)
        raw_iex = H.load_raw("iex", day)
    except Exception as e:
        print(f"Error loading {day}: {e}")
        return []
        
    results = []
    
    # Process symbols (either specific list or all symbols in day)
    target_syms = syms if syms else sorted(raw_sip.keys())
    
    for sym in target_syms:
        if sym not in raw_sip or sym not in raw_iex:
            continue
        sip_rows = raw_sip[sym]
        if len(sip_rows) < 60:
            continue
            
        # Parse into minute map: m -> [open, high, low, close, vol]
        sip = {int(r[0]): [float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in sip_rows}
        mins = sorted(sip.keys())
        
        # Only trade between 09:45 (585) and 15:30 (930)
        start_idx = 0
        while start_idx < len(mins) and mins[start_idx] < 585:
            start_idx += 1
            
        for i in range(max(15, start_idx), len(mins) - 15):
            m = mins[i]
            if m > 930:
                break
                
            cur = sip[m]
            px = cur[3]  # close
            if px < 20.0 or px > 500.0:  # Desk price universe
                continue
                
            # 1. Check Uptrend (Leg 1 proof):
            # Look back 15 bars: did price move up by at least +0.35%?
            past_15 = [sip[mins[k]][3] for k in range(i - 15, i)]
            min_past = min(past_15)
            leg1_high = max(past_15)
            
            # Require leg1 move >= +0.35% from low to high
            if (leg1_high / min_past - 1.0) < 0.0035:
                continue
                
            # 2. Check Pullback:
            # We want price to have pulled back from leg1_high
            # Current close should be below leg1_high by at least 0.10%
            pullback_pct = (leg1_high - px) / leg1_high
            if pullback_pct < 0.0010 or pullback_pct > 0.015:  # gentle pullback (0.10% to 1.5%)
                continue
                
            # Prior candle was red or consolidation
            prev = sip[mins[i-1]]
            if cur[3] > cur[0] and prev[3] > prev[0]:
                # 2 consecutive green bars is not a pullback setup yet
                continue
                
            # Pullback candle high and low
            pb_high = max(cur[1], prev[1])
            pb_low = min(cur[2], prev[2])
            
            # Risk distance
            buy_stop = pb_high + 0.01
            hard_stop = pb_low - 0.01
            risk = buy_stop - hard_stop
            if risk <= 0.02 or risk / buy_stop > 0.015:  # Risk must be reasonable (e.g. 3c to 1.5%)
                continue
                
            # 3. Simulate Buy-Stop order for the next 10 minutes:
            # Does price trigger buy_stop before hard_stop?
            filled = False
            fill_m = None
            fill_px = None
            
            for j in range(i + 1, min(i + 11, len(mins))):
                fut_m = mins[j]
                fut = sip[fut_m]
                
                # Check if price flushes down through hard stop before triggering
                if fut[2] <= hard_stop:
                    # Flush down — buy stop protected us!
                    break
                    
                # Check if price hits buy stop
                if fut[1] >= buy_stop:
                    filled = True
                    fill_m = fut_m
                    fill_px = buy_stop
                    fill_idx = j
                    break
                    
            if not filled:
                # Order expired or protected from a flush
                continue
                
            # 4. Now simulate trade outcome from fill_idx:
            # Target 1 = +1R, Target 2 = Leg 1 High (or +2R)
            t1 = fill_px + risk
            t2 = max(fill_px + 2 * risk, leg1_high)
            
            outcome = None
            exit_px = None
            exit_m = None
            peak_seen = fill_px
            low_seen = fill_px
            
            for k in range(fill_idx, min(fill_idx + 30, len(mins))):  # hold up to 30 mins
                bar = sip[mins[k]]
                peak_seen = max(peak_seen, bar[1])
                low_seen = min(low_seen, bar[2])
                
                # Hit stop loss?
                if bar[2] <= hard_stop:
                    outcome = "stop_loss"
                    exit_px = hard_stop
                    exit_m = mins[k]
                    break
                    
                # Hit T1? (Once T1 is hit, stop moves to breakeven / trail)
                if bar[1] >= t1 and outcome != "t1_hit":
                    outcome = "t1_hit"
                    hard_stop = fill_px  # move stop to breakeven
                    
                # Hit T2 / Leg 1 retest?
                if bar[1] >= t2:
                    outcome = "t2_win"
                    exit_px = t2
                    exit_m = mins[k]
                    break
                    
            if outcome is None or exit_px is None:
                # Time exit at 30 mins
                exit_m = mins[min(fill_idx + 30, len(mins) - 1)]
                exit_px = sip[exit_m][3]
                if outcome is None:
                    outcome = "time_exit"
                elif outcome == "t1_hit":
                    outcome = "t1_time_exit"
                
            mfe_r = (peak_seen - fill_px) / risk
            mae_r = (low_seen - fill_px) / risk
            ret_bp = (exit_px / fill_px - 1.0) * 1e4
            spread_bp = 0.02 / fill_px * 1e4  # ~2c spread
            net_bp = ret_bp - spread_bp
            
            results.append({
                "day": day,
                "sym": sym,
                "entry_time": fill_m,
                "entry_px": fill_px,
                "stop_px": pb_low - 0.01,
                "risk": risk,
                "exit_px": exit_px,
                "outcome": outcome,
                "mfe_r": mfe_r,
                "mae_r": mae_r,
                "gross_bp": ret_bp,
                "net_bp": net_bp,
            })
            
    return results

if __name__ == "__main__":
    days = sys.argv[1:] if len(sys.argv) > 1 else ["2026-10-15", "2026-10-14", "2026-10-13", "2026-10-12", "2026-10-09"]
    all_res = []
    for d in days:
        r = run_day(d)
        all_res.extend(r)
        print(f"Day {d}: {len(r)} trades simulated")
        
    print(f"\n================ TOTAL TRADES: {len(all_res)} ================")
    if not all_res:
        sys.exit(0)
        
    wins = [r for r in all_res if r["gross_bp"] > 0]
    losses = [r for r in all_res if r["gross_bp"] < 0]
    win_rate = len(wins) / len(all_res) * 100
    
    mean_gross = statistics.mean(r["gross_bp"] for r in all_res)
    mean_net = statistics.mean(r["net_bp"] for r in all_res)
    median_gross = statistics.median(r["gross_bp"] for r in all_res)
    
    outcomes = Counter(r["outcome"] for r in all_res)
    mean_mfe = statistics.mean(r["mfe_r"] for r in all_res)
    mean_mae = statistics.mean(r["mae_r"] for r in all_res)
    
    print(f"Win Rate: {win_rate:.1f}% ({len(wins)} wins / {len(losses)} losses / {len(all_res)-len(wins)-len(losses)} flat)")
    print(f"Mean Gross: {mean_gross:+.2f} bp | Mean Net: {mean_net:+.2f} bp | Median Gross: {median_gross:+.2f} bp")
    print(f"Mean MFE: +{mean_mfe:.2f}R | Mean MAE: {mean_mae:.2f}R")
    print(f"Outcomes: {dict(outcomes)}")
