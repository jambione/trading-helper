#!/usr/bin/env python3
"""Head-to-head simulation of entry triggers on ext_bars_cache.pkl and outcomes.jsonl:
1. Baseline (Actual Desk Fills)
2. Single-Line Mid-Rise (Fast %R crossing -50)
3. Dual-Line Full Square (■) (Fast >= -20, Slow >= -20, Gap <= 15, streak >= 2)
4. Dual-Line Pre-Square (▢) (Fast >= -35, Slow >= -35, Gap <= 15, streak >= 2)
5. Volume Pace Surge (Vol >= 2.0x 15m avg + breaking 3m high)
6. Dual Pre-Square + Volume Pace (Pre-Square + Vol >= 1.25x)
7. CM RSI-2 Oversold Reversal (RSI-2 <= 35 turning up + fast %R rising)

Read-only simulation.
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
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not os.path.isdir(os.path.join(ROOT, "ai_reports")):
    ROOT = os.getcwd()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))

ET = zoneinfo.ZoneInfo("America/New_York")
CACHE_PATH = os.path.join(ROOT, "ai_reports", "ext_bars_cache.pkl")
OUTCOMES_PATH = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")
COST_RT = 0.0020  # 20 bp standard round-trip cost


def compute_indicators(df: pd.DataFrame):
    """Compute fast %R, slow %R, volume pace, and RSI-2 on 1m bars."""
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    volumes = df["volume"].to_numpy(dtype=float)
    n = len(closes)

    # 1. Fast %R (21, EMA 7)
    fast_raw = np.full(n, np.nan)
    for i in range(20, n):
        hh = np.max(highs[i - 20:i + 1])
        ll = np.min(lows[i - 20:i + 1])
        if hh > ll:
            fast_raw[i] = -100.0 * (hh - closes[i]) / (hh - ll)
    alpha_f = 2.0 / (7.0 + 1.0)
    fast_ewm = np.full(n, np.nan)
    val = np.nan
    for i in range(n):
        if not np.isnan(fast_raw[i]):
            val = fast_raw[i] if np.isnan(val) else alpha_f * fast_raw[i] + (1.0 - alpha_f) * val
            fast_ewm[i] = val

    # 2. Slow %R (112, EMA 3)
    slow_raw = np.full(n, np.nan)
    for i in range(111, n):
        hh = np.max(highs[i - 111:i + 1])
        ll = np.min(lows[i - 111:i + 1])
        if hh > ll:
            slow_raw[i] = -100.0 * (hh - closes[i]) / (hh - ll)
    alpha_s = 2.0 / (3.0 + 1.0)
    slow_ewm = np.full(n, np.nan)
    val_s = np.nan
    for i in range(n):
        if not np.isnan(slow_raw[i]):
            val_s = slow_raw[i] if np.isnan(val_s) else alpha_s * slow_raw[i] + (1.0 - alpha_s) * val_s
            slow_ewm[i] = val_s

    # 3. Volume Pace (current vol / 15-bar rolling mean vol)
    vol_pace = np.full(n, 1.0)
    for i in range(15, n):
        m_vol = np.mean(volumes[i - 15:i])
        if m_vol > 0:
            vol_pace[i] = volumes[i] / m_vol

    # 4. Connors RSI-2
    rsi2 = np.full(n, np.nan)
    diffs = np.diff(closes)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)
    if len(diffs) >= 2:
        ag = np.mean(gains[:2])
        al = np.mean(losses[:2])
        for i in range(2, len(diffs)):
            ag = (ag * 1.0 + gains[i]) / 2.0
            al = (al * 1.0 + losses[i]) / 2.0
            if al == 0:
                rsi2[i + 1] = 100.0
            else:
                rs = ag / al
                rsi2[i + 1] = 100.0 - (100.0 / (1.0 + rs))

    return fast_ewm, slow_ewm, vol_pace, rsi2


def load_dataset():
    print("Loading cache and outcomes on mini...", file=sys.stderr)
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
    print(f"Loaded {len(cache)} cached frames and {len(trades)} trades", file=sys.stderr)
    return cache, trades


def simulate_on_historical_trades(cache, trades):
    """Part 1: How would each trigger have performed on the actual setups the desk traded?"""
    print("\n=========================================================================================")
    print("PART 1: TRIGGER COMPARISON ON DESK SETUPS (outcomes.jsonl, n=995)")
    print("How each trigger behaves when the desk nominated and traded the setup:")
    print("=========================================================================================\n")

    results = {
        "Baseline (Actual Desk)": {"fired": len(trades), "win_pct": 100 * sum(t["act_ret"] > 0 for t in trades) / len(trades),
                                   "tot_usd": sum(t["act_usd"] for t in trades), "mean_bp": st.mean([t["act_ret"] for t in trades]) * 1e4,
                                   "delta_m": 0.0},
        "Dual-Line Full Square (■)": [],
        "Dual-Line Pre-Square (▢)": [],
        "Volume Pace Surge (>=2x + 3m break)": [],
        "Dual Pre-Square + Vol Pace (>=1.25x)": [],
        "CM RSI-2 Reversal (<=35 turn)": [],
    }

    for t in trades:
        frame = cache.get((t["sym"], t["day"]))
        if frame is None or len(frame) < 60:
            continue
        times = [ts.timestamp() for ts in frame.index] if hasattr(frame.index[0], "timestamp") else list(frame["time"])
        m_in = int(t["et"] // 60 * 60)
        if m_in not in times:
            continue
        idx_0 = times.index(m_in)
        if idx_0 < 30:
            continue

        opens = frame["open"].to_numpy(dtype=float)
        highs = frame["high"].to_numpy(dtype=float)
        closes = frame["close"].to_numpy(dtype=float)
        fast, slow, vpace, rsi2 = compute_indicators(frame)

        # Lookback window: up to 15m before desk fill
        lb_start = max(25, idx_0 - 15)

        # 1. Dual-Line Full Square (■): fast >= -20, slow >= -20, gap <= 15, streak >= 2, fast rising
        sq_idx = None
        for k in range(lb_start, idx_0 + 1):
            if not np.isnan(fast[k]) and not np.isnan(slow[k]) and not np.isnan(fast[k-1]):
                if fast[k] >= -20 and slow[k] >= -20 and abs(fast[k] - slow[k]) <= 15:
                    if fast[k] > fast[k-1]: # rising
                        if fast[k-1] >= -20 and slow[k-1] >= -20 and abs(fast[k-1] - slow[k-1]) <= 15: # streak 2
                            sq_idx = k
                            break
        if sq_idx is not None:
            fill_k = min(sq_idx + 1, len(opens) - 1)
            ep = opens[fill_k] * 1.00025
            results["Dual-Line Full Square (■)"].append({
                "ret": t["xp"] / ep - 1.0,
                "usd": (t["xp"] - ep) * t["qty"],
                "delta_m": (times[sq_idx] - times[idx_0]) / 60.0,
            })

        # 2. Dual-Line Pre-Square (▢): fast >= -35, slow >= -35, gap <= 15, streak >= 2, fast rising
        pre_idx = None
        for k in range(lb_start, idx_0 + 1):
            if not np.isnan(fast[k]) and not np.isnan(slow[k]) and not np.isnan(fast[k-1]):
                if fast[k] >= -35 and slow[k] >= -35 and abs(fast[k] - slow[k]) <= 15:
                    if fast[k] > fast[k-1]:
                        if fast[k-1] >= -35 and slow[k-1] >= -35 and abs(fast[k-1] - slow[k-1]) <= 15:
                            pre_idx = k
                            break
        if pre_idx is not None:
            fill_k = min(pre_idx + 1, len(opens) - 1)
            ep = opens[fill_k] * 1.00025
            results["Dual-Line Pre-Square (▢)"].append({
                "ret": t["xp"] / ep - 1.0,
                "usd": (t["xp"] - ep) * t["qty"],
                "delta_m": (times[pre_idx] - times[idx_0]) / 60.0,
            })

        # 3. Volume Pace Surge: vpace >= 2.0 and close > max(highs[k-3:k])
        vol_idx = None
        for k in range(lb_start, idx_0 + 1):
            if vpace[k] >= 2.0 and closes[k] > np.max(highs[max(0, k-3):k]):
                vol_idx = k
                break
        if vol_idx is not None:
            fill_k = min(vol_idx + 1, len(opens) - 1)
            ep = opens[fill_k] * 1.00025
            results["Volume Pace Surge (>=2x + 3m break)"].append({
                "ret": t["xp"] / ep - 1.0,
                "usd": (t["xp"] - ep) * t["qty"],
                "delta_m": (times[vol_idx] - times[idx_0]) / 60.0,
            })

        # 4. Dual Pre-Square + Vol Pace (>= 1.25x)
        prev_idx = None
        for k in range(lb_start, idx_0 + 1):
            if not np.isnan(fast[k]) and not np.isnan(slow[k]) and not np.isnan(fast[k-1]):
                if fast[k] >= -35 and slow[k] >= -35 and abs(fast[k] - slow[k]) <= 15:
                    if fast[k] > fast[k-1] and vpace[k] >= 1.25:
                        if fast[k-1] >= -35 and slow[k-1] >= -35 and abs(fast[k-1] - slow[k-1]) <= 15:
                            prev_idx = k
                            break
        if prev_idx is not None:
            fill_k = min(prev_idx + 1, len(opens) - 1)
            ep = opens[fill_k] * 1.00025
            results["Dual Pre-Square + Vol Pace (>=1.25x)"].append({
                "ret": t["xp"] / ep - 1.0,
                "usd": (t["xp"] - ep) * t["qty"],
                "delta_m": (times[prev_idx] - times[idx_0]) / 60.0,
            })

        # 5. CM RSI-2 Reversal: rsi2 <= 35 and turns up + fast rising
        rsi_idx = None
        for k in range(lb_start, idx_0 + 1):
            if not np.isnan(rsi2[k]) and not np.isnan(rsi2[k-1]) and not np.isnan(fast[k]):
                if rsi2[k-1] <= 35.0 and rsi2[k] > rsi2[k-1] and fast[k] > fast[k-1]:
                    rsi_idx = k
                    break
        if rsi_idx is not None:
            fill_k = min(rsi_idx + 1, len(opens) - 1)
            ep = opens[fill_k] * 1.00025
            results["CM RSI-2 Reversal (<=35 turn)"].append({
                "ret": t["xp"] / ep - 1.0,
                "usd": (t["xp"] - ep) * t["qty"],
                "delta_m": (times[rsi_idx] - times[idx_0]) / 60.0,
            })

    print(f"{'Trigger Option':<38} | {'Fired':<7} | {'Win %':<7} | {'Net P&L ($)':<12} | {'Mean bp':<9} | {'Timing Lead':<12}")
    print("-" * 97)
    base = results["Baseline (Actual Desk)"]
    print(f"{'Baseline (Actual Desk)':<38} | {base['fired']:<7} | {base['win_pct']:5.1f}% | ${base['tot_usd']:+10.2f} | {base['mean_bp']:+7.1f} | 0.0 min")

    for name in ["Dual-Line Full Square (■)", "Dual-Line Pre-Square (▢)", "Volume Pace Surge (>=2x + 3m break)",
                 "Dual Pre-Square + Vol Pace (>=1.25x)", "CM RSI-2 Reversal (<=35 turn)"]:
        items = results[name]
        n_it = len(items)
        if n_it == 0:
            continue
        win = 100 * sum(x["ret"] > 0 for x in items) / n_it
        tot_u = sum(x["usd"] for x in items)
        m_bp = st.mean([x["ret"] for x in items]) * 1e4
        lead = abs(st.mean([x["delta_m"] for x in items]))
        print(f"{name:<38} | {n_it:<7} | {win:5.1f}% | ${tot_u:+10.2f} | {m_bp:+7.1f} | {lead:4.1f} min earlier")


def simulate_full_systematic_walk(cache):
    """Part 2: Full Systematic Walk on all 2,389 frames with standard exits and 20 bp friction."""
    print("\n=========================================================================================")
    print("PART 2: SYSTEMATIC WALK ACROSS ALL 2,389 ADMITTED FRAMES (Independent Test)")
    print("Exit: Local trail (0.25% peak ratchet) + 3m double check (-15 bp fail) + 45m max hold.")
    print("Charges 20 bp round-trip cost on EVERY trade.")
    print("=========================================================================================\n")

    triggers = [
        ("1. Single-Line Mid-Rise (Current)", "mid_rise"),
        ("2. Dual-Line Full Square (■)", "square"),
        ("3. Dual-Line Pre-Square (▢)", "presquare"),
        ("4. Volume Pace Surge (>=2x + 3m break)", "vol_surge"),
        ("5. Dual Pre-Square + Vol Pace (>=1.25x)", "presquare_vol"),
        ("6. CM RSI-2 Reversal (<=35 turn)", "rsi2_turn"),
    ]

    summary = []

    for label, trig_type in triggers:
        trades = []
        for (sym, day), frame in cache.items():
            if frame is None or len(frame) < 60:
                continue
            times = [ts.timestamp() for ts in frame.index] if hasattr(frame.index[0], "timestamp") else list(frame["time"])
            opens = frame["open"].to_numpy(dtype=float)
            highs = frame["high"].to_numpy(dtype=float)
            lows = frame["low"].to_numpy(dtype=float)
            closes = frame["close"].to_numpy(dtype=float)
            fast, slow, vpace, rsi2 = compute_indicators(frame)

            pos = None
            for i in range(25, len(closes)):
                t_min = datetime.datetime.fromtimestamp(times[i], datetime.timezone.utc).astimezone(ET)
                m_et = t_min.hour * 60 + t_min.minute

                # Active position management
                if pos is not None:
                    bars_held = i - pos["entry_idx"]
                    cur_px = closes[i]
                    peak_px = max(pos["peak"], highs[i])
                    pos["peak"] = peak_px

                    # 3m Double Checker: if 3 min elapsed, price < entry, and fast %R falling
                    if bars_held >= 3 and cur_px < pos["entry_px"] and fast[i] < fast[i-1]:
                        xp = cur_px * 0.99975
                        ret = xp / pos["entry_px"] - 1.0 - COST_RT
                        trades.append({"ret": ret, "usd": (xp - pos["entry_px"]) * 50, "hold": bars_held, "hour": t_min.hour})
                        pos = None
                        continue

                    # Local trail: ratchets peak give 0.25% if MFE >= 0.15%
                    mfe = (peak_px - pos["entry_px"]) / pos["entry_px"]
                    if mfe >= 0.0015:
                        stop_px = peak_px * (1.0 - 0.0025)
                        if lows[i] <= stop_px:
                            xp = stop_px
                            ret = xp / pos["entry_px"] - 1.0 - COST_RT
                            trades.append({"ret": ret, "usd": (xp - pos["entry_px"]) * 50, "hold": bars_held, "hour": t_min.hour})
                            pos = None
                            continue

                    # Hard max hold 45 min or 15:50 EOD
                    if bars_held >= 45 or m_et >= 15 * 60 + 50:
                        xp = closes[i] * 0.99975
                        ret = xp / pos["entry_px"] - 1.0 - COST_RT
                        trades.append({"ret": ret, "usd": (xp - pos["entry_px"]) * 50, "hold": bars_held, "hour": t_min.hour})
                        pos = None
                        continue

                # Entry signals: 09:40 to 15:15 ET
                if pos is None and 9 * 60 + 40 <= m_et <= 15 * 60 + 15:
                    arm = False
                    if trig_type == "mid_rise":
                        # Fast crosses above -50
                        if fast[i-1] <= -50.0 and fast[i] > -50.0:
                            arm = True
                    elif trig_type == "square":
                        # Dual OB >= -20, tight <= 15, streak >= 2, rising
                        if fast[i] >= -20 and slow[i] >= -20 and abs(fast[i] - slow[i]) <= 15 and fast[i] > fast[i-1]:
                            if fast[i-1] >= -20 and slow[i-1] >= -20 and abs(fast[i-1] - slow[i-1]) <= 15:
                                arm = True
                    elif trig_type == "presquare":
                        # Dual approach >= -35, tight <= 15, streak >= 2, rising
                        if fast[i] >= -35 and slow[i] >= -35 and abs(fast[i] - slow[i]) <= 15 and fast[i] > fast[i-1]:
                            if fast[i-1] >= -35 and slow[i-1] >= -35 and abs(fast[i-1] - slow[i-1]) <= 15:
                                arm = True
                    elif trig_type == "vol_surge":
                        # Vol >= 2.0x 15m avg + breaking 3m high
                        if vpace[i] >= 2.0 and closes[i] > np.max(highs[max(0, i-3):i]):
                            arm = True
                    elif trig_type == "presquare_vol":
                        # Pre-square + Vol >= 1.25x
                        if fast[i] >= -35 and slow[i] >= -35 and abs(fast[i] - slow[i]) <= 15 and fast[i] > fast[i-1] and vpace[i] >= 1.25:
                            if fast[i-1] >= -35 and slow[i-1] >= -35 and abs(fast[i-1] - slow[i-1]) <= 15:
                                arm = True
                    elif trig_type == "rsi2_turn":
                        # Connors RSI-2 <= 35 turn up + fast %R rising
                        if not np.isnan(rsi2[i]) and not np.isnan(rsi2[i-1]):
                            if rsi2[i-1] <= 35.0 and rsi2[i] > rsi2[i-1] and fast[i] > fast[i-1]:
                                arm = True

                    if arm and i + 1 < len(opens):
                        pos = {
                            "entry_idx": i + 1,
                            "entry_px": opens[i + 1] * 1.00025,
                            "peak": opens[i + 1],
                        }

        n_t = len(trades)
        if n_t:
            rets = [t["ret"] for t in trades]
            wins = [r for r in rets if r > 0]
            win_pct = 100 * len(wins) / n_t
            tot_usd = sum(t["usd"] for t in trades)
            m_bp = st.mean(rets) * 1e4
            med_bp = st.median(rets) * 1e4
            m_win = st.mean([r for r in rets if r > 0]) * 1e4 if wins else 0
            m_loss = st.mean([r for r in rets if r <= 0]) * 1e4 if len(wins) < n_t else 0
            # midday win % (11:30 - 13:59 ET)
            midday = [t for t in trades if 11 <= t["hour"] <= 13]
            mid_win = 100 * sum(t["ret"] > 0 for t in midday) / len(midday) if midday else 0
            summary.append({
                "label": label, "n": n_t, "win_pct": win_pct, "tot_usd": tot_usd,
                "mean_bp": m_bp, "med_bp": med_bp, "win_loss_ratio": abs(m_win / m_loss) if m_loss != 0 else 0,
                "midday_n": len(midday), "midday_win": mid_win,
            })

    print(f"{'Trigger Strategy':<42} | {'Trades':<6} | {'Win %':<7} | {'Total $ (Net)':<13} | {'Mean bp':<8} | {'Midday Win%':<11}")
    print("-" * 98)
    for s in summary:
        print(f"{s['label']:<42} | {s['n']:<6} | {s['win_pct']:5.1f}% | ${s['tot_usd']:+11.2f} | {s['mean_bp']:+6.1f} | {s['midday_win']:5.1f}% ({s['midday_n']} t)")


def main():
    cache, trades = load_dataset()
    simulate_on_historical_trades(cache, trades)
    simulate_full_systematic_walk(cache)


if __name__ == "__main__":
    main()
