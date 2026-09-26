#!/usr/bin/env python3
"""edge_a_cost_decomp.py — Round-1 strategy-edge study, part A (2026-09-26).

For every round trip the desk actually filled (ai_reports/fills/DAY.jsonl), split
the P&L into
  move   mid-to-mid: SIP mid at the sell submit / SIP mid at the buy submit - 1
  cost   what the fills paid vs those mids: buy fill/mid - 1 + 1 - sell fill/mid
         = half-spread paid (in + out) + excess slippage
Mids come from the SIP NBBO at submit (same lookup as tools/exec_report.py and
the nightly job: historical SIP quotes, free once 15 min old). Cached to
ai_reports/edge_nbbo_cache.json.

USAGE (mini, after hours):  .venv/bin/python tools/studies/edge_a_cost_decomp.py 2026-09-16 2026-09-25
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict, deque

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import exec_report as er  # noqa: E402

CACHE = os.path.join(ROOT, "ai_reports", "edge_nbbo_cache.json")


def main():
    d0, d1 = sys.argv[1], sys.argv[2]
    days = sorted(f[:-6] for f in os.listdir(os.path.join(ROOT, "ai_reports", "fills"))
                  if f.endswith(".jsonl") and d0 <= f[:-6] <= d1)
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    cl = bars.client()
    trades = []
    unmatched = 0
    for day in days:
        os_ = er.orders(day)
        for o in os_:
            if not o["t_sub"]:
                continue
            k = o["oid"]
            if k not in cache:
                q = er.nbbo_at(cl, o["sym"], o["t_sub"])
                cache[k] = list(q) if q else None
                time.sleep(0.3)
            q = cache[k]
            o["bid"], o["ask"] = (q if q else (None, None))
        json.dump(cache, open(CACHE, "w"))
        book = defaultdict(deque)
        for o in os_:
            qty = float(o["qty"] or 0)
            if qty <= 0:
                continue
            if o["side"] == "buy":
                book[o["sym"]].append([o, qty])
            else:
                left = qty
                while left > 1e-9 and book[o["sym"]]:
                    b, bq = book[o["sym"]][0]
                    q = min(bq, left)
                    trades.append((day, b, o, q))
                    left -= q
                    book[o["sym"]][0][1] -= q
                    if book[o["sym"]][0][1] <= 1e-9:
                        book[o["sym"]].popleft()
                if left > 1e-9:
                    unmatched += 1
    rows = []
    for day, b, s, q in trades:
        if not (b.get("bid") and s.get("bid")):
            continue
        mb = (b["bid"] + b["ask"]) / 2
        ms = (s["bid"] + s["ask"]) / 2
        notional = b["px"] * q
        r = {
            "day": day, "sym": b["sym"], "px": b["px"], "q": q, "notional": notional,
            "gross": (s["px"] / b["px"] - 1) * 1e4,
            "move": (ms / mb - 1) * 1e4,
            "cost_in": (b["px"] / mb - 1) * 1e4,
            "cost_out": (1 - s["px"] / ms) * 1e4,
            "half_in": (b["ask"] - b["bid"]) / 2 / mb * 1e4,
            "half_out": (s["ask"] - s["bid"]) / 2 / ms * 1e4,
            "pnl": (s["px"] - b["px"]) * q,
            "hold_min": ((s["t_sub"] - b["t_sub"]).total_seconds() / 60),
            "note_out": s["note"][:20],
        }
        r["cost"] = r["cost_in"] + r["cost_out"]
        r["spread_paid"] = r["half_in"] + r["half_out"]
        r["excess"] = r["cost"] - r["spread_paid"]
        r["move_usd"] = r["move"] / 1e4 * notional
        r["cost_usd"] = r["cost"] / 1e4 * notional
        rows.append(r)
    print(f"PART A — {len(rows)} round trips with SIP mids ({days[0]}..{days[-1]}), unmatched sells {unmatched}\n")

    def agg(label, rs):
        if not rs:
            return
        m = lambda k: statistics.mean(x[k] for x in rs)
        pnl = sum(x["pnl"] for x in rs)
        mu = sum(x["move_usd"] for x in rs)
        cu = sum(x["cost_usd"] for x in rs)
        sd = statistics.pstdev([x["move"] for x in rs]) / math.sqrt(len(rs)) if len(rs) > 1 else 0
        print(f"  {label:<18}{len(rs):>5}{m('gross'):>+8.1f}{m('move'):>+8.1f} (±{1.96*sd:4.1f}){m('cost'):>+7.1f}"
              f"{m('spread_paid'):>7.1f}{m('excess'):>+7.1f}{m('hold_min'):>7.1f}   ${pnl:>+8.2f} = move ${mu:>+8.2f} - cost ${cu:>7.2f}")
    hdr = (f"  {'group':<18}{'n':>5}{'gross':>8}{'move':>8}{'  95%':>9}{'cost':>7}{'sprd':>7}{'excess':>7}{'hold':>7}"
           "   P&L decomposition (bp are per trade, notional-unweighted)")
    print(hdr)
    agg("ALL", rows)
    for day in days:
        agg(day, [r for r in rows if r["day"] == day])
    print()
    print(hdr)
    for lo, hi in ((0, 20), (20, 30), (30, 50), (50, 100), (100, 1e9)):
        agg(f"${lo}-{hi if hi < 1e9 else '+'}", [r for r in rows if lo <= r["px"] < hi])
    for lo, hi in ((0, 3), (3, 6), (6, 10), (10, 20), (20, 1e9)):
        agg(f"half-spr {lo}-{hi if hi<1e9 else '+'}bp", [r for r in rows if lo <= r["half_in"] < hi])
    print()
    print(hdr)
    for lo, hi in ((0, 5), (5, 15), (15, 30), (30, 1e9)):
        agg(f"hold {lo}-{hi if hi<1e9 else '+'}min", [r for r in rows if lo <= r["hold_min"] < hi])
    tot = sum(r["pnl"] for r in rows)
    cu = sum(r["cost_usd"] for r in rows)
    print(f"\n  total P&L ${tot:+.2f}; cost ${cu:.2f}; move ${sum(r['move_usd'] for r in rows):+.2f}; "
          f"cost share of loss {cu / -tot:.0%}" if tot < 0 else "")
    json.dump(rows, open("/tmp/edge_a_rows.json", "w"), default=str)


if __name__ == "__main__":
    main()
