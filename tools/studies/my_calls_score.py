#!/usr/bin/env python3
"""Score the operator's "Buy?" calls from the book (ai_reports/my_calls.jsonl).

Scored the honest way the entry studies use (operator 10/16 Call bar):
  entry   open of the first SIP 1m bar starting after the click (premarket included, 04:00-15:45; the bar cache ends 16:00)
  cost    the full SIP quoted spread at the click (replay_costing.entry_spread_bp); 0.20% when no quote (counted)
  net     close 5 / 15 / 30 min after entry (capped 15:59) / entry - 1 - cost; MFE/MAE inside the 30 min
  lift    net15 minus net15 of ONE random LATER minute of the same name (1-60 min after the click), seeded per call;
          a same-hour control sits on the run-up a momentum call fires after and flatters it (10/13 calibration)
Repeat clicks on one name within 5 minutes count once. Retroactive entries (said after the fact) are listed, never scored.

Run on the mini (needs Alpaca data): .venv/bin/python tools/studies/my_calls_score.py [--since 2026-10-05]
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import statistics
import sys
from datetime import datetime

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not os.path.isdir(os.path.join(ROOT, "ai_reports")):
    ROOT = os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import indicator_levels_study as ils  # noqa: E402

COST = 0.0020


def at(day, hh, mm):
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=bars.ET).timestamp()


def path_from(t, o, h, l, c, i, day, hold, cost=COST):
    """(entry, net, mfe, mae) entering at the open of bar i."""
    if i >= len(t):
        return None
    px = float(o[i])
    tx = min(float(t[i]) + hold * 60, at(day, 15, 59))
    j = int(np.searchsorted(t, tx, side="right")) - 1
    if j < i or px <= 0:
        return None
    return (px, float(c[j]) / px - 1 - cost, float(h[i:j + 1].max()) / px - 1, float(l[i:j + 1].min()) / px - 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2000-01-01")
    ap.add_argument("--file", default=os.path.join(ROOT, "ai_reports", "my_calls.jsonl"))
    a = ap.parse_args()
    calls = []
    for line in open(a.file):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        d = bars.day_of(float(r["ts"]))
        if d >= a.since:
            calls.append({**r, "day": d})
    calls.sort(key=lambda r: r["ts"])
    kept, last = [], {}
    for r in calls:
        if r["ts"] - last.get(r["symbol"], -1e9) >= 300:
            kept.append(r)
            last[r["symbol"]] = r["ts"]
    if not kept:
        print("no calls yet")
        return
    want = collections.defaultdict(set)
    for r in kept:
        want[r["day"]].add(r["symbol"])
    cache = ils.fetch_ext(want)

    import random
    import replay_costing as RC
    qcache, box, no_quote = RC.load_cache(), {}, 0
    rows, per_day_lift = [], collections.defaultdict(list)
    for r in kept:
        df = cache.get((r["symbol"], r["day"]))
        m = bars.et_minutes(r["ts"])
        tag = ""
        if r.get("retroactive"):
            tag = "retroactive (not scored)"
        elif df is None or len(df) < 30:
            tag = "no bars"
        elif not (4 * 60 <= m <= 15 * 60 + 45):
            tag = "outside 04:00-15:45"
        if tag:
            rows.append((r, None, None, None, None, tag))
            continue
        sp = RC.entry_spread_bp(qcache, r["symbol"], r["ts"], box)
        if sp is None:
            no_quote += 1
        cost = COST if sp is None else sp / 1e4
        t = np.array([x.timestamp() for x in df.index])
        o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
        i = int(np.searchsorted(t, r["ts"], side="right"))            # first bar starting after the click
        p5, p15, p30 = (path_from(t, o, h, l, c, i, r["day"], hh, cost) for hh in (5, 15, 30))
        later = [k for k in range(i + 1, min(len(t), i + 61)) if t[k] - t[i] <= 3600]
        rng = random.Random(f"{r['symbol']}|{r['ts']}|my-call-later-v1")
        pc = path_from(t, o, h, l, c, rng.choice(later), r["day"], 15, cost) if later else None
        lift = (p15[1] - pc[1]) if p15 and pc else None
        if lift is not None:
            per_day_lift[r["day"]].append(lift)
        rows.append((r, p5, p15, p30, lift, ""))
    RC.save_cache(qcache)

    bp = lambda x: "—" if x is None else f"{x * 1e4:+.0f}"
    print(f"{'time':19} {'sym':6} {'setup':10} {'entry':>9} {'net5':>6} {'net15':>6} {'net30':>6} {'MFE30':>6} {'MAE30':>6} {'lift':>6}")
    for r, p5, p15, p30, lift, tag in rows:
        when = datetime.fromtimestamp(r["ts"], bars.ET).strftime("%m-%d %H:%M:%S")
        setup = str(r.get("setup") or "")[:10]
        if tag:
            print(f"{when:19} {r['symbol']:6} {setup:10}  {tag}")
            continue
        print(f"{when:19} {r['symbol']:6} {setup:10} {p15[0] if p15 else float('nan'):>9.4f} {bp(p5 and p5[1]):>6} "
              f"{bp(p15 and p15[1]):>6} {bp(p30 and p30[1]):>6} {bp(p30 and p30[2]):>6} {bp(p30 and p30[3]):>6} {bp(lift):>6}")
    n15 = [p15[1] for _, _, p15, _, _, tag in rows if p15 and not tag]
    lifts = [x for v in per_day_lift.values() for x in v]
    if n15:
        print(f"\ncalls scored {len(n15)} (of {len(rows)}; {no_quote} without a SIP quote, charged 0.20%): "
              f"net15 mean {statistics.mean(n15) * 1e4:+.1f} bp, win {np.mean(np.array(n15) > 0):.0%}")
    if lifts:
        dm = [statistics.mean(v) for v in per_day_lift.values()]
        t = statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm))) if len(dm) > 2 and statistics.stdev(dm) > 0 else None
        print(f"lift vs a random LATER minute (same name, 1-60 min after): {statistics.mean(dm) * 1e4:+.1f} bp day-mean over "
              f"{len(lifts)} calls, {len(dm)} days" + (f", day-clustered t {t:+.1f}" if t is not None else ""))
        print("Bar used for every desk rule: >= +5 bp with t >= 2 in both halves of the days, net > 0, >= 100 calls.")


if __name__ == "__main__":
    main()
