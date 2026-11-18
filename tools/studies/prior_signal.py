#!/usr/bin/env python3
"""prior_signal - does the outcome of a name's earlier pre-square signal today predict its next one?
Pass bar: docs/studies/prior_signal_prereg.json. Read-only, warehouse only.

    .venv/bin/python tools/studies/prior_signal.py     # -> ai_reports/prior_signal/result.json
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

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "prior_signal"


def classify(sigs):
    """sigs: one name's signals today [{t, net, base, down}] sorted by t -> rows for the BASE ones with group/prev info."""
    out = []
    for k, x in enumerate(sigs):
        if not x["base"]:
            continue
        prev = [p for p in sigs[:k] if p["t"] + 10 < x["t"]]
        if not prev:
            g, pm = "NONE", None
        else:
            g = "WIN" if prev[-1]["net"] > 0 else "LOSS"
            pm = float(np.mean([p["net"] for p in prev]))
        out.append({"t": x["t"], "net": x["net"], "down": x["down"], "group": g, "prev_mean": pm})
    return out


def stats(rows):
    days = defaultdict(lambda: {"WIN": [], "LOSS": []})
    for r in rows:
        if r["group"] in ("WIN", "LOSS"):
            days[r["day"]][r["group"]].append(r["net"])
    diffs = [np.mean(v["WIN"]) - np.mean(v["LOSS"]) for v in days.values() if v["WIN"] and v["LOSS"]]
    sd = float(np.std(diffs, ddof=1)) if len(diffs) > 2 else 0.0
    g = lambda name: [r for r in rows if r["group"] == name]
    m = lambda xs, k="net": round(float(np.mean([r[k] for r in xs])), 2) if xs else None
    return {"days_with_both": len(diffs), "win_minus_loss_bp": round(float(np.mean(diffs)), 2) if diffs else None,
            "t_day": round(float(np.mean(diffs) / (sd / math.sqrt(len(diffs)))), 2) if sd > 0 else None,
            "n": {k: len(g(k)) for k in ("WIN", "LOSS", "NONE")},
            "mean": {k: m(g(k)) for k in ("WIN", "LOSS", "NONE")}, "all_mean": m(rows),
            "win_pct": {k: round(float(np.mean([r["net"] > 0 for r in g(k)])) * 100, 1) if g(k) else None for k in ("WIN", "LOSS", "NONE")},
            "straight_down": {k: m(g(k), "down") for k in ("WIN", "LOSS", "NONE")}}


def main():
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    rows, prev = [], None
    for f in files:
        day = load_day(f)
        if prev is not None:
            si, psi = day["idx"].get("SPY"), prev["idx"].get("SPY")
            if si is not None and psi is not None:
                spy_c, spy_pc = day["c"][si], ns.last_close(prev["c"][psi])
                for s in universe(day, prev, set(ALL_ETF)):
                    i = day["idx"][s]
                    sigs = []
                    for x in ns.signals_for(day, prev, s, spy_c, spy_pc) or []:
                        t = x["t"]
                        o = day["o"][i][t + 1]
                        lo = day["l"][i][t + 1:t + 11]
                        down = float(bool(np.isfinite(o) and o > 0 and np.isfinite(lo).any()
                                          and (np.nanmin(lo) / o - 1) * 1e4 <= -40))
                        sigs.append({"t": t, "net": x["net"], "base": x["base"], "down": down})
                    for r in classify(sorted(sigs, key=lambda z: z["t"])):
                        rows.append({**r, "day": f.stem})
                print(f.stem, len(rows), flush=True)
        prev = day
    days = sorted({r["day"] for r in rows})
    half = set(days[: len(days) // 2])
    allr, h1, h2 = stats(rows), stats([r for r in rows if r["day"] in half]), stats([r for r in rows if r["day"] not in half])
    fails = []
    if allr["n"]["WIN"] < 1000 or allr["n"]["LOSS"] < 1000:
        v, fails = "UNDERPOWERED", [f"WIN {allr['n']['WIN']} / LOSS {allr['n']['LOSS']} (need 1,000 each)"]
    else:
        if (allr["win_minus_loss_bp"] or 0) < 3.0:
            fails.append(f"size: {allr['win_minus_loss_bp']} bp (need +3.0)")
        if (allr["t_day"] or 0) < 2.0:
            fails.append(f"significance: t {allr['t_day']} (need 2.0)")
        if (h1["win_minus_loss_bp"] or 0) <= 0 or (h2["win_minus_loss_bp"] or 0) <= 0:
            fails.append(f"both_halves: {h1['win_minus_loss_bp']} / {h2['win_minus_loss_bp']}")
        if allr["mean"]["WIN"] is None or allr["mean"]["WIN"] <= allr["all_mean"]:
            fails.append(f"win_beats_all: WIN {allr['mean']['WIN']} vs all {allr['all_mean']}")
        v = "PASS" if not fails else "FAIL"
    buckets = {}
    for lo_, hi_, lab in ((0, 60, "09:30-10:30"), (60, 180, "10:30-12:30"), (180, 390, "12:30-close")):
        sub = [r for r in rows if lo_ <= r["t"] < hi_]
        buckets[lab] = {k: (len([r for r in sub if r["group"] == k]),
                            round(float(np.mean([r["net"] for r in sub if r["group"] == k])), 2) if any(r["group"] == k for r in sub) else None)
                        for k in ("NONE", "WIN", "LOSS")}
    res = {"verdict": v, "fails": fails, "days": len(days), "all": allr, "half1": h1, "half2": h2,
           "info_by_time": buckets, "generated": datetime.now(ET).isoformat(timespec="minutes")}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
