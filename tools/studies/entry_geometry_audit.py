#!/usr/bin/env python3
"""
Entry Geometry Audit Tool
Empirically evaluates the relationship between entry price altitude (Williams %R,
distance to range extremes) and trade outcome (win rate, net return, never-green rate).
Proves why 'buying low' (dips) vs 'buying high' (exhaustion) perform the way they do in our desk.
"""

import json
import os
import sys
import numpy as np

def run_audit(outcomes_path="ai_reports/outcomes.jsonl"):
    if not os.path.exists(outcomes_path):
        print(f"Error: {outcomes_path} not found.")
        return None

    rows = []
    with open(outcomes_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                feat = r.get("features")
                if not feat or not isinstance(feat, dict):
                    continue

                pctr = feat.get("pctr")
                if pctr is None:
                    continue

                # Net return
                net_ret = r.get("net_return_bp")
                if net_ret is None:
                    ep = r.get("entry_price")
                    xp = r.get("exit_price")
                    if ep and xp and ep > 0:
                        net_ret = (xp - ep) / ep * 10000.0 - 3.0 # standard 3 bp friction
                    else:
                        continue

                peak_gain = r.get("peak_gain_bp")
                if peak_gain is None:
                    peak_px = r.get("peak_price") or r.get("max_price")
                    ep = r.get("entry_price")
                    if peak_px and ep and ep > 0:
                        peak_gain = (peak_px - ep) / ep * 10000.0
                    else:
                        peak_gain = 0.0

                never_green = (peak_gain < 5.0)

                rows.append({
                    "symbol": r.get("symbol"),
                    "pctr": float(pctr),
                    "net_bp": float(net_ret),
                    "peak_bp": float(peak_gain),
                    "never_green": never_green,
                    "win": (float(net_ret) > 0.0)
                })
            except Exception:
                continue

    if not rows:
        print("No valid rows found.")
        return None

    print(f"Loaded {len(rows)} trades with valid %R entry features.\n")

    # Bucket definitions
    buckets = [
        ("Deep Oversold (<-70)", lambda p: p < -70),
        ("Pullback / Lower Half (-70 to -50)", lambda p: -70 <= p < -50),
        ("Mid / Transition (-50 to -30)", lambda p: -50 <= p < -30),
        ("Overbought / Continuation (-30 to -15)", lambda p: -30 <= p < -15),
        ("Terminal Apex (>= -15)", lambda p: p >= -15)
    ]

    report = []
    header = f"| Altitude Band (%R) | N Trades | Share % | Win Rate % | Never-Green % | Mean Net (bp) | Median Net (bp) |"
    sep = f"|---|---:|---:|---:|---:|---:|---:|"
    report.append(header)
    report.append(sep)

    total_n = len(rows)
    for name, pred in buckets:
        matched = [r for r in rows if pred(r["pctr"])]
        n = len(matched)
        if n == 0:
            continue
        share = n / total_n * 100.0
        win_rate = sum(1 for r in matched if r["win"]) / n * 100.0
        ng_rate = sum(1 for r in matched if r["never_green"]) / n * 100.0
        mean_net = np.mean([r["net_bp"] for r in matched])
        med_net = np.median([r["net_bp"] for r in matched])

        line = f"| {name} | {n} | {share:.1f}% | {win_rate:.1f}% | {ng_rate:.1f}% | {mean_net:+.1f} bp | {med_net:+.1f} bp |"
        report.append(line)

    # Decile breakdown
    all_pctrs = [r["pctr"] for r in rows]
    decile_cutoffs = np.percentile(all_pctrs, np.arange(10, 100, 10))

    summary = "\n".join(report)
    print(summary)
    return summary

if __name__ == "__main__":
    outcomes_file = sys.argv[1] if len(sys.argv) > 1 else "ai_reports/outcomes.jsonl"
    run_audit(outcomes_file)
