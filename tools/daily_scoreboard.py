#!/usr/bin/env python3
"""Daily scoreboard (operator 11/05): one line per session and a rolling-20 summary from the desk's own outcomes.
Reads ai_reports/tapes/<day>/outcomes.jsonl (single-day folders), de-duplicated by (symbol, round(entry_time)).
Net bp/trade = (exit-entry)/entry - 3 bp cost. Never-green = peak_price < entry +5 bp. Writes docs/SCOREBOARD.md.
usage: tools/daily_scoreboard.py [--days 20]"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import statistics as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COST_BP = 3.0


def load_day(path):
    rows, seen = [], set()
    for line in open(path):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        e, x, t = o.get("entry_price"), o.get("exit_price"), o.get("entry_time")
        sym = str(o.get("symbol") or "")
        if not (isinstance(e, (int, float)) and e > 0 and x and t) or not re.fullmatch(r"[A-Z]{1,5}", sym):
            continue
        k = (sym, round(t))
        if k in seen:
            continue
        seen.add(k)
        pk = o.get("peak_price") or e
        rows.append({"bp": (x - e) / e * 1e4 - COST_BP, "usd": float(o.get("realized_pl_usd") or 0.0),
                     "ng": (pk - e) / e * 1e4 < 5})
    return rows


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=20)
    n = ap.parse_args().days
    days = []
    for f in sorted(glob.glob(os.path.join(ROOT, "ai_reports/tapes/2026-*/outcomes.jsonl"))):
        d = f.split("/")[-2]
        if len(d) == 10:
            r = load_day(f)
            if r:
                days.append((d, r))
    days = days[-n:]
    if not days:
        print("no sessions"); return 1
    out = ["# Desk scoreboard", "", f"Rolling {len(days)} sessions ending {days[-1][0]}. Net = exit vs entry less {COST_BP:g} bp. "
           "Never-green = no print at or above entry +5 bp. Dollars are the desk's realized P&L.", "",
           "| day | trades | win % | never-green % | net bp/trade | $ |", "|---|---:|---:|---:|---:|---:|"]
    per_day = []
    for d, r in days:
        nb = sum(x["bp"] for x in r) / len(r)
        per_day.append(nb)
        out.append(f"| {d} | {len(r)} | {100*sum(x['bp']>0 for x in r)/len(r):.0f} | {100*sum(x['ng'] for x in r)/len(r):.0f} | {nb:+.1f} | {sum(x['usd'] for x in r):+.2f} |")
    allr = [x for _, r in days for x in r]
    m = sum(x["bp"] for x in allr) / len(allr)
    se = (st.pstdev(per_day) / math.sqrt(len(per_day))) if len(per_day) > 2 else float("nan")
    last5 = [x for _, r in days[-5:] for x in r]
    line = (f"ROLLING {len(days)}: {len(allr)} trades, net {m:+.1f} bp/trade (day-level SE {se:.1f}), "
            f"win {100*sum(x['bp']>0 for x in allr)/len(allr):.0f}%, never-green {100*sum(x['ng'] for x in allr)/len(allr):.0f}%, "
            f"${sum(x['usd'] for x in allr):+.2f}; last 5 sessions net {sum(x['bp'] for x in last5)/max(1,len(last5)):+.1f} bp/trade")
    out += ["", f"**{line}**", ""]
    open(os.path.join(ROOT, "docs/SCOREBOARD.md"), "w").write("\n".join(out))
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
