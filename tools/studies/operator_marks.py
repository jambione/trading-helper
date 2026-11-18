#!/usr/bin/env python3
"""operator_marks - the operator's chart signals (fast %R curl, RSI-2 oversold hook, deep %R curl, and both together) scored on the
desk's +20/-50 bp bracket. Pass bar: docs/studies/operator_marks_prereg.json. Read-only, warehouse only.

    .venv/bin/python tools/studies/operator_marks.py     # -> ai_reports/operator_marks/result.json
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day  # noqa: E402
from board_structure_family import universe  # noqa: E402
from evening_family import WH_SIP, ALL_ETF, minute_grid  # noqa: E402
from bar_signal_family import wr_smoothed  # noqa: E402
from holistic_entry import bracket  # noqa: E402
import news_source as ns  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "operator_marks"
RULES = ("EARLY_CURL", "RSI2_HOOK", "DEEP_CURL", "BOTH")
REFRACTORY, T_FIRST, T_LAST = 30, 15, 360


def rsi2(c):
    """Wilder RSI, period 2, on closes (NaN-tolerant: carries the last value over gaps)."""
    out = np.full(len(c), np.nan)
    ag = al = None
    prev = None
    for i, x in enumerate(c):
        if not np.isfinite(x):
            out[i] = out[i - 1] if i else np.nan
            continue
        if prev is not None:
            d = x - prev
            g, lo = max(d, 0.0), max(-d, 0.0)
            ag = g if ag is None else (ag + g) / 2
            al = lo if al is None else (al + lo) / 2
            out[i] = 100.0 if al == 0 and ag > 0 else (50.0 if al == 0 else 100 - 100 / (1 + ag / al))
        prev = x
    return out


def fire(fast, slow, r2, i):
    """{rule: bool} at grid index i. Pure."""
    f, fp, s = fast[i], fast[i - 1], slow[i]
    res = dict.fromkeys(RULES, False)
    if not (np.isfinite(f) and np.isfinite(fp)):
        return res
    res["EARLY_CURL"] = bool(np.isfinite(s) and f <= -50 and s >= -35 and f - fp > 0.05)
    hook = lambda k: bool(np.isfinite(r2[k]) and np.isfinite(r2[k - 1]) and r2[k - 1] < 10 and r2[k] > r2[k - 1])
    res["RSI2_HOOK"] = hook(i)
    w = fast[i - 5:i + 1]
    res["DEEP_CURL"] = bool(np.isfinite(w).any() and np.nanmin(w) <= -70 and fast[i] > fast[i - 1] > fast[i - 2])
    res["BOTH"] = res["DEEP_CURL"] and any(hook(k) for k in range(i - 4, i + 1))
    return res


def summarize(rows):
    out = {}
    days_all = sorted({r["day"] for r in rows})
    half = set(days_all[: len(days_all) // 2])
    for rule in RULES:
        xs = [r for r in rows if r["rule"] == rule]
        if not xs:
            out[rule] = {"n": 0}
            continue
        per = defaultdict(list)
        for r in xs:
            per[r["day"]].append(r["net"])
        m = [np.mean(v) for v in per.values()]
        sd = float(np.std(m, ddof=1)) if len(m) > 2 else 0.0
        h1 = [r["net"] for r in xs if r["day"] in half]
        h2 = [r["net"] for r in xs if r["day"] not in half]
        out[rule] = {"n": len(xs), "mean_net": round(float(np.mean([r["net"] for r in xs])), 2),
                     "win_pct": round(float(np.mean([r["net"] > 0 for r in xs])) * 100, 1),
                     "t_day": round(float(np.mean(m) / (sd / math.sqrt(len(m)))), 2) if sd > 0 else None,
                     "half1": round(float(np.mean(h1)), 2) if h1 else None, "half2": round(float(np.mean(h2)), 2) if h2 else None,
                     "per_10m": round(len(xs) / max(1, len(days_all)) / 346 * 10, 2)}
    return out


def main():
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    rows, rows_any, prev = [], [], None
    for fp in files:
        day = load_day(fp)
        if prev is not None and day["idx"].get("SPY") is not None:
            for s in universe(day, prev, set(ALL_ETF)):
                i, j = day["idx"][s], prev["idx"].get(s)
                if j is None:
                    continue
                pc = ns.last_close(prev["c"][j])
                if not pc > 0:
                    continue
                o, h, l, c = (day[k][i] for k in "ohlc")
                cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], c, h, l)
                fast = wr_smoothed(hh, ll, cc, n=21, span=7)
                slow = wr_smoothed(hh, ll, cc, n=112, span=3)
                r2 = rsi2(cc)
                last = dict.fromkeys(RULES, -999)
                for t in range(T_FIRST, T_LAST + 1):
                    g = t + off
                    fr = fire(fast, slow, r2, g)
                    for rule, ok in fr.items():
                        if not ok or t - last[rule] < REFRACTORY:
                            continue
                        last[rule] = t
                        net = bracket(o, h, l, c, t)
                        if net is None or not np.isfinite(c[t]):
                            continue
                        rec = {"day": fp.stem, "rule": rule, "net": float(net)}
                        rows_any.append(rec)
                        if 20.0 <= c[t] <= 400.0 and (c[t] / pc - 1) * 100 >= 1.5:
                            rows.append(rec)
            print(fp.stem, len(rows), len(rows_any), flush=True)
        prev = day
    main_ = summarize(rows)
    verdicts = {}
    for rule, r in main_.items():
        f = []
        if r.get("n", 0) < 500:
            verdicts[rule] = ("UNDERPOWERED", [f"n {r.get('n', 0)} (need 500)"])
            continue
        if r["mean_net"] < 2.0:
            f.append(f"edge: {r['mean_net']} bp (need +2.0)")
        if (r["t_day"] or 0) < 2.0:
            f.append(f"significance: t {r['t_day']}")
        if not ((r["half1"] or -1) > 0 and (r["half2"] or -1) > 0):
            f.append(f"both_halves: {r['half1']} / {r['half2']}")
        verdicts[rule] = ("PASS" if not f else "FAIL", f)
    res = {"verdicts": verdicts, "admission_band": main_, "info_no_band": summarize(rows_any),
           "compare": {"avg_admissible_moment": -6.14, "desk_presquare": -6.48},
           "days": len({r["day"] for r in rows}), "generated": datetime.now(ET).isoformat(timespec="minutes")}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
