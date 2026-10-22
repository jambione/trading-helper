#!/usr/bin/env python3
"""Score docs/studies/sr_breakout_forward_prereg.json (registered 2026-10-06) on the held-out desk fills.

Reads ai_reports/order_block_gate/heldout_fills.jsonl (written per session by ob_fills_daily.py). Per hypothesis: realized
bp per fill, group minus the reference group, OLS with day fixed effects (within-day demeaning), day-clustered CR1 t.
PASS = difference >= 5 bp in the predicted direction, t >= 2.4 in that direction, >= 30 fills in the tested group, and
the same sign in the first and second half of the eligible sessions; < 30 fills = UNDERPOWERED; else NOT_PASSED.

  H1 resist_0.3 True minus False (predicted negative; 2026-10-06 excluded)
  H2 brk_dist_pct >= 0.30 minus no breakout (predicted negative)
  H3 0.10 <= brk_dist_pct < 0.30 minus no breakout (predicted positive)
The caveat 'a trail change mid-test ... the test is split at it' is reported per exit regime as information.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.path.join(ROOT, "ai_reports", "order_block_gate", "heldout_fills.jsonl")
FIRST = "2026-10-05"
BAR, T_PASS, MIN_N = 5.0, 2.4, 30
# exit-regime boundaries after 10/06 (memory exits-np-lob-on-2026-10-08): np180 + lob on 10/08 (lob off 10:30 that
# day), np cut off 2026-10-15 11:25 ET; sessions are split at the day level (10/08 and 10/15 start new regimes).
REGIMES = (("to 10/07", "0000", "2026-10-07"), ("10/08-10/14 np180 on", "2026-10-08", "2026-10-14"),
           ("10/15 on, np off", "2026-10-15", "9999"))


def group(r: dict, hyp: str) -> str | None:
    """'G' tested group, 'R' reference, None = neither / not eligible."""
    if r.get("realized_bp") is None:
        return None
    if hyp == "H1":
        if r["day"] == "2026-10-06" or r.get("resist_0.3") is None:
            return None
        return "G" if r["resist_0.3"] else "R"
    b = r.get("brk_dist_pct")
    if b is None:
        return "R"
    if hyp == "H2":
        return "G" if b >= 0.30 else None
    return "G" if 0.10 <= b < 0.30 else None


def fe_diff(rows: list[dict], hyp: str) -> dict:
    """OLS of y on the group dummy with day fixed effects; CR1 day-clustered SE."""
    by = defaultdict(list)
    for r in rows:
        g = group(r, hyp)
        if g is not None and r["day"] >= FIRST:
            by[r["day"]].append((1.0 if g == "G" else 0.0, float(r["realized_bp"])))
    dm = []
    for d, xs in by.items():
        md, my = sum(x for x, _ in xs) / len(xs), sum(y for _, y in xs) / len(xs)
        dm.append((d, [(x - md, y - my) for x, y in xs]))
    sxx = sum(x * x for _, xs in dm for x, _ in xs)
    n_g = sum(1 for _, xs in by.items() for x, _ in xs if x == 1.0)
    n = sum(len(xs) for xs in by.values())
    if sxx <= 0:
        return {"diff": None, "t": None, "n_group": n_g, "n": n, "days": len(by)}
    beta = sum(x * y for _, xs in dm for x, y in xs) / sxx
    G, K = len(dm), len(dm) + 1
    meat = sum(sum(x * (y - beta * x) for x, y in xs) ** 2 for _, xs in dm)
    adj = (G / (G - 1)) * ((n - 1) / max(1, n - K)) if G > 1 else float("nan")
    se = math.sqrt(adj * meat) / sxx if G > 1 else None
    return {"diff": beta, "t": beta / se if se else None, "n_group": n_g, "n": n, "days": G}


def verdict(full: dict, halves: list[dict], hyp: str) -> str:
    sign = -1 if hyp in ("H1", "H2") else 1
    if full["n_group"] < MIN_N:
        return "UNDERPOWERED"
    if full["diff"] is None or full["t"] is None:
        return "NOT_PASSED"
    ok = (sign * full["diff"] >= BAR and sign * full["t"] >= T_PASS
          and all(h["diff"] is not None and sign * h["diff"] > 0 for h in halves))
    return "PASS" if ok else "NOT_PASSED"


def main() -> None:
    rows = [json.loads(x) for x in open(PATH) if x.strip()]
    days = sorted({r["day"] for r in rows if r["day"] >= FIRST})
    res = {"sessions": days, "fills": len(rows), "hypotheses": {}}
    for hyp in ("H1", "H2", "H3"):
        el = sorted({r["day"] for r in rows if r["day"] >= FIRST and not (hyp == "H1" and r["day"] == "2026-10-06")})
        cut = el[: len(el) // 2]
        full = fe_diff(rows, hyp)
        halves = [fe_diff([r for r in rows if r["day"] in set(cut)], hyp),
                  fe_diff([r for r in rows if r["day"] in set(el) - set(cut)], hyp)]
        regimes = {lab: fe_diff([r for r in rows if lo <= r["day"] <= hi], hyp) for lab, lo, hi in REGIMES}
        res["hypotheses"][hyp] = {"full": full, "half1": halves[0], "half2": halves[1],
                                  "half_split": [cut[0], cut[-1], el[len(cut)], el[-1]] if cut else None,
                                  "by_exit_regime": regimes, "verdict": verdict(full, halves, hyp)}
    out = os.path.join(ROOT, "ai_reports", "order_block_gate", "forward_result.json")
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    sys.exit(main())
