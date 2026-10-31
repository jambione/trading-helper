#!/usr/bin/env python3
"""Closing Flow SIP Simulation (`closing_flow_sip_sim`).
Tests entering at 15:50:00 ET on names experiencing heavy institutional pre-close accumulation
(15:45-15:50 ET volume and price surge) and holding to 15:59:00 ET (closing print),
completely free using local warehouse SIP 1-minute bars across 120 sessions.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
import numpy as np

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "ai_reports", "closing_flow_sip")
os.makedirs(OUT_DIR, exist_ok=True)

WH_DIR = os.path.join(ROOT, "ai_reports", "warehouse", "sip")
MIN_PRICE = 20.00


def run_sim():
    if not os.path.exists(WH_DIR):
        raise SystemExit(f"Missing {WH_DIR}")
    
    days = sorted(f[:-4] for f in os.listdir(WH_DIR) if f.endswith(".npz"))
    print(f"Running Closing Flow simulation across {len(days)} warehouse sessions...")
    
    records = []
    
    for day in days:
        h_label = "A" if day <= "2026-07-29" else "B"
        z = np.load(os.path.join(WH_DIR, f"{day}.npz"))
        syms, idx, mins, o, h, l, c, v = z["syms"], z["sym_idx"], z["minute"], z["o"], z["h"], z["l"], z["c"], z["v"]
        
        order = np.lexsort((mins, idx))
        idx, mins, o, h, l, c, v = idx[order], mins[order], o[order], h[order], l[order], c[order], v[order]
        bounds = np.searchsorted(idx, np.arange(len(syms) + 1))
        
        day_events = []
        for si, sym in enumerate(syms):
            a, b = bounds[si], bounds[si + 1]
            if b - a < 300:
                continue
            s_mins = mins[a:b]
            s_c = c[a:b]
            s_o = o[a:b]
            s_v = v[a:b]
            
            # Find minute 945 (15:45), 950 (15:50), and 959 (15:59)
            m_map = {int(m): i for i, m in enumerate(s_mins)}
            if 945 not in m_map or 950 not in m_map or 959 not in m_map:
                continue
            
            i_945 = m_map[945]
            i_950 = m_map[950]
            i_959 = m_map[959]
            
            px_entry = float(s_o[i_950])
            if px_entry < MIN_PRICE:
                continue
            
            # 15:45 to 15:50 volume and return
            vol_5m = float(np.sum(s_v[i_945:i_950]))
            total_day_vol = float(np.sum(s_v))
            vol_share = vol_5m / total_day_vol if total_day_vol > 0 else 0
            
            ret_5m = (float(s_c[i_950 - 1]) - float(s_o[i_945])) / float(s_o[i_945])
            
            # MOC drift: 15:50 open to 15:59 close
            px_exit = float(s_c[i_959])
            drift_gross_bp = (px_exit - px_entry) / px_entry * 10_000
            
            day_events.append({
                "day": day,
                "sym": str(sym),
                "half": h_label,
                "entry_px": px_entry,
                "vol_share": vol_share,
                "ret_5m": ret_5m,
                "drift_gross_bp": drift_gross_bp,
            })
        
        # Rank by 15:45-15:50 volume share
        if len(day_events) >= 20:
            day_events.sort(key=lambda x: x["vol_share"], reverse=True)
            # Top 10 heavy close names with positive 15:45-15:50 return
            buy_events = [e for e in day_events if e["ret_5m"] > 0][:10]
            records.extend(buy_events)
            
            # Controls: bottom decile / average volume names
            ctrl_events = [e for e in day_events if e not in buy_events][:10]
            for ce in ctrl_events:
                ce_copy = dict(ce)
                ce_copy["is_control"] = True
                records.append(ce_copy)
    
    print("\n" + "=" * 70)
    print("15:50 CLOSING AUCTION DRIFT (15:50-15:59 ET) SIMULATION RESULTS")
    print("=" * 70)
    
    primary = [r for r in records if not r.get("is_control")]
    ctrl = [r for r in records if r.get("is_control")]
    
    for grp_name, recs in [("PRIMARY (Top MOC Volume + Up 15:45-15:50)", primary), ("CONTROL (Average Volume Names)", ctrl)]:
        print(f"\nGroup: {grp_name} (N = {len(recs)} trades across {len(set(r['day'] for r in recs))} sessions)")
        for h in ("A", "B", "Pooled"):
            if h == "Pooled":
                sub = recs
            else:
                sub = [r for r in recs if r["half"] == h]
            if not sub:
                continue
            
            # Day-clustered means
            by_day = defaultdict(list)
            for r in sub:
                by_day[r["day"]].append(r["drift_gross_bp"])
            d_means = [float(np.mean(v)) for v in by_day.values()]
            
            mean_gross = float(np.mean(d_means))
            se = float(np.std(d_means, ddof=1) / math.sqrt(len(d_means))) if len(d_means) > 1 else 0
            t_stat = mean_gross / se if se > 0 else 0
            
            # Quoted cost: MOC exit has zero exit spread (auction cross), entry has ~3 bp spread on $20+ liquid names
            net_3bp = mean_gross - 3.0
            net_5bp = mean_gross - 5.0
            win_rate = sum(1 for r in sub if r["drift_gross_bp"] > 3.0) / len(sub) * 100.0
            
            print(f"  Half {h:6s} ({len(sub):4d} trades, {len(d_means):3d} days) | Gross Drift: {mean_gross:+6.2f} bp (t: {t_stat:+5.2f}) | Net@3bp: {net_3bp:+6.2f} bp | Net@5bp: {net_5bp:+6.2f} bp | Win: {win_rate:4.1f}%")

    out_file = os.path.join(OUT_DIR, "result.json")
    with open(out_file, "w") as f:
        json.dump(records, f, indent=2)
    print(f"\nSaved closing flow results to {out_file}.\n" + "=" * 70)


if __name__ == "__main__":
    run_sim()
