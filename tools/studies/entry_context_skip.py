#!/usr/bin/env python3
"""entry_context_skip - would skipping buys made while SPY is falling, or bunched right after other buys, avoid the trades that
fall straight to the stop? Pass bar: docs/studies/entry_context_skip_prereg.json. Read-only.

    .venv/bin/python tools/studies/entry_context_skip.py     # -> ai_reports/entry_context_skip/result.json
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
from relative_strength_scan import load_day  # noqa: E402
from evening_family import WH_SIP  # noqa: E402
from flow_exit import load_trades, TRAIN_END, TEST_START  # noqa: E402
import loser_breakdown as lb  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "entry_context_skip"
XS = (5.0, 10.0, 15.0)
MKS = ((2, 1), (5, 1), (5, 2))


def spy_drop_bp(spy_c, m):
    """SPY return over the 5 complete minutes before entry minute m (close m-6 -> close m-1), bp; None if unknown."""
    if m < 6:
        return None
    a, b = spy_c[m - 6], spy_c[m - 1]
    if not (np.isfinite(a) and np.isfinite(b)) or a <= 0:
        return None
    return (b / a - 1) * 1e4


def prior_buys(trades_day, t, minutes):
    """Other-symbol buys filled in the `minutes` before trade t."""
    return sum(1 for o in trades_day if o is not t and o["symbol"] != t["symbol"]
               and t["t_in"] - minutes * 60 <= o["t_in"] < t["t_in"])


def score(trades, skip):
    per_day, skipped = defaultdict(list), []
    for t in trades:
        s = skip(t)
        per_day[t["day"]].append(-t["ret_bp"] if s else 0.0)
        if s:
            skipped.append(t)
    means = [np.mean(v) for v in per_day.values()]
    n = sum(len(v) for v in per_day.values())
    book = float(sum(sum(v) for v in per_day.values()) / max(1, n))
    sd = float(np.std(means, ddof=1)) if len(means) > 2 else 0.0
    kept = [t["ret_bp"] for t in trades if not skip(t)]
    return {"trades": n, "skipped": len(skipped), "book_delta_bp": round(book, 2),
            "t_day": round(float(np.mean(means) / (sd / math.sqrt(len(means)))), 2) if sd > 0 else None,
            "all_mean_bp": round(float(np.mean([t["ret_bp"] for t in trades])), 2) if trades else None,
            "kept_mean_bp": round(float(np.mean(kept)), 2) if kept else None,
            "skipped_mean_bp": round(float(np.mean([t["ret_bp"] for t in skipped])), 2) if skipped else None,
            "skipped_stopout_share": round(float(np.mean([t["ret_bp"] <= -40 for t in skipped])), 3) if skipped else None,
            "kept_stopout_share": round(float(np.mean([r <= -40 for r in kept])), 3) if kept else None,
            "skipped_per_session": round(len(skipped) / max(1, len(per_day)), 2)}


def verdict(test, train_best):
    if test["skipped"] < 40:
        return "UNDERPOWERED", [f"test skips {test['skipped']} (need 40)"]
    f = []
    if test["book_delta_bp"] < 1.0:
        f.append(f"book: {test['book_delta_bp']} bp/trade (need +1.0)")
    if (test["t_day"] or 0) < 2.0:
        f.append(f"significance: day t {test['t_day']} (need 2.0)")
    if train_best <= 0:
        f.append(f"both_halves: train {train_best}")
    if test["kept_mean_bp"] is None or test["kept_mean_bp"] <= test["all_mean_bp"]:
        f.append(f"kept_quality: kept {test['kept_mean_bp']} vs all {test['all_mean_bp']}")
    return ("PASS" if not f else "FAIL"), f


def main():
    trades = load_trades()
    spy = {}
    for d in sorted({t["day"] for t in trades}):
        p = WH_SIP / f"{d}.npz"
        if p.exists():
            day = load_day(p)
            i = day["idx"].get("SPY")
            if i is not None:
                spy[d] = day["c"][i]
    trades = [t for t in trades if t["day"] in spy]
    by_day = defaultdict(list)
    for t in trades:
        t["m"] = lb.minute_of(t["t_in"], t["day"])
        t["spy5"] = spy_drop_bp(spy[t["day"]], t["m"])
        by_day[t["day"]].append(t)
    for t in trades:
        t["prior2"], t["prior5"] = prior_buys(by_day[t["day"]], t, 2), prior_buys(by_day[t["day"]], t, 5)
    train = [t for t in trades if t["day"] <= TRAIN_END]
    test = [t for t in trades if t["day"] >= TEST_START]

    def drop_rule(x):
        return lambda t: t["spy5"] is not None and t["spy5"] <= -x

    def bunch_rule(mk):
        m, k = mk
        return lambda t: t[f"prior{m}"] >= k

    out = {}
    for name, grid, mk_rule in (("MARKET_DROP", XS, drop_rule), ("BUNCHED", MKS, bunch_rule)):
        tg = [{"setting": g, **score(train, mk_rule(g))} for g in grid]
        best = max(tg, key=lambda r: r["book_delta_bp"])
        te = score(test, mk_rule(best["setting"]))
        v, f = verdict(te, best["book_delta_bp"])
        out[name] = {"verdict": v, "fails": f, "chosen": best["setting"], "train_grid": tg, "test": te}
    both = lambda t: drop_rule(out["MARKET_DROP"]["chosen"])(t) or bunch_rule(tuple(out["BUNCHED"]["chosen"]))(t)
    res = {**out, "info_both_test": score(test, both), "n_train": len(train), "n_test": len(test),
           "days": len(spy), "generated": datetime.now(ET).isoformat(timespec="minutes")}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1, default=list))
    print(json.dumps(res, indent=1, default=list))


if __name__ == "__main__":
    main()
