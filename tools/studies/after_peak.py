#!/usr/bin/env python3
"""after_peak - are pre-square buys made just after a fresh peak, with the slow %R rolling over, worse than other buys?
Pass bar: docs/studies/after_peak_prereg.json. Read-only, warehouse only.

    .venv/bin/python tools/studies/after_peak.py     # -> ai_reports/after_peak/result.json
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
from evening_family import WH_SIP, ALL_ETF, minute_grid, presquare_bars, first_true, trade  # noqa: E402
from bar_signal_family import wr_smoothed  # noqa: E402
import news_source as ns  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "after_peak"


def flags(hh, cc, slow, i):
    """(peak_lower, slow_roll) at grid index i. Pure."""
    if i < 10:
        return False, False
    win = hh[i - 10:i]
    if not np.isfinite(win).any() or not np.isfinite(cc[i]):
        return False, False
    p = int(np.nanargmax(win)) + (i - 10)
    ago = i - p
    peak_lower = bool(2 <= ago <= 8 and (cc[i] / hh[p] - 1) * 1e4 <= -10)
    s = slow
    roll = bool(np.isfinite([s[i], s[i - 2], s[i - 3], s[i - 8]]).all() and s[i] - s[i - 2] <= 0 and s[i - 3] - s[i - 8] > 0)
    return peak_lower, roll


def compare(rows, key):
    days = defaultdict(lambda: {"f": [], "o": []})
    for r in rows:
        days[r["day"]]["f" if r[key] else "o"].append(r["net"])
    diffs = [np.mean(v["f"]) - np.mean(v["o"]) for v in days.values() if v["f"] and v["o"]]
    sd = float(np.std(diffs, ddof=1)) if len(diffs) > 2 else 0.0
    f = [r for r in rows if r[key]]
    o = [r for r in rows if not r[key]]
    m = lambda xs, k="net": round(float(np.mean([x[k] for x in xs])), 2) if xs else None
    return {"flagged": len(f), "share": round(len(f) / max(1, len(rows)), 3), "flag_mean": m(f), "other_mean": m(o),
            "all_mean": m(rows), "diff_daymean": round(float(np.mean(diffs)), 2) if diffs else None,
            "t_day": round(float(np.mean(diffs) / (sd / math.sqrt(len(diffs)))), 2) if sd > 0 else None,
            "flag_win": round(float(np.mean([x["net"] > 0 for x in f])) * 100, 1) if f else None,
            "other_win": round(float(np.mean([x["net"] > 0 for x in o])) * 100, 1) if o else None,
            "flag_down": m(f, "down"), "other_down": m(o, "down")}


def main():
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    rows, prev = [], None
    for fp in files:
        day = load_day(fp)
        if prev is not None and day["idx"].get("SPY") is not None and prev["idx"].get("SPY") is not None:
            for s in universe(day, prev, set(ALL_ETF)):
                i, j = day["idx"][s], prev["idx"].get(s)
                if j is None:
                    continue
                pc = ns.last_close(prev["c"][j])
                if not pc > 0:
                    continue
                cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], day["c"][i], day["h"][i], day["l"][i])
                slow = wr_smoothed(hh, ll, cc, n=112, span=3)
                for t in first_true(presquare_bars(cc, hh, ll, off, 21)):
                    px = day["c"][i][t]
                    if np.isnan(px) or not (20.0 <= px <= 400.0) or (px / pc - 1) * 100 < 1.5:
                        continue
                    net = trade(day["o"][i], day["c"][i], t, 10, 6.0)
                    if net is None:
                        continue
                    pl, roll = flags(hh, cc, slow, t + off)
                    o = day["o"][i][t + 1]
                    lo = day["l"][i][t + 1:t + 11]
                    down = float(bool(np.isfinite(o) and o > 0 and np.isfinite(lo).any() and (np.nanmin(lo) / o - 1) * 1e4 <= -40))
                    rows.append({"day": fp.stem, "net": float(net), "down": down, "pl": pl, "roll": roll, "ap": pl and roll})
            print(fp.stem, len(rows), sum(r["ap"] for r in rows), flush=True)
        prev = day
    days = sorted({r["day"] for r in rows})
    half = set(days[: len(days) // 2])
    main_ = compare(rows, "ap")
    h1, h2 = compare([r for r in rows if r["day"] in half], "ap"), compare([r for r in rows if r["day"] not in half], "ap")
    kept = [r["net"] for r in rows if not r["ap"]]
    fails = []
    if main_["flagged"] < 500:
        v, fails = "UNDERPOWERED", [f"flagged {main_['flagged']} (need 500)"]
    else:
        if (main_["diff_daymean"] or 0) > -3.0:
            fails.append(f"size: {main_['diff_daymean']} bp (need <= -3.0)")
        if (main_["t_day"] or 0) > -2.0:
            fails.append(f"significance: t {main_['t_day']} (need <= -2.0)")
        if (h1["diff_daymean"] or 0) >= 0 or (h2["diff_daymean"] or 0) >= 0:
            fails.append(f"both_halves: {h1['diff_daymean']} / {h2['diff_daymean']}")
        if not kept or np.mean(kept) <= main_["all_mean"]:
            fails.append("kept_quality")
        v = "PASS" if not fails else "FAIL"
    res = {"verdict": v, "fails": fails, "days": len(days), "signals": len(rows), "after_peak": main_, "half1": h1, "half2": h2,
           "info_peak_lower_only": compare(rows, "pl"), "info_slow_roll_only": compare(rows, "roll"),
           "generated": datetime.now(ET).isoformat(timespec="minutes")}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
