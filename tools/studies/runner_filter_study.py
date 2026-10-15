#!/usr/bin/env python3
"""Runner entry filter: does a pre-registered entry trait separate the runner's winners from its losers?
Spec: docs/studies/runner_filter_prereg.json. Builds on right_tail_runner.py (FAIL 10/15: the tail is real, the
losers outweigh it).

Same trades and runner exit as right_tail_runner (first fill per name-day 9/08-10/13, 2% stop, 1% trail after +1%,
worse bar ordering, 5 bp + measured stop slippage). Each filter keeps the trades whose entry-time trait passes:
  F1 room   price at entry <= 97.26% of the day's SIP high so far (09:30 .. entry minute - 1)
  F2 price  entry price >= $50
  F3 early  day change at entry > +2.35% vs the prior close AND entry 09:30-10:46
Entry-time only: every input is from bars that closed before the entry minute, or the prior close.

  (default)  inventory: trait coverage and kept counts; no returns
  --score    after a result-skeptic READY
Cache only: early_entry_honest SIP bars, hist_universe raw daily cache (prior close), outcomes.jsonl.
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import early_entry_honest as E  # noqa: E402
import limit_below_study as L  # noqa: E402
import right_tail_runner as R  # noqa: E402

ROOM_MAX = 0.9726
PRICE_MIN = 50.0
DAY_CHG_MIN, EARLY_END = 0.0235, 10 * 60 + 46
RTH0 = 9 * 60 + 30
T_BAR = 2.4                 # three filters on the same trades
LIFT_BAR = 0.0010
MIN_KEEP_SHARE, MIN_KEEP_N = 0.15, 60      # pooled (amended before any return)
FILTERS = ("F1_room", "F2_price50", "F3_early_up")


# ── pure ─────────────────────────────────────────────────────────────────────

def hod_before(bars: dict, entry_min: int) -> float | None:
    hs = [b[1] for m, b in bars.items() if RTH0 <= m < entry_min]
    return max(hs) if hs else None


def traits(bars: dict, entry_min: int, entry_px: float, prior_close: float | None) -> dict:
    hod = hod_before(bars, entry_min)
    out = {"F1_room": None if hod is None else entry_px <= hod * ROOM_MAX,
           "F2_price50": entry_px >= PRICE_MIN,
           "F3_early_up": None}
    if prior_close:
        out["F3_early_up"] = (entry_px / prior_close - 1 > DAY_CHG_MIN) and (RTH0 <= entry_min <= EARLY_END)
    return out


# ── data ─────────────────────────────────────────────────────────────────────

def prior_closes() -> dict:
    """{(day, sym): prior session raw SIP close} from the hist_universe daily cache; missing = None."""
    p = os.path.join(E.ROOT, "ai_reports", "hist_universe", "cache", "daily_raw_sip.pkl")
    if not os.path.exists(p):
        return {}
    data = pickle.load(open(p, "rb"))["data"]
    return data


def prior_close_for(daily: dict, sym: str, day: str) -> float | None:
    rows = daily.get(sym) or []
    prev = [r for r in rows if r[0] < day]
    return float(prev[-1][2]) if prev else None


def score(datas: list[dict], stop_cost: float) -> tuple[dict, Counter]:
    by_day = {d["day"]: d for d in datas}
    first, _groups = R.trades(datas)
    daily = prior_closes()
    cov = Counter()
    rows = []
    for t in first:
        sipf = by_day[t["day"]]["sip"].get(t["sym"])
        if sipf is None or t["entry_min"] >= R.EOD_MIN:
            cov["skipped"] += 1
            continue
        bars = L.ohlc_map(sipf)
        ex = R.runner_exit(bars, t["entry_min"], t["entry_px"])
        if ex is None:
            cov["skipped"] += 1
            continue
        pc = prior_close_for(daily, t["sym"], t["day"])
        tr = traits(bars, t["entry_min"], t["entry_px"], pc)
        for k, v in tr.items():
            cov[f"{k}_{'known' if v is not None else 'unknown'}"] += 1
        cost = R.EXIT_COST + (stop_cost if ex[2] in ("stop", "trail") else 0.0)
        rows.append({"day": t["day"], "half": "A" if t["day"] <= L.SPLIT_END_A else "B",
                     "run": ex[0] / t["entry_px"] - 1 - cost, **tr})
    res = {}
    for f in FILTERS:
        for h in ("A", "B", "ALL"):
            allr = [r for r in rows if h == "ALL" or r["half"] == h]
            kept = [r for r in allr if r[f] is True]
            known = [r for r in allr if r[f] is not None]
            kb, ab = defaultdict(list), defaultdict(list)
            for r in kept:
                kb[r["day"]].append(r["run"])
            for r in allr:
                ab[r["day"]].append(r["run"])
            lift = {d: [sum(v) / len(v) - sum(ab[d]) / len(ab[d])] for d, v in kb.items() if v}
            kn, kt, n, nd = E.day_equal(kb)
            lf, lt, _, _ = E.day_equal(lift)
            share = len(kept) / len(known) if known else 0.0
            res[(f, h)] = {"kept": len(kept), "known": len(known), "share": share, "kept_net": kn, "kept_t": kt,
                           "lift": lf, "lift_t": lt, "all_net": E.day_equal(ab)[0], "days": nd}
        P, A_, B_ = res[(f, "ALL")], res[(f, "A")], res[(f, "B")]
        ok = P["kept"] >= MIN_KEEP_N and P["share"] >= MIN_KEEP_SHARE and None not in (P["kept_net"], P["kept_t"], P["lift"], P["lift_t"])
        same = all(x["kept_net"] is not None and x["kept_net"] > 0 and x["lift"] is not None and x["lift"] > 0 for x in (A_, B_))
        passed = ok and P["kept_net"] > 0 and P["kept_t"] >= T_BAR and P["lift"] >= LIFT_BAR and P["lift_t"] >= T_BAR and same
        P["verdict"] = "UNDERPOWERED" if not ok else ("PASS" if passed else "FAIL")
    return res, cov


def _bp(x):
    return "—" if x is None else f"{x * 1e4:+.1f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    datas, _ex = E.load_days()
    sc = R.measured_stop_cost(R.load_fills())
    res, cov = score(datas, sc)
    print(f"trait coverage {dict(cov)}")
    for f in FILTERS:
        print(f"  {f}: kept " + "; ".join(f"{h} {res[(f, h)]['kept']}/{res[(f, h)]['known']} ({res[(f, h)]['share']:.0%})" for h in ("A", "B", "ALL")))
    if not a.score:
        print("(inventory only; --score after a result-skeptic READY)")
        return
    lines = ["# Runner entry filter (runner exit as right_tail_runner, 5 bp + measured stop slippage)", "",
             "| filter | half | kept / known | kept runner net (t) | all runner net | lift (t) | verdict |",
             "|---|---|---:|---:|---:|---:|---|"]
    for f in FILTERS:
        for h in ("A", "B", "ALL"):
            x = res[(f, h)]
            lines.append(f"| {f} | {h} | {x['kept']}/{x['known']} ({x['share']:.0%}) | {_bp(x['kept_net'])} ({x['kept_t'] and round(x['kept_t'], 2)}) "
                         f"| {_bp(x['all_net'])} | {_bp(x['lift'])} ({x['lift_t'] and round(x['lift_t'], 2)}) | {x.get('verdict', '')} |")
    out = "\n".join(lines)
    print(out)
    with open(os.path.join(E.OUT_DIR, "runner_filter_result.md"), "w") as fh:
        fh.write(out + "\n")


if __name__ == "__main__":
    main()
