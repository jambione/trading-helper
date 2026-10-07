#!/usr/bin/env python3
"""Forward score for docs/studies/slot_1000_skip_prereg.json. Run on the mini: .venv/bin/python tools/studies/slot_skip_score.py"""
from __future__ import annotations

import collections
import json
import math
import os
import statistics
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
from order_block_gate import fe_diff  # noqa: E402

ET = ZoneInfo("America/New_York")
START = "2026-10-08"


def main():
    rows = []
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not (d.get("entry_price") and d.get("exit_price") and isinstance(d.get("entry_time"), (int, float))):
            continue
        t = datetime.fromtimestamp(d["entry_time"], ET)
        day, m = t.strftime("%Y-%m-%d"), t.hour * 60 + t.minute
        if day < START or not (9 * 60 + 45 <= m < 15 * 60 + 30):
            continue
        rows.append({"day": day, "m": m, "slot": 600 <= m < 630, "bp": (d["exit_price"] / d["entry_price"] - 1) * 1e4,
                     "usd": d.get("realized_pl_usd") or 0.0, "src": d.get("source") or "?"})
    days = sorted({r["day"] for r in rows})
    print(f"sessions {len(days)} ({days[0] if days else '-'}..{days[-1] if days else '-'}), fills {len(rows)}, SLOT fills {sum(r['slot'] for r in rows)}")
    if len(days) < 3 or not any(r["slot"] for r in rows):
        print("too early")
        return
    def diff(rs):
        b, se = fe_diff([r["bp"] for r in rs], [1 if r["slot"] else 0 for r in rs], [r["day"] for r in rs])
        return b, (b / se if se else float("nan"))
    b, t = diff(rows)
    h = len(days) // 2
    b1, t1 = diff([r for r in rows if r["day"] in days[:h]]) if h >= 2 else (float("nan"), float("nan"))
    b2, t2 = diff([r for r in rows if r["day"] in days[h:]]) if len(days) - h >= 2 else (float("nan"), float("nan"))
    S = [r for r in rows if r["slot"]]
    R = [r for r in rows if not r["slot"]]
    for lab, g in (("SLOT 10:00-10:30", S), ("REST", R), ("09:45-10:00", [r for r in R if r["m"] < 600]),
                   ("10:30-11:00", [r for r in R if 630 <= r["m"] < 660])):
        if g:
            print(f"  {lab:<18} n {len(g):4d} mean {statistics.fmean(x['bp'] for x in g):+7.1f} bp  median {statistics.median(x['bp'] for x in g):+6.1f}"
                  f"  win {sum(x['bp'] > 0 for x in g) / len(g):.0%}  $/day {sum(x['usd'] for x in g) / len(days):+.2f}")
    print(f"SLOT - REST (day FE): {b:+.1f} bp, t {t:+.2f} | first half {b1:+.1f} (t {t1:+.2f}) | second half {b2:+.1f} (t {t2:+.2f})")
    ok = len(days) >= 10 and len(S) >= 60 and b <= -5 and t <= -2 and b1 < 0 and b2 < 0
    print("VERDICT:", "PASS (skeptic review, then a paper A/B of the skip)" if ok else
          ("not yet scoreable (need >= 10 sessions and >= 60 SLOT fills)" if (len(days) < 10 or len(S) < 60) else "FAIL (no skip)"))


if __name__ == "__main__":
    main()
