#!/usr/bin/env python3
"""Post-Exit Markout Study: Tests Grok's challenge on actual desk fills.

Measures the price trajectory of real positions after the 3-minute ratchet exits:
- Exit to +5 minutes
- Exit to +15 minutes
- Exit to +30 minutes
- Exit to EOD (15:50 ET)
- Max Favorable Excursion (MFE) and Max Adverse Excursion (MAE) over the 30m window.

Answers the definitive question:
Did the stocks continue running after exit (validating the Runner hypothesis),
or did they fade/drift (validating Grok's unselected right-tail finding)?
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ET = ZoneInfo("America/New_York")
M0 = 570  # 09:30 ET

ROOT = Path(__file__).resolve().parent
if not (ROOT / "ai_reports").exists():
    ROOT = Path.cwd()

WH_SIP = ROOT / "ai_reports" / "warehouse" / "sip"
OUTCOMES_FILE = ROOT / "ai_reports" / "outcomes.jsonl"


def load_day(path: Path) -> dict:
    z = np.load(path, allow_pickle=True)
    syms = [str(s) for s in z["syms"]]
    n = len(syms)
    shape = (n, 390)
    mats = {k: np.full(shape, np.nan, dtype=np.float32) for k in ("o", "h", "l", "c", "v")}
    si = z["sym_idx"].astype(np.int64)
    mi = z["minute"].astype(np.int64) - M0
    ok = (mi >= 0) & (mi < 390)
    for k in mats:
        mats[k][si[ok], mi[ok]] = z[k][ok]
    return {"syms": syms, "idx": {s: i for i, s in enumerate(syms)}, **mats}


def main():
    if not OUTCOMES_FILE.exists():
        print(f"Error: {OUTCOMES_FILE} not found")
        return 1

    trades_by_date = defaultdict(list)
    with open(OUTCOMES_FILE) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            exit_ts = rec.get("exit_time") or rec.get("ts")
            entry_ts = rec.get("entry_time")
            exit_px = rec.get("exit_price") or rec.get("exit_price_approx")
            sym = rec.get("symbol")
            if not (exit_ts and exit_px and sym):
                continue
            dt = datetime.fromtimestamp(float(exit_ts), tz=ET)
            d_str = dt.strftime("%Y-%m-%d")
            trades_by_date[d_str].append({
                "symbol": str(sym).upper(),
                "entry_px": float(rec.get("entry_price") or exit_px),
                "exit_px": float(exit_px),
                "exit_ts": float(exit_ts),
                "entry_ts": float(entry_ts) if entry_ts else float(exit_ts),
                "dt": dt,
                "hold_sec": float(rec.get("hold_sec") or 0.0),
                "reason": str(rec.get("close_reason") or rec.get("closing_reason") or ""),
            })

    print(f"Total trade records loaded: {sum(len(v) for v in trades_by_date.values())} across {len(trades_by_date)} days")

    available_days = sorted([p.stem for p in WH_SIP.glob("20??-??-??.npz")])
    overlap_days = [d for d in available_days if d in trades_by_date]
    print(f"Evaluating {len(overlap_days)} days with warehouse 1m SIP coverage ({overlap_days[0]} to {overlap_days[-1]})")

    results = []

    for d_str in overlap_days:
        day_path = WH_SIP / f"{d_str}.npz"
        day_data = load_day(day_path)
        sym_idx = day_data["idx"]
        c_mat = day_data["c"]
        h_mat = day_data["h"]
        l_mat = day_data["l"]

        for tr in trades_by_date[d_str]:
            sym = tr["symbol"]
            if sym not in sym_idx:
                continue
            s_i = sym_idx[sym]

            dt = tr["dt"]
            minute_of_day = dt.hour * 60 + dt.minute
            m_idx = minute_of_day - M0

            # Only analyze trades exited during market hours before 15:30
            if m_idx < 0 or m_idx >= 360:
                continue

            exit_px = tr["exit_px"]
            if exit_px <= 0 or np.isnan(exit_px):
                continue

            # Look forward +5, +15, +30 min, and EOD (min 380 = 15:50)
            def get_ret(offset):
                target_m = m_idx + offset
                if target_m >= 390:
                    target_m = 389
                px = c_mat[s_i, target_m]
                if np.isnan(px):
                    # search nearby
                    sub = c_mat[s_i, max(0, target_m - 2):min(390, target_m + 3)]
                    valid = sub[~np.isnan(sub)]
                    if len(valid) > 0:
                        px = valid[-1]
                if np.isnan(px) or px <= 0:
                    return np.nan
                return (px - exit_px) / exit_px * 10000.0  # basis points

            ret_5 = get_ret(5)
            ret_15 = get_ret(15)
            ret_30 = get_ret(30)
            ret_eod = get_ret(380 - m_idx) if m_idx < 380 else 0.0

            # MFE and MAE over next 30 minutes
            m_end = min(390, m_idx + 31)
            highs = h_mat[s_i, m_idx:m_end]
            lows = l_mat[s_i, m_idx:m_end]
            valid_h = highs[~np.isnan(highs)]
            valid_l = lows[~np.isnan(lows)]

            mfe_30 = ((np.max(valid_h) - exit_px) / exit_px * 10000.0) if len(valid_h) > 0 else np.nan
            mae_30 = ((np.min(valid_l) - exit_px) / exit_px * 10000.0) if len(valid_l) > 0 else np.nan

            results.append({
                "date": d_str,
                "symbol": sym,
                "hold_sec": tr["hold_sec"],
                "exit_px": exit_px,
                "ret_5": ret_5,
                "ret_15": ret_15,
                "ret_30": ret_30,
                "ret_eod": ret_eod,
                "mfe_30": mfe_30,
                "mae_30": mae_30,
            })

    print(f"\nSuccessfully evaluated {len(results)} valid closed trades")
    if not results:
        return 0

    # Calculate statistics
    for horizon, label in [("ret_5", "+5 min"), ("ret_15", "+15 min"), ("ret_30", "+30 min"), ("ret_eod", "EOD (15:50)")]:
        vals = [r[horizon] for r in results if not np.isnan(r[horizon])]
        if not vals:
            continue
        vals = np.array(vals)
        mean_bp = np.mean(vals)
        med_bp = np.median(vals)
        pct_positive = np.mean(vals > 0) * 100
        pct_super_runner = np.mean(vals > 100) * 100  # Ran > 100 bp (1%)
        pct_super_dump = np.mean(vals < -100) * 100   # Dropped > 100 bp
        std_err = np.std(vals) / np.sqrt(len(vals))
        t_stat = mean_bp / std_err if std_err > 0 else 0.0

        print(f"\n--- Post-Exit Markout: {label} (n={len(vals)}) ---")
        print(f"  Mean:      {mean_bp:+.2f} bp (SE: {std_err:.2f}, t: {t_stat:+.2f})")
        print(f"  Median:    {med_bp:+.2f} bp")
        print(f"  Win %:     {pct_positive:.1f}% positive")
        print(f"  Runners:   {pct_super_runner:.1f}% rose > +100 bp (+1%)")
        print(f"  Dumps:     {pct_super_dump:.1f}% fell < -100 bp (-1%)")

    # MFE vs MAE over 30 mins
    mfes = np.array([r["mfe_30"] for r in results if not np.isnan(r["mfe_30"])])
    maes = np.array([r["mae_30"] for r in results if not np.isnan(r["mae_30"])])
    print(f"\n--- 30-Minute Excursion Envelope (n={len(mfes)}) ---")
    print(f"  Avg MFE (Max Upside):  {np.mean(mfes):+.2f} bp (Median: {np.median(mfes):+.2f} bp)")
    print(f"  Avg MAE (Max Drawdown): {np.mean(maes):+.2f} bp (Median: {np.median(maes):+.2f} bp)")
    print(f"  MFE / |MAE| Ratio:     {abs(np.mean(mfes) / np.mean(maes)):.2f}")

    # Chronological Half Splits (docs/TEST_STANDARD.md requirement)
    half_idx = len(results) // 2
    h1 = results[:half_idx]
    h2 = results[half_idx:]
    print(f"\n--- Chronological Half Breakdown (Exit to +30 min) ---")
    for name, half in [("Half 1 (Early)", h1), ("Half 2 (Late)", h2)]:
        v30 = np.array([r["ret_30"] for r in half if not np.isnan(r["ret_30"])])
        if len(v30) > 0:
            m = np.mean(v30)
            se = np.std(v30) / np.sqrt(len(v30))
            t = m / se if se > 0 else 0
            print(f"  {name} (n={len(v30)}): Mean {m:+.2f} bp, Median {np.median(v30):+.2f} bp, t: {t:+.2f}")


if __name__ == "__main__":
    main()
