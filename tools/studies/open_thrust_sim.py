#!/usr/bin/env python3
"""3-Minute Open Thrust Day Trading Simulation (`open_thrust_sim`).
Tests entering during the opening 1-2 minutes (09:31-09:32 ET) on morning catalyst runners
and holding for exactly 3 minutes (flat before 09:36 ET), capturing the initial opening surge
BEFORE any morning fade or pullback begins.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
import numpy as np

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "ai_reports", "open_thrust")
os.makedirs(OUT_DIR, exist_ok=True)

BARS_DIR = os.path.join(ROOT, "ai_reports", "catalyst_pullback", "bars")
CANDS_FILE = os.path.join(ROOT, "ai_reports", "catalyst_pullback", "classified_candidates.json")


def run_sim():
    if not os.path.exists(CANDS_FILE):
        raise SystemExit(f"Missing {CANDS_FILE}")
    candidates = json.load(open(CANDS_FILE))
    print(f"Loaded {len(candidates)} candidates from catalyst_pullback...")
    
    primary = [c for c in candidates if c.get("is_primary")]
    tech = [c for c in candidates if not c.get("is_primary")]
    print(f"Primary catalysts: {len(primary)}, Technical controls: {len(tech)}")
    
    # Cost model: consolidated SIP spread (~10 bp round trip on $20+ catalyst stocks) + 1 bp fee
    # We will score at 5 bp, 10 bp, and 15 bp round-trip cost
    
    results = {"primary": [], "tech": []}
    
    for c in candidates:
        day_str = c["day"]
        sym = c["sym"]
        bar_path = os.path.join(BARS_DIR, f"{day_str}_{sym}.json")
        if not os.path.exists(bar_path):
            continue
        
        bars = json.load(open(bar_path))
        if len(bars) < 8:
            continue
        
        # bar 0 = 09:30-09:31
        # bar 1 = 09:31-09:32
        # bar 2 = 09:32-09:33
        b0 = bars[0]
        b1 = bars[1]
        
        # Setup: Open Thrust Entry
        # Bar 0 is green (close > open) or breaks above open
        # Enter at open of Bar 1 (09:31 ET) or Bar 2 (09:32 ET)
        if b0["c"] <= b0["o"]:
            continue  # first 1m bar red -> skip opening thrust
        
        entry_bar_idx = 1  # enter at 09:31:00 open
        entry_px = b1["o"]
        if entry_px < 20.0:
            continue
        
        # 3-minute hold: exit at close of bar 3 (09:33:59 ET)
        # 5-minute hold: exit at close of bar 5 (09:35:59 ET)
        exit_3m_px = bars[min(3, len(bars) - 1)]["c"]
        exit_5m_px = bars[min(5, len(bars) - 1)]["c"]
        
        gross_3m_bp = (exit_3m_px - entry_px) / entry_px * 10_000
        gross_5m_bp = (exit_5m_px - entry_px) / entry_px * 10_000
        
        rec = {
            "day": day_str,
            "sym": sym,
            "half": c["half"],
            "entry_px": entry_px,
            "gross_3m_bp": gross_3m_bp,
            "gross_5m_bp": gross_5m_bp,
            "net_3m_10bp": gross_3m_bp - 10.0,
            "net_3m_5bp": gross_3m_bp - 5.0,
            "net_5m_10bp": gross_5m_bp - 10.0,
        }
        
        if c.get("is_primary"):
            results["primary"].append(rec)
        else:
            results["tech"].append(rec)
    
    print("\n" + "=" * 70)
    print("3-MINUTE OPEN THRUST (09:31-09:34 ET) SIMULATION RESULTS")
    print("=" * 70)
    
    for group_name, records in [("PRIMARY (Fundamental Catalysts)", results["primary"]), ("CONTROL (Technical Gappers)", results["tech"])]:
        print(f"\nGroup: {group_name} (N = {len(records)} trades)")
        for h in ("A", "B", "Pooled"):
            if h == "Pooled":
                sub = records
            else:
                sub = [r for r in records if r["half"] == h]
            if not sub:
                continue
            
            # Day-clustered means
            by_day = defaultdict(list)
            for r in sub:
                by_day[r["day"]].append(r["gross_3m_bp"])
            d_means_gross = [float(np.mean(v)) for v in by_day.values()]
            
            mean_gross = float(np.mean(d_means_gross))
            se_gross = float(np.std(d_means_gross, ddof=1) / math.sqrt(len(d_means_gross))) if len(d_means_gross) > 1 else 0
            t_gross = mean_gross / se_gross if se_gross > 0 else 0
            
            mean_net_10 = mean_gross - 10.0
            mean_net_5 = mean_gross - 5.0
            win_rate = sum(1 for r in sub if r["gross_3m_bp"] > 10.0) / len(sub) * 100.0
            
            print(f"  Half {h:6s} ({len(sub):4d} trades, {len(d_means_gross):3d} days) | Gross 3m: {mean_gross:+6.2f} bp (t: {t_gross:+5.2f}) | Net@10bp: {mean_net_10:+6.2f} bp | Net@5bp: {mean_net_5:+6.2f} bp | Win: {win_rate:4.1f}%")

    out_file = os.path.join(OUT_DIR, "result.json")
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved open thrust results to {out_file}.\n" + "=" * 70)


if __name__ == "__main__":
    run_sim()
