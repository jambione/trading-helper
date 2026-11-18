#!/usr/bin/env python3
"""repeat_name - does the desk's first trade of the day in a name do worse than its later trades in it, by time of day?
Pass bar: docs/studies/repeat_name_prereg.json. Read-only, broker fills only.

    .venv/bin/python tools/studies/repeat_name.py     # -> ai_reports/repeat_name/result.json
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
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from flow_exit import load_trades, TRAIN_END, TEST_START  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "repeat_name"
BUCKETS = ((0, 60), (60, 180), (180, 390))


def label(trades):
    """Adds first (bool) and bucket to each trade; first = no earlier CLOSED trade in that symbol that day at the buy."""
    by = defaultdict(list)
    for t in sorted(trades, key=lambda x: x["t_in"]):
        prior = [o for o in by[(t["day"], t["symbol"])] if o["t_out"] <= t["t_in"]]
        t["first"] = not prior
        m = datetime.fromtimestamp(t["t_in"], ET)
        mins = m.hour * 60 + m.minute - 570
        t["bucket"] = next((i for i, (a, b) in enumerate(BUCKETS) if a <= mins < b), 2)
        by[(t["day"], t["symbol"])].append(t)
    return trades


def diff_stats(trades):
    per_day = defaultdict(list)
    nf = nr = 0
    days = defaultdict(list)
    for t in trades:
        days[t["day"]].append(t)
    for d, ts in days.items():
        ds = []
        for b in range(len(BUCKETS)):
            f = [t["ret_bp"] for t in ts if t["bucket"] == b and t["first"]]
            r = [t["ret_bp"] for t in ts if t["bucket"] == b and not t["first"]]
            if f and r:
                ds.append(np.mean(r) - np.mean(f))
                nf += len(f)
                nr += len(r)
        if ds:
            per_day[d] = float(np.mean(ds))
    v = list(per_day.values())
    sd = float(np.std(v, ddof=1)) if len(v) > 2 else 0.0
    return {"days": len(v), "first_n": nf, "repeat_n": nr, "diff_bp": round(float(np.mean(v)), 2) if v else None,
            "t_day": round(float(np.mean(v) / (sd / math.sqrt(len(v)))), 2) if sd > 0 else None}


def half_size_dollars(trades):
    """Actual vs half-size-first dollars per day."""
    act, rule = defaultdict(float), defaultdict(float)
    for t in trades:
        usd = (t["px_out"] - t["px_in"]) * t["qty"]
        act[t["day"]] += usd
        rule[t["day"]] += usd * (0.5 if t["first"] else 1.0)
    n = max(1, len(act))
    return {"actual_usd_per_day": round(sum(act.values()) / n, 2), "half_first_usd_per_day": round(sum(rule.values()) / n, 2)}


def main():
    trades = label(load_trades())
    train = [t for t in trades if t["day"] <= TRAIN_END]
    test = [t for t in trades if t["day"] >= TEST_START]
    tr, te = diff_stats(train), diff_stats(test)
    hs = half_size_dollars(test)
    fails = []
    if te["first_n"] < 100 or te["repeat_n"] < 100:
        v, fails = "UNDERPOWERED", [f"test first {te['first_n']} / repeat {te['repeat_n']} (need 100 each)"]
    else:
        if (te["diff_bp"] or 0) < 3.0:
            fails.append(f"test_size: {te['diff_bp']} bp (need +3.0)")
        if (te["t_day"] or 0) < 2.0:
            fails.append(f"test_significance: t {te['t_day']} (need 2.0)")
        if (tr["diff_bp"] or 0) <= 0:
            fails.append(f"both_halves: train {tr['diff_bp']}")
        if hs["half_first_usd_per_day"] <= hs["actual_usd_per_day"]:
            fails.append(f"rule_book: {hs}")
        v = "PASS" if not fails else "FAIL"
    mean = lambda xs: round(float(np.mean(xs)), 2) if xs else None
    res = {"verdict": v, "fails": fails, "train": tr, "test": te, "test_half_size": hs,
           "info_test_means": {"first": mean([t["ret_bp"] for t in test if t["first"]]),
                               "repeat": mean([t["ret_bp"] for t in test if not t["first"]])},
           "generated": datetime.now(ET).isoformat(timespec="minutes")}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
