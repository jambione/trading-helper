#!/usr/bin/env python3
"""Apply docs/studies/midpoint_policy_prereg.json (amended_1) to scorer outputs.

    python3 tools/studies/midpoint_policy_verdict.py CALIB.json HELDOUT.json

1. Calibration picks the fill rule: of trade_through / bid_cross, the one whose
   predicted fill rate on the desk's real SELL_LIMIT orders is closest to the actual
   rate. If neither is within 10 points, the study is VOID (the model cannot
   represent paper fills).
2. PRIMARY: that rule at the 10 s collar, eligible exits only (no fallback after a
   SELL_LIMIT, paper fill lag <= 10 s). t-statistics are DAY-CLUSTERED (over the
   per-session mean deltas). The 5 s collar and the other rule are SECONDARY.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.studies.shadow_loggers_score import eligible  # noqa: E402

H1 = ("2026-09-16", "2026-10-02")
H2 = ("2026-10-05", "2026-10-23")
CALIB_TOL = 0.10
CALIB_MIN_KAPPA = 0.30
UNTOUCHED = ("2026-09-18", "2026-09-28")  # the only block no midpoint study has scored


def _t(v):
    if len(v) < 3:
        return None
    sd = statistics.stdev(v)
    return statistics.mean(v) / (sd / math.sqrt(len(v))) if sd > 0 else None


def pick_rule(calib: dict) -> tuple[str | None, dict]:
    s = calib["summary"]
    act = s["actual_fill_rate"]
    gaps = {r: abs(s[r]["pred_fill_rate"] - act) for r in ("trade_through", "bid_cross")
            if s.get(r, {}).get("pred_fill_rate") is not None and act is not None}
    if not gaps:
        return None, {"actual": act, "gaps": gaps}
    rule = min(gaps, key=gaps.get)
    kappa = s[rule].get("kappa")
    ok = gaps[rule] <= CALIB_TOL and kappa is not None and kappa >= CALIB_MIN_KAPPA
    return (rule if ok else None), {"actual": act, "gaps": gaps, "picked": rule, "kappa": kappa}


def verdict(rows: list[dict], key: str) -> dict:
    pool = [r for r in rows if eligible(r)]
    sc = [r for r in pool if isinstance(r.get(key), dict)]
    missing = len(pool) - len(sc)
    passive = [r for r in sc if r[key]["route"] == "passive"]

    def day_means(v):
        days = sorted({r["day"] for r in v})
        return {d: statistics.mean(r[key]["delta_bp"] for r in v if r["day"] == d) for d in days}

    def block(lo, hi):
        v = [r for r in sc if lo <= r["day"] <= hi]
        dm = day_means(v)
        return {"n": len(v), "n_passive": sum(1 for r in v if r[key]["route"] == "passive"),
                "sessions": len(dm), "mean": statistics.mean(r[key]["delta_bp"] for r in v) if v else None,
                "t_day": _t(list(dm.values()))}

    h1, h2 = block(*H1), block(*H2)
    untouched = block(*UNTOUCHED)
    by_reason = {}
    for r in sc:
        by_reason.setdefault(r.get("reason") or "unknown", []).append(r[key]["delta_bp"])
    dm = day_means(sc)
    share_pos = (sum(1 for x in dm.values() if x > 0) / len(dm)) if dm else 0.0
    mean = statistics.mean(r[key]["delta_bp"] for r in sc) if sc else None
    t = _t(list(dm.values()))
    out = {"key": key, "n_pool": len(pool), "n_scored": len(sc), "n_passive": len(passive),
           "missing_share": (missing / len(pool)) if pool else None, "mean_bp": mean, "t_day": t,
           "half_1": h1, "half_2": h2, "untouched_9_18_9_28": untouched,
           "by_reason": {k: {"n": len(x), "mean": statistics.mean(x)} for k, x in sorted(by_reason.items())},
           "sessions": len(dm), "share_days_positive": share_pos,
           "delta2_mean_bp": statistics.mean(r[key]["delta2_bp"] for r in sc) if sc else None}
    halves_ok = all(h["n"] >= 100 and h["n_passive"] >= 25 for h in (h1, h2))
    if len(sc) < 300 or len(passive) < 50 or (pool and missing / len(pool) > 0.10) or not halves_ok:
        v = "VOID"
    elif mean is None or mean <= 0 or any(
            h["mean"] is not None and h["mean"] < 0 and h["t_day"] is not None and abs(h["t_day"]) >= 1
            for h in (h1, h2)):
        v = "FAIL"
    elif (t is not None and t >= 2.0 and share_pos >= 0.60
          and all(h["mean"] and h["mean"] > 0 and h["t_day"] is not None and h["t_day"] >= 1.0 for h in (h1, h2))
          and untouched["mean"] is not None and untouched["mean"] >= 0):
        v = "PASS"
    elif all(h["mean"] and h["mean"] > 0 for h in (h1, h2)):
        v = "PROMISING"
    else:
        v = "INCONCLUSIVE"
    out["verdict"] = v
    return out


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    calib = json.load(open(argv[0]))
    rows = json.load(open(argv[1]))["rows"]
    rule, info = pick_rule(calib)
    print("CALIBRATION", json.dumps(info, default=lambda x: round(x, 3) if isinstance(x, float) else x))
    if rule is None:
        print("PRIMARY VERDICT: VOID (no fill rule within 10 points of the real paper fill rate with kappa >= 0.3)")
        return 0
    prim = verdict(rows, f"p1_{rule}_c10")
    print("PRIMARY", json.dumps(prim, indent=2, default=lambda x: round(x, 3) if isinstance(x, float) else x))
    other = "bid_cross" if rule == "trade_through" else "trade_through"
    for k in (f"p1_{rule}_c5", f"p1_{other}_c10"):
        s = verdict(rows, k)
        print(f"SECONDARY (not a verdict) {k}: mean {s['mean_bp']} t_day {s['t_day']} "
              f"h1 {s['half_1']['mean']} h2 {s['half_2']['mean']} passive {s['n_passive']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
