#!/usr/bin/env python3
"""trend_confidence_exit - operator 11/18: hold while the trade trends up or curls up; exit when it has never been above entry, is
below it now, and is going sideways or down. Pass bar: docs/studies/trend_confidence_exit_prereg.json. Read-only; data-only keys.

    .venv/bin/python tools/studies/trend_confidence_exit.py     # -> ai_reports/trend_confidence_exit/result.json
"""
from __future__ import annotations

import json
import math
import pickle
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import loser_breakdown as lb  # noqa: E402
from flow_exit import load_trades, TRAIN_END, TEST_START  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "trend_confidence_exit"
TS, EXIT_COST = (3, 5, 10), 3.0


def trend_up_or_curl(c, lo):
    """c, lo: the last 5 closes / lows (oldest first). Up-slope or a curl (higher close and a higher low) = hold."""
    c, lo = np.asarray(c, float), np.asarray(lo, float)
    if len(c) < 5 or not np.isfinite(c).all() or not np.isfinite(lo).all():
        return True                     # unknown shape: do not act
    slope = np.polyfit(np.arange(5), c, 1)[0]
    curl = c[-1] > c[-2] and lo[-1] > lo[:-1].min()
    return bool(slope > 0 or curl)


def decide(tr, h, lo, c, m_in, T, use_trend=True):
    """bp delta vs the actual exit; 0 when the rule does not act. h/lo/c: the symbol's minute arrays for the day."""
    m_end = m_in + T
    if m_end >= 389:
        return 0.0
    t_end = datetime.fromisoformat(tr["day"] + "T09:30").replace(tzinfo=ET).timestamp() + (m_end + 1) * 60
    if tr["t_out"] <= t_end:
        return 0.0
    seg_h = h[m_in + 1:m_end + 1]
    if not np.isfinite(c[m_end]) or not np.isfinite(seg_h).any():
        return 0.0
    if np.nanmax(seg_h) > tr["px_in"] or not c[m_end] < tr["px_in"]:
        return 0.0
    if use_trend and trend_up_or_curl(c[m_end - 4:m_end + 1], lo[m_end - 4:m_end + 1]):
        return 0.0
    return (c[m_end] / tr["px_in"] - 1) * 1e4 - EXIT_COST - tr["ret_bp"]


def score(trades, bars, T, use_trend=True):
    per_day, cut = defaultdict(list), []
    for t in trades:
        b = bars.get(t["day"])
        i = b["idx"].get(t["symbol"]) if b else None
        if i is None:
            continue
        m_in = lb.minute_of(t["t_in"], t["day"])
        if not (0 <= m_in < 389):
            continue
        d = decide(t, b["h"][i], b["l"][i], b["c"][i], m_in, T, use_trend)
        per_day[t["day"]].append(d)
        if d != 0.0:
            cut.append((t, d))
    means = [np.mean(v) for v in per_day.values()]
    n = sum(len(v) for v in per_day.values())
    book = float(sum(sum(v) for v in per_day.values()) / max(1, n))
    sd = float(np.std(means, ddof=1)) if len(means) > 2 else 0.0
    return {"T": T, "trend": use_trend, "trades": n, "cut": len(cut), "book_delta_bp": round(book, 2),
            "t_day": round(float(np.mean(means) / (sd / math.sqrt(len(means)))), 2) if sd > 0 else None,
            "cut_stopouts": sum(1 for t, _ in cut if t["ret_bp"] <= -40), "cut_winners": sum(1 for t, _ in cut if t["ret_bp"] > 0),
            "mean_delta_per_cut": round(float(np.mean([x for _, x in cut])), 1) if cut else None}


def main():
    trades = load_trades()
    OUT.mkdir(parents=True, exist_ok=True)
    cache_p = OUT / "bars.pkl"
    bars = pickle.load(open(cache_p, "rb")) if cache_p.exists() else {}
    by_day = defaultdict(set)
    for t in trades:
        by_day[t["day"]].add(t["symbol"])
    for d, syms in sorted(by_day.items()):
        if d in bars:
            continue
        try:
            bars[d] = lb.api_bars(d, syms)
        except Exception as e:  # noqa: BLE001
            print("bars failed", d, str(e)[:80], flush=True)
            continue
        pickle.dump(bars, open(cache_p, "wb"))
        print(d, len(syms), "symbols", flush=True)
    train = [t for t in trades if t["day"] <= TRAIN_END]
    test = [t for t in trades if t["day"] >= TEST_START]
    grid = [score(train, bars, T) for T in TS]
    best = max(grid, key=lambda g: g["book_delta_bp"])
    T = best["T"]
    rule_t, plain_t = score(test, bars, T), score(test, bars, T, use_trend=False)
    fails = []
    if rule_t["cut"] < 40:
        verdict, fails = "UNDERPOWERED", [f"test exits {rule_t['cut']} (need 40)"]
    else:
        if rule_t["book_delta_bp"] < 1.0:
            fails.append(f"book: {rule_t['book_delta_bp']} bp/trade (need +1.0)")
        if (rule_t["t_day"] or 0) < 2.0:
            fails.append(f"significance: day t {rule_t['t_day']} (need 2.0)")
        if rule_t["book_delta_bp"] < plain_t["book_delta_bp"] + 1.0:
            fails.append(f"beats_plain: rule {rule_t['book_delta_bp']} vs plain {plain_t['book_delta_bp']}")
        if best["book_delta_bp"] <= 0:
            fails.append(f"both_halves: train {best['book_delta_bp']}")
        verdict = "PASS" if not fails else "FAIL"
    res = {"verdict": verdict, "fails": fails, "chosen_T": T, "train_grid": grid,
           "train_plain": [score(train, bars, x, use_trend=False) for x in TS],
           "test_rule": rule_t, "test_plain": plain_t, "n_train": len(train), "n_test": len(test),
           "generated": datetime.now(ET).isoformat(timespec="minutes")}
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
