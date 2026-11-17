#!/usr/bin/env python3
"""relative_admit — does market-relative admission (market_tape.py, ai_relative_strength_admit) add opens without
lowering trade quality? Operator 11/17: "keep the strategy intact with a high win ratio, and also have more opens".

Read-only, SIP minute-bar warehouse. The replay cannot answer this: the movers/tight screeners apply the "up N%" bar
BEFORE the desk records its sources, so names the new rule would add were never recorded. Here both rules are applied
to the same minute bars.

  universe   the tight screener's: top 400 by prior-session dollar volume, ETFs excluded (board_structure_family.universe)
  price      $20-$400 at the signal minute (desk floor $20, tight ceiling $400)
  signal     the desk's live pre-square arm, fast %R 21 (evening_family.presquare_bars, prior-session warm-up), refractory 30
  admission  BASE: day change >= 1.5% at the signal minute.  REL: >= 1.5% + min(0, SPY day change at that minute)
  trade      buy the open of t+1, sell the close of t+10 (the desk's ~10-minute hold); cost 6 bp round trip (10 bp stress)

Reports opens per 10 minutes (signals, an upper bound on desk opens: spread/volume/tape gates are not modelled and apply
equally to both rules), and net bp / win % for BASE trades and for the EXTRA trades REL adds - all days and red-SPY
days - with a day-clustered t on (extra mean - base mean). Descriptive: no live change follows from it alone.

    .venv/bin/python tools/studies/relative_admit.py [--days N]      # writes ai_reports/relative_admit/result.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day  # noqa: E402
from board_structure_family import universe  # noqa: E402
from evening_family import minute_grid, presquare_bars, first_true, trade, WH_SIP, ALL_ETF  # noqa: E402

BASE_PCT = 1.5
PX_LO, PX_HI = 20.0, 400.0
H = 10
COST, STRESS = 6.0, 10.0
OUT = ROOT / "ai_reports" / "relative_admit"


def last_close(row):
    v = row[~np.isnan(row)]
    return float(v[-1]) if len(v) else float("nan")


def rel_bar(spy_pct: float) -> float:
    """The REL admission bar (market_tape.effective_min_pct with the flag on)."""
    return BASE_PCT + min(0.0, spy_pct)


def score_day(day, prev):
    """-> list of {t, sym, pct, spy, base, rel, net} for every pre-square signal in the price band."""
    si, psi = day["idx"].get("SPY"), prev["idx"].get("SPY")
    if si is None or psi is None:
        return []
    spy_c, spy_pc = day["c"][si], last_close(prev["c"][psi])
    names = universe(day, prev, set(ALL_ETF))
    rows = []
    for s in names:
        i, j = day["idx"][s], prev["idx"].get(s)
        if j is None:
            continue
        pc = last_close(prev["c"][j])
        if not pc > 0:
            continue
        cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], day["c"][i], day["h"][i], day["l"][i])
        for t in first_true(presquare_bars(cc, hh, ll, off, 21)):
            px = day["c"][i][t]
            if np.isnan(px) or not (PX_LO <= px <= PX_HI) or np.isnan(spy_c[t]):
                continue
            pct = (px / pc - 1) * 100
            spy = (spy_c[t] / spy_pc - 1) * 100
            net = trade(day["o"][i], day["c"][i], t, H, COST)
            if net is None:
                continue
            rows.append({"t": int(t), "sym": s, "pct": round(float(pct), 3), "spy": round(float(spy), 3),
                         "base": bool(pct >= BASE_PCT), "rel": bool(pct >= rel_bar(spy)), "net": round(float(net), 2)})
    return rows


def summarize(days_rows: dict) -> dict:
    """days_rows {day: rows}. Signals per 10 min, BASE vs EXTRA (rel and not base) quality, day-clustered t."""
    minutes = 346  # signal window 15..360
    out = {}
    for label, keep in (("all_days", lambda rows: True),
                        ("red_days", lambda rows: bool(rows) and np.median([r["spy"] for r in rows]) < 0)):
        sel = {d: r for d, r in days_rows.items() if keep(r)}
        nd = len(sel)
        b = [x for r in sel.values() for x in r if x["base"]]
        e = [x for r in sel.values() for x in r if x["rel"] and not x["base"]]

        def q(xs):
            if not xs:
                return {"n": 0}
            n = np.array([x["net"] for x in xs])
            return {"n": len(xs), "mean_net_bp": round(float(n.mean()), 2), "win_pct": round(float((n > 0).mean() * 100), 1),
                    "mean_net_stress_bp": round(float(n.mean() - (STRESS - COST)), 2)}
        diffs = []
        for r in sel.values():
            bb = [x["net"] for x in r if x["base"]]
            ee = [x["net"] for x in r if x["rel"] and not x["base"]]
            if bb and ee:
                diffs.append(np.mean(ee) - np.mean(bb))
        t = (float(np.mean(diffs) / (np.std(diffs, ddof=1) / math.sqrt(len(diffs))))
             if len(diffs) >= 3 and np.std(diffs, ddof=1) > 0 else None)
        out[label] = {
            "days": nd,
            "base_per_10m": round(len(b) / max(1, nd) / minutes * 10, 3),
            "rel_per_10m": round((len(b) + len(e)) / max(1, nd) / minutes * 10, 3),
            "base": q(b), "extra": q(e),
            "extra_minus_base_bp_daymean": round(float(np.mean(diffs)), 2) if diffs else None,
            "t_day_clustered": round(t, 2) if t is not None else None, "days_with_both": len(diffs)}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="only the most recent N days (0 = all)")
    a = ap.parse_args(argv)
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    if a.days:
        files = files[-(a.days + 1):]
    days_rows, prev = {}, None
    for f in files:
        day = load_day(f)
        if prev is not None:
            days_rows[f.stem] = score_day(day, prev)
            print(f.stem, len(days_rows[f.stem]), "signals", flush=True)
        prev = day
    res = {"generated": date.today().isoformat(), "n_days": len(days_rows), "summary": summarize(days_rows),
           "per_day": {d: {"spy_med": (round(float(np.median([x["spy"] for x in r])), 2) if r else None),
                           "base": sum(x["base"] for x in r), "extra": sum(x["rel"] and not x["base"] for x in r)}
                       for d, r in days_rows.items()}}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res["summary"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
