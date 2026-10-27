#!/usr/bin/env python3
"""Grid Search Simulation for Leg-1 RTH Gate Parameters.

Evaluates historical trade outcomes from ai_reports/outcomes.jsonl against
a parameter grid for Leg-1 RTH Gate (wr_leg1_refusal):
  - max_time (cutoff time)
  - min_vol_mult (volume threshold)
  - min_day_chg (minimum momentum)
  - max_day_chg (maximum extension)
  - min_price (price floor for eligibility)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from itertools import product

ET = ZoneInfo("America/New_York")
OUTCOMES_PATH = Path("ai_reports/outcomes.jsonl")


def load_trades():
    trades = []
    if not OUTCOMES_PATH.exists():
        print(f"File not found: {OUTCOMES_PATH}")
        return []
    with open(OUTCOMES_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            pl = d.get("realized_pl_usd")
            r = d.get("realized_r_multiple")
            et = d.get("entry_time")
            px = d.get("entry_price")
            if pl is None or r is None or not et or not px:
                continue
            dt = datetime.fromtimestamp(et, ET)
            feats = d.get("features") or {}
            trades.append({
                "symbol": d.get("symbol", ""),
                "ts": et,
                "dt": dt,
                "day": dt.strftime("%Y-%m-%d"),
                "minute": dt.hour * 60 + dt.minute,
                "entry_price": float(px),
                "realized_pl": float(pl),
                "realized_r": float(r),
                "pct_change": feats.get("pct_change"),
                "rvol": feats.get("rvol"),
                "spread_r": feats.get("spread_r"),
            })
    return trades


def eval_filter(trade, cfg):
    """Returns True if trade passes (allowed to trade), False if rejected."""
    px = trade["entry_price"]
    min_px = cfg["min_price"]
    # If below min_price, stock is exempt from Leg-1 gate -> allowed
    if px < min_px:
        return True

    m = trade["minute"]
    if m > cfg["max_time_m"]:
        return False

    dchg = trade["pct_change"]
    if dchg is not None:
        if dchg < cfg["min_day_chg"]:
            return False
        if dchg > cfg["max_day_chg"]:
            return False

    rvol = trade["rvol"]
    if cfg["min_vol_mult"] > 0:
        if rvol is not None and rvol < cfg["min_vol_mult"]:
            return False

    return True


def calc_stats(trades):
    if not trades:
        return {
            "n": 0, "wr": 0.0, "tot_pl": 0.0, "tot_r": 0.0,
            "avg_r": 0.0, "avg_pl": 0.0, "pf": 0.0, "gw": 0.0, "gl": 0.0
        }
    n = len(trades)
    wins = [t for t in trades if t["realized_pl"] > 0.005]
    losses = [t for t in trades if t["realized_pl"] < -0.005]
    wr = len(wins) / n * 100.0
    tot_pl = sum(t["realized_pl"] for t in trades)
    tot_r = sum(t["realized_r"] for t in trades)
    gw = sum(t["realized_pl"] for t in wins)
    gl = abs(sum(t["realized_pl"] for t in losses))
    pf = gw / gl if gl > 0 else (999.0 if gw > 0 else 0.0)
    return {
        "n": n,
        "wr": wr,
        "tot_pl": tot_pl,
        "tot_r": tot_r,
        "avg_r": tot_r / n,
        "avg_pl": tot_pl / n,
        "pf": pf,
        "gw": gw,
        "gl": gl,
    }


def main():
    trades = load_trades()
    print(f"Loaded {len(trades)} trades.")

    # Base performance
    base = calc_stats(trades)
    base_ge20 = calc_stats([t for t in trades if t["entry_price"] >= 20.0])
    print(f"Baseline All: N={base['n']}, WR={base['wr']:.1f}%, P/L=${base['tot_pl']:.2f}, R={base['tot_r']:.2f}R, AvgR={base['avg_r']:+.4f}R, PF={base['pf']:.2f}")
    print(f"Baseline >=$20: N={base_ge20['n']}, WR={base_ge20['wr']:.1f}%, P/L=${base_ge20['tot_pl']:.2f}, R={base_ge20['tot_r']:.2f}R, AvgR={base_ge20['avg_r']:+.4f}R, PF={base_ge20['pf']:.2f}")

    # Grid dimensions
    times = [
        ("10:30", 10 * 60 + 30),
        ("11:00", 11 * 60),
        ("11:30", 11 * 60 + 30),
        ("12:00", 12 * 60),
        ("16:00", 16 * 60),
    ]
    vol_mults = [0.0, 0.8, 1.0, 1.2, 1.5, 2.0]
    min_chgs = [0.0, 0.5]
    max_chgs = [5.0, 8.0, 12.0, 999.0]
    min_prices = [15.0, 20.0]

    results = []

    for (t_label, t_m), vm, min_c, max_c, mp in product(times, vol_mults, min_chgs, max_chgs, min_prices):
        cfg = {
            "max_time_str": t_label,
            "max_time_m": t_m,
            "min_vol_mult": vm,
            "min_day_chg": min_c,
            "max_day_chg": max_c,
            "min_price": mp,
        }
        passed = [t for t in trades if eval_filter(t, cfg)]
        rejected = [t for t in trades if not eval_filter(t, cfg)]
        st = calc_stats(passed)
        rej_st = calc_stats(rejected)

        # Delta vs baseline
        pl_delta = st["tot_pl"] - base["tot_pl"]
        r_delta = st["tot_r"] - base["tot_r"]

        results.append({
            "cfg": cfg,
            "passed": st,
            "rejected": rej_st,
            "pl_delta": pl_delta,
            "r_delta": r_delta,
        })

    # Sort by Total P/L Delta (or Total P/L)
    results.sort(key=lambda x: x["passed"]["tot_pl"], reverse=True)

    print("\n" + "=" * 110)
    print(f"{'Top Configurations Ranked by Total P/L':^110}")
    print("=" * 110)
    print(f"{'Rank':4} | {'Cutoff':6} | {'MinVol':6} | {'ChgBand':12} | {'MinPx':5} | {'N':5} | {'WR%':5} | {'Tot P/L':9} | {'Tot R':8} | {'Avg R':8} | {'PF':5} | {'P/L Saved':9}")
    print("-" * 110)

    for i, r in enumerate(results[:20], 1):
        c = r["cfg"]
        p = r["passed"]
        chg_str = f"[{c['min_day_chg']:.1f}%..{c['max_day_chg']:.0f}%]" if c['max_day_chg'] < 900 else f">={c['min_day_chg']:.1f}%"
        print(f"{i:4} | {c['max_time_str']:6} | {c['min_vol_mult']:4.1f}x | {chg_str:12} | ${c['min_price']:4.0f} | {p['n']:5} | {p['wr']:4.1f}% | ${p['tot_pl']:8.2f} | {p['tot_r']:7.2f}R | {p['avg_r']:+.4f}R | {p['pf']:4.2f} | ${r['pl_delta']:+8.2f}")

    print("\n" + "=" * 110)
    print(f"{'Comparison: Default Shipped Settings vs Baseline':^110}")
    print("=" * 110)
    # find default shipped settings: 11:00, 1.5x, [0.5%..5.0%], $20.0
    def_match = [r for r in results if r["cfg"]["max_time_str"] == "11:00" and r["cfg"]["min_vol_mult"] == 1.5 and r["cfg"]["min_day_chg"] == 0.5 and r["cfg"]["max_day_chg"] == 5.0 and r["cfg"]["min_price"] == 20.0]
    if def_match:
        dm = def_match[0]
        c = dm["cfg"]
        p = dm["passed"]
        rej = dm["rejected"]
        print(f"Default Leg-1 Config: MaxTime={c['max_time_str']}, MinVol={c['min_vol_mult']}x, Chg=[{c['min_day_chg']}%..{c['max_day_chg']}%, MinPx=${c['min_price']}")
        print(f"Passed: N={p['n']}, WR={p['wr']:.1f}%, P/L=${p['tot_pl']:.2f}, TotR={p['tot_r']:.2f}R, AvgR={p['avg_r']:+.4f}R, PF={p['pf']:.2f}")
        print(f"Rejected (Filtered Out): N={rej['n']}, WR={rej['wr']:.1f}%, P/L=${rej['tot_pl']:.2f}, TotR={rej['tot_r']:.2f}R, AvgR={rej['avg_r']:+.4f}R, PF={rej['pf']:.2f}")
        print(f"Net Improvement vs Baseline: P/L=${dm['pl_delta']:+.2f}, R={dm['r_delta']:+.2f}R")


if __name__ == "__main__":
    main()
