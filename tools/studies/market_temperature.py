#!/usr/bin/env python3
"""Market temperature (Bullish Bob, 2026-10-02 lesson): is a 'hot' morning — many stocks gapping up big — a better
day to trade momentum than a 'cold' one?

Temperature for day t = number of stocks (open >= $1, prior 20-day dollar volume >= $1M) whose open is >= G above
the prior close, G in {20%, 50%, 100%}. Measured at the 9:30 open from the 10-year daily panel (a proxy for the
7 AM premarket read; the premarket gap is usually close to the opening gap for these names).

Days are split HOT / MID / COLD by terciles of the 20%-gapper count, cutoffs fixed on IS (2017-2021) and applied
to OOS unchanged. Outcomes on each day, open -> close (bp):
  gappers      every >=20% gapper (the low-float universe Bob trades)
  gappers>=$10 the same at an open >= $10 (closer to the desk's universe)
  top movers   the day's 50 biggest |open->close| moves among liquid names >= $10 (how much room there is;
               a movement measure, not a direction)
t is across days. Also listed: the desk's own days since 9/16 (ai_reports/outcomes.jsonl) with each day's count.

Run on the mini from the repo root: .venv/bin/python tools/studies/market_temperature.py
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

LH = os.path.expanduser("~/lh_cache")
sys.path.insert(0, LH)
import lh_core as C  # noqa: E402
import lh_strat as S  # noqa: E402

IS_END = pd.Timestamp("2021-12-31")


def tt(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return (x.mean(), x.mean() / (x.std(ddof=1) / math.sqrt(len(x))), len(x)) if len(x) > 2 else (np.nan, np.nan, len(x))


def main():
    P = S.returns(C.load())
    ON, ID, rc, adv, dates = P["ON"], P["ID"], P["rc"], P["adv20"], P["dates"]
    T, N = ON.shape
    # open price on day t = raw prior close x (1 + gap); the gap uses adjusted prices, the level uses raw close
    rc_prev = np.vstack([np.full((1, N), np.nan), rc[:-1]])
    adv_prev = np.vstack([np.full((1, N), np.nan), adv[:-1]])
    o_raw = rc_prev * (1 + ON)
    liq = (o_raw >= 1) & (adv_prev >= 1e6)
    rows = []
    for t in range(260, T):
        g = ON[t]
        ok = liq[t] & np.isfinite(g) & np.isfinite(ID[t])
        cnt = {G: int(((g >= G) & ok).sum()) for G in (0.2, 0.5, 1.0)}
        gp = ok & (g >= 0.2)
        gp10 = gp & (o_raw[t] >= 10)
        mv = ok & (o_raw[t] >= 10) & (adv_prev[t] >= 20e6)
        absid = np.abs(ID[t, mv])
        top = np.sort(absid)[-50:] if absid.size >= 50 else absid
        rows.append({"d": dates[t], **{f"n{int(G * 100)}": v for G, v in cnt.items()},
                     "gap_id": np.nanmean(ID[t, gp]) if gp.any() else np.nan,
                     "gap10_id": np.nanmean(ID[t, gp10]) if gp10.any() else np.nan,
                     "top_abs": top.mean() if top.size else np.nan})
    df = pd.DataFrame(rows)
    is_m = df["d"] <= IS_END
    lo, hi = np.nanpercentile(df.loc[is_m, "n20"], [33.3, 66.7])
    df["temp"] = np.where(df["n20"] >= hi, "HOT", np.where(df["n20"] <= lo, "COLD", "MID"))
    print(f"days {len(df)} ({df['d'].iloc[0].date()}..{df['d'].iloc[-1].date()}); 20%-gapper count: IS median "
          f"{df.loc[is_m, 'n20'].median():.0f}, tercile cutoffs (IS) COLD <= {lo:.0f}, HOT >= {hi:.0f}; "
          f"OOS median {df.loc[~is_m, 'n20'].median():.0f}\n")
    print(f"  {'period':<5}{'temp':<6}{'days':>6}{'n20':>6}{'n100':>6}   {'gappers o->c (t)':>18}"
          f"{'gappers>=$10 (t)':>20}{'top-50 |move|':>15}")
    for pname, msk in (("IS", is_m), ("OOS", ~is_m)):
        for tp in ("HOT", "MID", "COLD"):
            s = df[msk & (df["temp"] == tp)]
            a, b, c = tt(s["gap_id"] * 1e4), tt(s["gap10_id"] * 1e4), np.nanmean(s["top_abs"] * 1e4)
            print(f"  {pname:<5}{tp:<6}{len(s):>6}{s['n20'].median():>6.0f}{s['n100'].median():>6.0f}"
                  f"   {a[0]:>+9.0f} ({a[1]:>+5.1f}){b[0]:>+11.0f} ({b[1]:>+5.1f}){c:>13.0f}")
        hot, cold = df[msk & (df["temp"] == "HOT")], df[msk & (df["temp"] == "COLD")]
        for k, lab in (("gap_id", "gappers"), ("gap10_id", "gappers>=$10")):
            h, c_ = hot[k].dropna() * 1e4, cold[k].dropna() * 1e4
            if len(h) > 2 and len(c_) > 2:
                w = (h.mean() - c_.mean()) / math.sqrt(h.var(ddof=1) / len(h) + c_.var(ddof=1) / len(c_))
                print(f"  {pname:<5}HOT - COLD {lab}: {h.mean() - c_.mean():+.0f} bp (Welch t {w:+.2f})")
        print()

    # the desk's own days
    path = os.path.join("ai_reports", "outcomes.jsonl")
    if os.path.exists(path):
        sys.path.insert(0, "tools")
        import bars  # noqa: E402
        by = defaultdict(list)
        for line in open(path):
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("entry_time") and r.get("exit_price") and r.get("entry_price") and not (r.get("hold_days") or 0):
                d = bars.day_of(float(r["entry_time"]))
                if d >= "2026-09-16" and float(r["entry_price"]) >= 10:
                    by[d].append((float(r["exit_price"]) / float(r["entry_price"]) - 1) * 1e4)
        cnt = {str(x.date()): (n, tp) for x, n, tp in zip(df["d"], df["n20"], df["temp"])}
        print("  desk days since 9/16 (>= $10 fills): day, 20%-gappers, temp, trades, mean P&L bp")
        for d in sorted(by):
            n, tp = cnt.get(d, (None, "?"))
            print(f"    {d}  {n if n is not None else '—':>4}  {tp:<5}{len(by[d]):>4}  {np.mean(by[d]):+7.1f}")


if __name__ == "__main__":
    main()
