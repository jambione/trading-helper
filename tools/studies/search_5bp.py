#!/usr/bin/env python3
"""Search for Configurations Achieving +5+ bp Mean Return."""
import json
import statistics as st
from datetime import datetime
from itertools import product
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OUTCOMES_PATH = Path("ai_reports/outcomes.jsonl")


def main():
    trades = []
    with open(OUTCOMES_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            en = d.get("entry_price")
            ex = d.get("exit_price")
            et = d.get("entry_time")
            if en and ex and en > 0 and et:
                dt = datetime.fromtimestamp(et, ET)
                feats = d.get("features") or {}
                trades.append({
                    "en": en,
                    "ex": ex,
                    "bp": (ex - en) / en * 10000.0,
                    "pl": d.get("realized_pl_usd", 0.0),
                    "r": d.get("realized_r_multiple", 0.0),
                    "minute": dt.hour * 60 + dt.minute,
                    "dc": feats.get("pct_change"),
                    "rv": feats.get("rvol"),
                    "px": en,
                    "month": dt.strftime("%Y-%m"),
                })

    print(f"Loaded {len(trades)} trades with valid entry/exit prices.")

    # 1. Option 1 Staged as currently configured
    def opt1_pass(t):
        if t["minute"] > 13 * 60:
            return False
        dc = t["dc"]
        if dc is not None and (dc < 0.5 or dc > 6.0):
            return False
        rv = t["rv"]
        m = t["minute"]
        req = 1.5 if m <= 630 else (1.2 if m <= 690 else 1.0)
        if rv is not None and rv < req:
            return False
        return True

    opt1_trades = [t for t in trades if opt1_pass(t)]
    opt1_bp = [t["bp"] for t in opt1_trades]
    print(f"Option 1 Staged: N={len(opt1_trades)}, Mean={st.mean(opt1_bp):+.2f} bp, Median={st.median(opt1_bp):+.2f} bp, WR={sum(1 for b in opt1_bp if b > 0)/len(opt1_bp)*100:.1f}%, Tot P/L=${sum(t['pl'] for t in opt1_trades):+.2f}")

    # 2. Grid Search
    cutoffs = [("11:00", 660), ("11:30", 690), ("12:00", 720), ("12:30", 750), ("13:00", 780)]
    vols = [1.2, 1.5, 1.8, 2.0, 2.2, 2.5]
    chg_bands = [(0.5, 3.0), (0.5, 4.0), (1.0, 4.0), (0.5, 5.0), (1.0, 5.0), (0.5, 6.0)]
    min_prices = [15.0, 20.0, 25.0]

    runs = []
    for (c_str, c_m), vm, (dc_min, dc_max), mp in product(cutoffs, vols, chg_bands, min_prices):
        def test_pass(t):
            if t["px"] < mp:
                return False
            if t["minute"] > c_m:
                return False
            dc = t["dc"]
            if dc is not None and (dc < dc_min or dc > dc_max):
                return False
            rv = t["rv"]
            if rv is not None and rv < vm:
                return False
            return True

        sub = [t for t in trades if test_pass(t)]
        if len(sub) >= 30:
            bps = [t["bp"] for t in sub]
            mean_bp = st.mean(bps)
            med_bp = st.median(bps)
            wr = sum(1 for b in bps if b > 0) / len(bps) * 100.0
            tot_pl = sum(t["pl"] for t in sub)
            gw = sum(t["pl"] for t in sub if t["pl"] > 0.005)
            gl = abs(sum(t["pl"] for t in sub if t["pl"] < -0.005))
            pf = gw / gl if gl > 0 else 999.0
            runs.append({
                "c_str": c_str, "vm": vm, "band": (dc_min, dc_max), "mp": mp,
                "n": len(sub), "mean_bp": mean_bp, "med_bp": med_bp, "wr": wr,
                "tot_pl": tot_pl, "pf": pf
            })

    # Sort by mean_bp
    runs.sort(key=lambda x: x["mean_bp"], reverse=True)

    print("\n" + "=" * 90)
    print(f"{'TOP CONFIGURATIONS RANKED BY MEAN BASIS POINTS (BP)':^90}")
    print("=" * 90)
    print(f"Cutoff | MinVol | DayChange  | MinPx | N   | Mean BP   | Median BP | WinRate | ProfitFactor | Tot P/L")
    print("-" * 90)

    for r in runs[:25]:
        b = r["band"]
        print(f"{r['c_str']:6} | {r['vm']:4.1f}x  | {b[0]:.1f}%-{b[1]:.1f}% | ${r['mp']:4.0f} | {r['n']:3} | {r['mean_bp']:+7.2f} bp | {r['med_bp']:+7.2f} bp | {r['wr']:5.1f}%  | {r['pf']:12.2f} | ${r['tot_pl']:+7.2f}")


if __name__ == "__main__":
    main()
