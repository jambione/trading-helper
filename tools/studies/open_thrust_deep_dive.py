#!/usr/bin/env python3
"""open_thrust_deep_dive.py: Deep optimization and statistical test of the 3-Minute Open Thrust strategy.
Evaluates 1,403 classified morning catalyst events across 417 trading sessions (2024-2026).
Tests filters:
1. Bar 0 candle anatomy: Close near high (hammer/thrust vs doji/reversal)
2. Gap magnitude: >= 3%, >= 5%, >= 8%
3. Fundamental catalysts (earnings/FDA/M&A) vs technical gappers
4. Exit rules: Fixed 2m/3m/5m vs Profit Target (+30 bp) with tight Stop (-20 bp)
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BARS_DIR = os.path.join(ROOT, "ai_reports", "catalyst_pullback", "bars")
CANDS_FILE = os.path.join(ROOT, "ai_reports", "catalyst_pullback", "classified_candidates.json")


def run():
    if not os.path.exists(CANDS_FILE):
        print(f"Missing {CANDS_FILE}")
        return

    candidates = json.load(open(CANDS_FILE))
    print(f"Loaded {len(candidates)} classified catalyst candidates from 2024-2026.")

    # Parse and simulate
    records = []

    for c in candidates:
        day_str = c["day"]
        sym = c["sym"]
        bar_path = os.path.join(BARS_DIR, f"{day_str}_{sym}.json")
        if not os.path.exists(bar_path):
            continue

        try:
            bars = json.load(open(bar_path))
        except Exception:
            continue

        if len(bars) < 8:
            continue

        b0 = bars[0]  # 09:30 - 09:31
        b1 = bars[1]  # 09:31 - 09:32
        entry_px = b1["o"]
        if entry_px < 20.0:
            continue

        b0_o, b0_h, b0_l, b0_c = b0["o"], b0["h"], b0["l"], b0["c"]
        b0_range = b0_h - b0_l
        if b0_range <= 0:
            continue

        b0_is_green = b0_c > b0_o
        b0_close_location = (b0_c - b0_l) / b0_range  # 0.0 (at low) to 1.0 (at high)
        b0_return_bp = ((b0_c - b0_o) / b0_o) * 10000.0

        gap_pct = float(c.get("gap") or 0.0) * 100.0
        is_primary = bool(c.get("is_primary"))

        # Measure forward horizons:
        # Exit at close of bar 1 (1m hold), bar 2 (2m hold), bar 3 (3m hold), bar 5 (5m hold)
        px_1m = bars[1]["c"]
        px_2m = bars[2]["c"]
        px_3m = bars[3]["c"]
        px_5m = bars[5]["c"]

        ret_1m_bp = ((px_1m - entry_px) / entry_px) * 10000.0
        ret_2m_bp = ((px_2m - entry_px) / entry_px) * 10000.0
        ret_3m_bp = ((px_3m - entry_px) / entry_px) * 10000.0
        ret_5m_bp = ((px_5m - entry_px) / entry_px) * 10000.0

        # High watermark within 3 minutes and 5 minutes (MFE)
        max_px_3m = max(bars[i]["h"] for i in range(1, 4))
        min_px_3m = min(bars[i]["l"] for i in range(1, 4))
        mfe_3m_bp = ((max_px_3m - entry_px) / entry_px) * 10000.0
        mae_3m_bp = ((min_px_3m - entry_px) / entry_px) * 10000.0

        # Bracket Simulation: Target +30 bp, Stop -20 bp within first 5 minutes
        bracket_hit = None
        for i in range(1, 6):
            b = bars[i]
            hi_bp = ((b["h"] - entry_px) / entry_px) * 10000.0
            lo_bp = ((b["l"] - entry_px) / entry_px) * 10000.0
            if hi_bp >= 30.0 and lo_bp > -20.0:
                bracket_hit = +30.0
                break
            elif lo_bp <= -20.0 and hi_bp < 30.0:
                bracket_hit = -20.0
                break
            elif hi_bp >= 30.0 and lo_bp <= -20.0:
                # Both hit in same bar -> assume stop hit first (conservative)
                bracket_hit = -20.0
                break
        if bracket_hit is None:
            bracket_hit = ret_5m_bp  # flat at 5 min

        records.append({
            "day": day_str,
            "sym": sym,
            "half": c.get("half", "A"),
            "is_primary": is_primary,
            "gap_pct": gap_pct,
            "b0_is_green": b0_is_green,
            "b0_close_location": b0_close_location,
            "b0_return_bp": b0_return_bp,
            "entry_px": entry_px,
            "ret_1m_bp": ret_1m_bp,
            "ret_2m_bp": ret_2m_bp,
            "ret_3m_bp": ret_3m_bp,
            "ret_5m_bp": ret_5m_bp,
            "mfe_3m_bp": mfe_3m_bp,
            "mae_3m_bp": mae_3m_bp,
            "bracket_ret_bp": bracket_hit,
        })

    print(f"Evaluated {len(records)} events with full 1m intraday bar paths.\n")

    # Evaluate Filters
    print("=" * 115)
    print("3-MINUTE OPEN THRUST: FILTER & STRATEGY MATRIX (Scored after 7.0 bp Round-Trip Friction)")
    print("=" * 115)
    print(f"{'Strategy / Filter Variation':45s} | {'N':5s} | {'Gross Bp':10s} | {'Net @ 7bp':10s} | {'Win %':7s} | {'t-stat':8s} | {'H1 / H2 Net':16s}")
    print("-" * 115)

    filters = [
        ("1. All Morning Gappers (Unfiltered 3m hold)", lambda r: True),
        ("2. Bar 0 Green Only (3m hold)", lambda r: r["b0_is_green"]),
        ("3. Bar 0 Strong Bull (Close in Top 30% of Bar)", lambda r: r["b0_close_location"] >= 0.70),
        ("4. Fundamental Catalysts (Primary News Only)", lambda r: r["is_primary"]),
        ("5. Fundamental Catalysts + Bar 0 Green", lambda r: r["is_primary"] and r["b0_is_green"]),
        ("6. Fundamental Catalysts + Strong Bull Bar 0", lambda r: r["is_primary"] and r["b0_close_location"] >= 0.70),
        ("7. Catalysts + Strong Bull + Gap >= 4%", lambda r: r["is_primary"] and r["b0_close_location"] >= 0.70 and r["gap_pct"] >= 4.0),
        ("8. Catalysts + Strong Bull (2-Minute Quick Exit)", lambda r: r["is_primary"] and r["b0_close_location"] >= 0.70),
        ("9. Catalysts + Strong Bull (+30bp Tgt / -20bp Stop)", lambda r: r["is_primary"] and r["b0_close_location"] >= 0.70),
    ]

    for label, fn in filters:
        sub = [r for r in records if fn(r)]
        if not sub:
            continue

        if "2-Minute" in label:
            gross_vals = [r["ret_2m_bp"] for r in sub]
        elif "+30bp Tgt" in label:
            gross_vals = [r["bracket_ret_bp"] for r in sub]
        else:
            gross_vals = [r["ret_3m_bp"] for r in sub]

        net_vals = [x - 7.0 for x in gross_vals]  # 7 bp round trip friction
        n = len(sub)
        mean_gross = np.mean(gross_vals)
        mean_net = np.mean(net_vals)
        win_pct = np.mean([x > 0 for x in net_vals]) * 100.0
        se = np.std(net_vals) / math.sqrt(n) if n > 1 else 1.0
        t_stat = mean_net / se if se > 0 else 0.0

        # Half breakdown
        h1 = [net_vals[i] for i, r in enumerate(sub) if r["half"] == "A"]
        h2 = [net_vals[i] for i, r in enumerate(sub) if r["half"] == "B"]
        h1_net = np.mean(h1) if h1 else 0.0
        h2_net = np.mean(h2) if h2 else 0.0

        print(f"{label:45s} | {n:5d} | {mean_gross:+8.2f}bp | {mean_net:+8.2f}bp | {win_pct:5.1f}% | {t_stat:+6.2f}   | {h1_net:+6.1f} / {h2_net:+6.1f} bp")

    print("=" * 115)


if __name__ == "__main__":
    run()
