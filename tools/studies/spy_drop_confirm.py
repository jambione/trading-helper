#!/usr/bin/env python3
"""spy_drop_confirm - on the long minute-bar signal sample, do buys made while SPY fell over the prior 5 minutes do worse?
Pass bar: docs/studies/spy_drop_confirm_prereg.json. Read-only, warehouse only.

    .venv/bin/python tools/studies/spy_drop_confirm.py     # -> ai_reports/spy_drop_confirm/result.json
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
from evening_family import WH_SIP, ALL_ETF  # noqa: E402
import news_source as ns  # noqa: E402
from entry_context_skip import spy_drop_bp  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "spy_drop_confirm"
X, INFO_X = 5.0, (10.0, 15.0)


def book(rows, x):
    """rows [{day, net, spy5}] -> book delta stats for skipping spy5 <= -x."""
    per_day = defaultdict(list)
    for r in rows:
        s = r["spy5"] is not None and r["spy5"] <= -x
        per_day[r["day"]].append(-r["net"] if s else 0.0)
    means = [np.mean(v) for v in per_day.values()]
    n = sum(len(v) for v in per_day.values())
    sd = float(np.std(means, ddof=1)) if len(means) > 2 else 0.0
    sk = [r for r in rows if r["spy5"] is not None and r["spy5"] <= -x]
    kp = [r for r in rows if not (r["spy5"] is not None and r["spy5"] <= -x)]
    f = lambda xs, k: round(float(np.mean([r[k] for r in xs])), 2) if xs else None
    return {"signals": n, "skipped": len(sk), "skipped_share": round(len(sk) / max(1, n), 3),
            "book_delta_bp": round(float(sum(sum(v) for v in per_day.values()) / max(1, n)), 2),
            "t_day": round(float(np.mean(means) / (sd / math.sqrt(len(means)))), 2) if sd > 0 else None,
            "all_mean": f(rows, "net"), "kept_mean": f(kp, "net"), "skipped_mean": f(sk, "net"),
            "kept_straight_down": f(kp, "down"), "skipped_straight_down": f(sk, "down"),
            "kept_win": round(float(np.mean([r["net"] > 0 for r in kp])) * 100, 1) if kp else None,
            "skipped_win": round(float(np.mean([r["net"] > 0 for r in sk])) * 100, 1) if sk else None}


def main():
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    rows, prev = [], None
    for f in files:
        day = load_day(f)
        if prev is not None:
            si, psi = day["idx"].get("SPY"), prev["idx"].get("SPY")
            if si is not None and psi is not None:
                spy_c, spy_pc = day["c"][si], ns.last_close(prev["c"][psi])
                spy_day = (ns.last_close(spy_c) / spy_pc - 1) * 100 if spy_pc > 0 else 0.0
                for s in universe(day, prev, set(ALL_ETF)):
                    i = day["idx"][s]
                    for x in ns.signals_for(day, prev, s, spy_c, spy_pc) or []:
                        if not x["base"]:
                            continue
                        t = x["t"]
                        o = day["o"][i][t + 1]
                        lo = day["l"][i][t + 1:t + 11]
                        down = bool(np.isfinite(o) and o > 0 and np.isfinite(lo).any() and (np.nanmin(lo) / o - 1) * 1e4 <= -40)
                        rows.append({"day": f.stem, "net": x["net"], "spy5": spy_drop_bp(spy_c, t + 1),
                                     "down": float(down), "red": spy_day < 0})
                print(f.stem, len(rows), flush=True)
        prev = day
    days = sorted({r["day"] for r in rows})
    half = set(days[: len(days) // 2])
    main_ = book(rows, X)
    h1 = book([r for r in rows if r["day"] in half], X)
    h2 = book([r for r in rows if r["day"] not in half], X)
    fails = []
    if main_["skipped"] < 300:
        v, fails = "UNDERPOWERED", [f"skipped {main_['skipped']} (need 300)"]
    else:
        if main_["book_delta_bp"] < 1.0:
            fails.append(f"book: {main_['book_delta_bp']} (need +1.0)")
        if (main_["t_day"] or 0) < 2.0:
            fails.append(f"significance: t {main_['t_day']} (need 2.0)")
        if h1["book_delta_bp"] <= 0 or h2["book_delta_bp"] <= 0:
            fails.append(f"both_halves: {h1['book_delta_bp']} / {h2['book_delta_bp']}")
        if main_["kept_mean"] is None or main_["kept_mean"] <= main_["all_mean"]:
            fails.append(f"kept_quality: kept {main_['kept_mean']} vs all {main_['all_mean']}")
        v = "PASS" if not fails else "FAIL"
    res = {"verdict": v, "fails": fails, "days": len(days), "X": X, "main": main_, "half1": h1, "half2": h2,
           "info_other_X": {str(x): book(rows, x) for x in INFO_X},
           "info_red_days": book([r for r in rows if r["red"]], X), "info_green_days": book([r for r in rows if not r["red"]], X),
           "generated": datetime.now(ET).isoformat(timespec="minutes")}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
